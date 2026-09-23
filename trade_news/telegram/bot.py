"""Subscription bot: long-polls getUpdates and handles /start, /stop, /help.

- /start subscribes the chat immediately (private chat or group), no approval step;
- /stop unsubscribes;
- /help shows what the bot does and the chat's subscription status;
- the bot being blocked / kicked (my_chat_member → kicked|left) unsubscribes the chat;
- a channel subscribes by adding the bot as an administrator (channels can't send /start).

The owner (TELEGRAM_CHAT_ID) always receives the mailing and gets a short notice when someone
subscribes or unsubscribes. There are no other roles.

Only one process may call getUpdates per bot token (Telegram answers 409 Conflict otherwise),
so this bot needs its own token — not one shared with another polling app.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime

import sqlalchemy as sa
import structlog

from trade_news.telegram import subscribers as subs
from trade_news.telegram.api import Api, TelegramError

log = structlog.get_logger()

COMMANDS = [
    ("start", "Подписаться на рассылку"),
    ("stop", "Отписаться от рассылки"),
    ("help", "Что это за бот и статус подписки"),
]
ABOUT = (
    "📰 <b>trade-news</b>: отобранные новости и первичные данные по акциям США, крипте "
    "и форексу. В каждом сообщении суть, актив, дата применимости и ссылка на первоисточник."
)
SUBSCRIBED = "✅ Вы подписаны на рассылку. Отписаться: /stop"
ALREADY_SUBSCRIBED = "Вы уже подписаны. Отписаться: /stop"
UNSUBSCRIBED = "❌ Подписка отменена. Подписаться снова: /start"
NOT_SUBSCRIBED = "Вы не подписаны. Подписаться: /start"
OWNER_NOTE = "Вы владелец бота (TELEGRAM_CHAT_ID) и получаете рассылку всегда."
POLL_TIMEOUT = 25


def chat_title(chat: dict, user: dict | None = None) -> str:
    if chat.get("type") in ("group", "supergroup", "channel"):
        return chat.get("title") or str(chat["id"])
    who = user or chat
    if who.get("username"):
        return f"@{who['username']}"
    name = " ".join(p for p in (who.get("first_name"), who.get("last_name")) if p)
    return name or str(chat["id"])


def parse_command(text: str, bot_username: str | None) -> str | None:
    """'/start@my_bot arg' → 'start'. Commands addressed to another bot are ignored."""
    if not text.startswith("/"):
        return None
    head = text.split(maxsplit=1)[0][1:]
    cmd, _, target = head.partition("@")
    if target and bot_username and target.lower() != bot_username.lower():
        return None
    return cmd.lower()


def handle_update(
    engine: sa.Engine,
    api: Api,
    update: dict,
    *,
    root_chat_id: int | None,
    bot_username: str | None,
    now: Callable[[], datetime],
) -> None:
    if msg := update.get("message"):
        chat = msg["chat"]
        cmd = parse_command(msg.get("text") or "", bot_username)
        if cmd in ("start", "stop", "help"):
            _handle_command(engine, api, cmd, chat, msg.get("from"), root_chat_id, now())
    elif member := update.get("my_chat_member"):
        _handle_membership(engine, api, member, root_chat_id, now())


def _handle_command(engine, api, cmd, chat, user, root_chat_id, now) -> None:
    chat_id = chat["id"]
    title = chat_title(chat, user)
    if cmd == "start":
        with engine.begin() as conn:
            new = subs.subscribe(conn, chat_id, chat.get("type"), title, now)
        _reply(api, chat_id, SUBSCRIBED if new else ALREADY_SUBSCRIBED)
        if new:
            log.info("telegram_subscribed", chat_id=chat_id, title=title)
            _notify_owner(api, root_chat_id, chat_id, f"🆕 Подписался: {title}")
    elif cmd == "stop":
        with engine.begin() as conn:
            was = subs.unsubscribe(conn, chat_id, "stop", now)
        text = UNSUBSCRIBED if was else NOT_SUBSCRIBED
        if chat_id == root_chat_id:
            text += f"\n\n{OWNER_NOTE}"
        _reply(api, chat_id, text)
        if was:
            log.info("telegram_unsubscribed", chat_id=chat_id, title=title, reason="stop")
            _notify_owner(api, root_chat_id, chat_id, f"👋 Отписался: {title}")
    else:  # help
        with engine.connect() as conn:
            active = subs.is_active(conn, chat_id)
        status = SUBSCRIBED if active else NOT_SUBSCRIBED
        if chat_id == root_chat_id:
            status = OWNER_NOTE
        cmds = "\n".join(f"/{c} — {d}" for c, d in COMMANDS)
        _reply(api, chat_id, f"{ABOUT}\n\n{status}\n\n<b>Команды:</b>\n{cmds}", html=True)


def _handle_membership(engine, api, member, root_chat_id, now) -> None:
    chat = member["chat"]
    chat_id = chat["id"]
    status = member["new_chat_member"]["status"]
    title = chat_title(chat, member.get("from"))
    if status in ("kicked", "left"):
        with engine.begin() as conn:
            was = subs.unsubscribe(conn, chat_id, "blocked", now)
        if was:
            log.info("telegram_unsubscribed", chat_id=chat_id, title=title, reason="blocked")
            _notify_owner(api, root_chat_id, chat_id, f"🚫 Бот удалён или заблокирован: {title}")
    elif chat.get("type") == "channel" and status == "administrator":
        with engine.begin() as conn:
            new = subs.subscribe(conn, chat_id, "channel", title, now)
        if new:
            log.info("telegram_subscribed", chat_id=chat_id, title=title)
            _notify_owner(api, root_chat_id, chat_id, f"🆕 Подключён канал: {title}")


def _reply(api: Api, chat_id: int, text: str, html: bool = False) -> None:
    params = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if html:
        params["parse_mode"] = "HTML"
    api("sendMessage", **params)


def _notify_owner(api: Api, root_chat_id: int | None, chat_id: int, text: str) -> None:
    if root_chat_id is None or chat_id == root_chat_id:
        return
    try:
        _reply(api, root_chat_id, f"{text} (chat_id {chat_id})")
    except Exception:
        log.exception("telegram_owner_notify_failed")


def setup_bot(api: Api) -> str | None:
    """Registers the command menu; returns the bot's username (for /cmd@bot parsing)."""
    me = api("getMe")
    try:
        api("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
    except TelegramError:
        log.exception("telegram_set_commands_failed")  # cosmetic
    return me.get("username")


def poll_forever(
    engine: sa.Engine,
    api: Api,
    *,
    root_chat_id: int | None,
    stop: threading.Event,
    now: Callable[[], datetime],
) -> None:
    """Long-polling loop. One bad update or a Telegram outage never ends it."""
    bot_username = None
    offset = None
    while not stop.is_set():
        try:
            if bot_username is None:
                bot_username = setup_bot(api)
                log.info("telegram_bot_started", username=bot_username)
            updates = api(
                "getUpdates",
                offset=offset,
                timeout=POLL_TIMEOUT,
                allowed_updates=["message", "my_chat_member"],
                http_timeout=POLL_TIMEOUT + 10,
            )
        except TelegramError as exc:
            if exc.error_code == 409:
                log.error("telegram_polling_conflict", hint="another process polls this bot token")
            else:
                log.exception("telegram_get_updates_failed")
            stop.wait(15)
            continue
        except Exception:
            log.exception("telegram_get_updates_failed")
            stop.wait(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            try:
                handle_update(
                    engine,
                    api,
                    update,
                    root_chat_id=root_chat_id,
                    bot_username=bot_username,
                    now=now,
                )
            except Exception:
                log.exception("telegram_update_failed", update_id=update.get("update_id"))
    log.info("telegram_bot_stopped")


def start_in_thread(engine, api, *, root_chat_id, now) -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()
    thread = threading.Thread(
        target=poll_forever,
        kwargs={
            "engine": engine,
            "api": api,
            "root_chat_id": root_chat_id,
            "stop": stop,
            "now": now,
        },
        name="telegram-bot",
        daemon=True,  # an in-flight long poll must not block process exit
    )
    thread.start()
    return thread, stop
