import httpx
import pytest
import respx
import sqlalchemy as sa

from tests.conftest import NOW
from trade_news.db.schema import subscribers
from trade_news.telegram import bot
from trade_news.telegram import subscribers as subs
from trade_news.telegram.api import TelegramError, make_api

ROOT = 111
USER = {"id": 222, "username": "alice", "first_name": "Alice"}


class FakeApi:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    def __call__(self, method, **params):
        assert method == "sendMessage"
        self.sent.append((params["chat_id"], params["text"]))
        return {"message_id": len(self.sent)}

    def to(self, chat_id):
        return [text for cid, text in self.sent if cid == chat_id]


def message(text, chat_id=222, chat_type="private", user=USER, title=None):
    chat = {"id": chat_id, "type": chat_type, **({"title": title} if title else {})}
    return {"update_id": 1, "message": {"chat": chat, "from": user, "text": text}}


def member(status, chat_id=222, chat_type="private", title=None):
    chat = {"id": chat_id, "type": chat_type, **({"title": title} if title else {})}
    return {
        "update_id": 1,
        "my_chat_member": {"chat": chat, "from": USER, "new_chat_member": {"status": status}},
    }


@pytest.fixture
def handle(engine):
    api = FakeApi()

    def _handle(update, bot_username="trade_news_bot"):
        bot.handle_update(
            engine, api, update, root_chat_id=ROOT, bot_username=bot_username, now=lambda: NOW
        )

    _handle.api = api
    return _handle


def active(engine):
    with engine.connect() as conn:
        return subs.active_chat_ids(conn)


def test_start_subscribes_immediately_and_notifies_owner(engine, handle):
    handle(message("/start"))
    assert active(engine) == [222]
    assert handle.api.to(222) == [bot.SUBSCRIBED]
    assert handle.api.to(ROOT) == ["🆕 Подписался: @alice (chat_id 222)"]


def test_start_twice_is_idempotent(engine, handle):
    handle(message("/start"))
    handle(message("/start"))
    assert active(engine) == [222]
    assert handle.api.to(222)[-1] == bot.ALREADY_SUBSCRIBED
    assert len(handle.api.to(ROOT)) == 1


def test_stop_and_resubscribe_keeps_one_row(engine, handle):
    handle(message("/start"))
    handle(message("/stop"))
    assert active(engine) == []
    assert handle.api.to(222)[-1] == bot.UNSUBSCRIBED
    handle(message("/stop"))
    assert handle.api.to(222)[-1] == bot.NOT_SUBSCRIBED
    handle(message("/start"))
    with engine.connect() as conn:
        rows = conn.execute(sa.select(subscribers)).all()
    assert len(rows) == 1 and rows[0].is_active and rows[0].unsubscribed_at is None


def test_blocking_the_bot_unsubscribes(engine, handle):
    handle(message("/start"))
    handle(member("kicked"))
    assert active(engine) == []
    with engine.connect() as conn:
        assert conn.execute(sa.select(subscribers.c.unsubscribe_reason)).scalar() == "blocked"


def test_group_start_and_commands_for_other_bots(engine, handle):
    handle(message("/start@other_bot", chat_id=-100500, chat_type="supergroup", title="Traders"))
    assert active(engine) == []
    handle(
        message("/start@Trade_News_Bot", chat_id=-100500, chat_type="supergroup", title="Traders")
    )
    assert active(engine) == [-100500]
    assert handle.api.to(ROOT) == ["🆕 Подписался: Traders (chat_id -100500)"]


def test_channel_subscribes_when_bot_made_admin(engine, handle):
    handle(member("administrator", chat_id=-100777, chat_type="channel", title="News"))
    assert active(engine) == [-100777]


def test_owner_always_receives_and_is_not_notified_about_self(engine, handle):
    handle(message("/start", chat_id=ROOT, user={"id": ROOT, "username": "owner"}))
    handle(message("/stop", chat_id=ROOT, user={"id": ROOT, "username": "owner"}))
    assert bot.OWNER_NOTE in handle.api.to(ROOT)[-1]
    assert not any("Подписался" in t for t in handle.api.to(ROOT))
    handle(message("/start"))
    with engine.connect() as conn:
        assert subs.recipients(conn, ROOT) == [ROOT, 222]


def test_help_and_unknown_text(handle):
    handle(message("hello"))
    handle(message("/unknown"))
    assert handle.api.sent == []
    handle(message("/help"))
    assert bot.NOT_SUBSCRIBED in handle.api.to(222)[0]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/start", "start"),
        ("/STOP now", "stop"),
        ("/help@my_bot", "help"),
        ("/help@x", None),
        ("hi", None),
    ],
)
def test_parse_command(text, expected):
    assert bot.parse_command(text, "my_bot") == expected


@respx.mock
def test_api_retries_429_with_retry_after_and_hides_token(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    route = respx.post("https://api.telegram.org/botSECRET/sendMessage").mock(
        side_effect=[
            httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 3},
                },
            ),
            httpx.Response(200, json={"ok": True, "result": {"message_id": 7}}),
        ]
    )
    with httpx.Client() as client:
        api = make_api(client, "SECRET")
        assert api("sendMessage", chat_id=1, text="x") == {"message_id": 7}
        respx.post("https://api.telegram.org/botSECRET/getMe").mock(
            return_value=httpx.Response(
                403,
                json={
                    "ok": False,
                    "error_code": 403,
                    "description": "Forbidden: bot was blocked by the user",
                },
            )
        )
        with pytest.raises(TelegramError) as ei:
            api("getMe")
    assert route.call_count == 2
    assert ei.value.chat_unreachable and "SECRET" not in str(ei.value)


def test_poll_loop_survives_errors_and_stops(engine):
    import threading

    stop = threading.Event()
    calls = []

    def api(method, **params):
        calls.append(method)
        if method == "getMe":
            return {"username": "b"}
        if method == "setMyCommands":
            return True
        if method == "getUpdates":
            if calls.count("getUpdates") == 1:
                raise TelegramError("getUpdates", 409, "Conflict", None)
            if calls.count("getUpdates") == 2:
                return [message("/start")]
            stop.set()
            return []
        return {"message_id": 1}

    stop.wait = lambda _t: None  # no real sleeping between retries
    bot.poll_forever(engine, api, root_chat_id=None, stop=stop, now=lambda: NOW)
    assert active(engine) == [222]


def test_sources_command_shows_status(engine, handle, monkeypatch):
    from datetime import timedelta

    from trade_news.db.schema import collector_runs

    with engine.begin() as conn:
        conn.execute(
            collector_runs.insert(),
            [
                {
                    "source": "sec_edgar",
                    "started_at": NOW - timedelta(hours=30),
                    "status": "ok",
                    "inserted": 500,
                },
                {
                    "source": "sec_edgar",
                    "started_at": NOW - timedelta(minutes=5),
                    "status": "ok",
                    "inserted": 7,
                },
                {
                    "source": "finnhub",
                    "started_at": NOW - timedelta(hours=2),
                    "status": "error",
                    "inserted": 0,
                },
            ],
        )
    sources = [("sec_edgar", "SEC EDGAR <filings>"), ("finnhub", "Finnhub"), ("new_src", "")]
    bot.handle_update(
        engine,
        handle.api,
        message("/sources"),
        root_chat_id=ROOT,
        bot_username="b",
        now=lambda: NOW,
        sources=sources,
    )
    (text,) = handle.api.to(222)
    assert "Источники (3)" in text
    assert "✅ SEC EDGAR &lt;filings&gt;" in text  # escaped for HTML parse mode
    assert "5 мин назад" in text and "за 24 ч: 7" in text  # the 30h-old run is outside 24h
    assert "⚠️ Finnhub" in text and "2 ч назад, ошибка" in text
    assert "⏳ new_src" in text and "ещё не запускался" in text


def test_sources_without_collectors():
    assert bot.format_sources([], NOW) == "Сейчас не подключено ни одного источника."
