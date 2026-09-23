# trade-news

Сборщик фундаментальных данных по акциям США, крипте и форексу. Размечает новости через LLM
и отправляет отобранное в Telegram. Техническое задание: [tz-news-aggregator.md](tz-news-aggregator.md).

**Статус:** этап 1 (схема БД, коллекторы SEC EDGAR и Finnhub, дедупликация) и подписка в Telegram.

## Быстрый старт

```bash
uv sync
cp .env.example .env              # заполнить ключи, см. ниже
uv run trade-news db-upgrade      # создать или обновить схему (миграции Alembic)
uv run trade-news sources         # какие источники есть и каких ключей не хватает
uv run trade-news collect         # один проход по всем включённым источникам
uv run trade-news collect sec_edgar
uv run trade-news run             # работа по расписанию (блокирующий режим)
uv run trade-news stats --hours 24
```

Флаг `--pretty` выводит логи в читаемом виде вместо JSON.

`.env`, `config.yaml` и `data/` ищутся в текущей рабочей папке.

Тесты: `uv run pytest`. Линтер: `uv run ruff check . && uv run ruff format --check .`

## Запуск на сервере (systemd)

Юнит: `trade-news.service`. Перед стартом он применяет миграции (`ExecStartPre=... db-upgrade`),
затем запускает `trade-news run`.

1. Склонировать репозиторий в `/opt/trade-news`, выполнить `uv sync --no-dev`, создать `.env`
   по образцу `.env.example`.
2. В юните указан `User=crip`. Если сервис будет работать под другим пользователем, поправить
   `User=`. Если путь не `/opt/trade-news`, поправить `WorkingDirectory=` и `ExecStart*=`.
   Пользователь должен иметь право на запись в `/opt/trade-news/data`.
3. Установить и запустить:
   ```
   sudo cp trade-news.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now trade-news
   ```
4. Логи: `journalctl -u trade-news -f`. Логи в JSON, удобно фильтровать через `jq`.
   Статистика: `cd /opt/trade-news && .venv/bin/trade-news stats`.

Остановка корректная. `systemctl stop` шлёт SIGTERM, и планировщик ждёт, пока текущие сборщики
допишут свою транзакцию: EDGAR иногда отвечает 20–30 секунд, поэтому `TimeoutStopSec=120`.
Запуски, оборванные убийством процесса, при следующем старте помечаются в `collector_runs`
как `aborted`. После падения сервис перезапускается сам (`Restart=on-failure`).

## Ключи

Все секреты задаются только через переменные окружения (`.env`). Полный список в `.env.example`.

| Переменная | Где получить |
|---|---|
| `SEC_USER_AGENT` | Ключ не нужен. SEC требует User-Agent вида `Name email@domain` ([fair access](https://www.sec.gov/os/accessing-edgar-data)) |
| `FINNHUB_API_KEY` | Бесплатная регистрация на https://finnhub.io/register, ключ появится в дашборде |
| `GEMINI_API_KEY` | https://aistudio.google.com/app/apikey (нужен с этапа 3) |
| `TELEGRAM_BOT_TOKEN` | @BotFather → `/newbot`. Нужен **отдельный** бот, см. раздел Telegram |
| `TELEGRAM_CHAT_ID` | Ваш chat id (владелец). Его можно узнать у @userinfobot |

Источник, которому не хватает ключа, пропускается с предупреждением в логе. Остальные продолжают работать.

## Telegram: подписка

Бот запускается внутри `trade-news run` отдельным потоком, если задан `TELEGRAM_BOT_TOKEN`.

| Команда | Что делает |
|---|---|
| `/start` | подписывает чат сразу, без подтверждения; работает в личке и в группе (`/start@bot`) |
| `/stop` | отписывает |
| `/help` | описание бота и статус подписки |

- **Каналы.** В канале нельзя отправить `/start`, поэтому канал подписывается, когда бота
  добавляют в него администратором.
- **Блокировка.** Если бота заблокировали или удалили из группы, чат отписывается автоматически.
- **Владелец** (`TELEGRAM_CHAT_ID`) получает рассылку всегда, даже без подписки, и видит
  уведомление, когда кто-то подписался или отписался. Других ролей нет.
- **Хранение.** Подписчики лежат в таблице `subscribers`. При отписке строка не удаляется,
  только деактивируется. Список: `trade-news subscribers`.

**Нужен отдельный бот.** Telegram разрешает только одному процессу опрашивать `getUpdates`
на один токен, второй получает 409 Conflict. Если дать trade-news токен бота ict-monitor,
приложения начнут перехватывать команды друг у друга. При конфликте в логе появляется
`telegram_polling_conflict`.

Сама рассылка новостей подписчикам появится на этапе 4, после LLM-разметки.

## Как это устроено

```
collector (функция) → Batch(items, cursor) → raw_items → items + dedup_group_id
```

- **Коллектор** — чистая функция `fetch(ctx, cursor) -> Batch`. В базу он не ходит:
  получает свой прошлый курсор и возвращает записи и новый курсор. Пайплайн пишет их
  одной транзакцией. Поэтому после сбоя ничего не теряется и ничего не пропускается.
- **Изоляция.** Каждый источник — отдельная задача APScheduler в своём потоке. Исключение
  логируется и пишется в `collector_runs` со статусом `error`. Остальные источники это не задевает.
- **Лимиты запросов.** Лимиты именованные (`rate_limits` в `config.yaml`), несколько источников
  могут делить один. Например, все запросы к SEC идут через ключ `sec`. Ретраи через tenacity:
  экспоненциальная пауза с джиттером на 429, 5xx и сетевые ошибки. `Retry-After` учитывается.
- **Сырьё не теряется.** `raw_items` уникален по `(source, source_item_id, content_hash)`.
  Повторная выборка ничего не дублирует, а изменённая ревизия той же новости сохраняется отдельной строкой.
- **Время.** В БД всё хранится в UTC. Тип `UTCDateTime` не принимает naive datetime.
  Для бэктеста используется `fetched_at`.

### Дедупликация

Новая запись сравнивается с записями в окне ±`dedup.window_hours` от её `published_at`.
Проверки идут по порядку:

1. нормализованный URL: без `www`, utm- и других трекинговых параметров, фрагмента,
   завершающего слеша, с отсортированным query;
2. точный хэш нормализованного заголовка;
3. нечёткое совпадение заголовка: rapidfuzz `token_sort_ratio` ≥ `dedup.fuzzy_threshold`,
   заголовки короче `dedup.min_title_len` не сравниваются.

Оригиналы не удаляются. Дубликат получает `dedup_group_id`, равный id первой записи группы,
а в `dedup_reason` пишется причина: `url`, `title_hash` или `fuzzy`.

Источники с шаблонными заголовками (EDGAR: `8-K - Apple Inc. (...)`) регистрируются
с `title_dedup=False`. Они дедуплицируются только по id и URL и не участвуют в сравнении
заголовков, иначе две разные подачи одной компании склеились бы.

### SQLite → PostgreSQL

Схема (`trade_news/db/schema.py`, SQLAlchemy Core) переносима. Enum хранятся как text с CHECK,
JSON на PostgreSQL становится JSONB, bigint-ключи на SQLite становятся INTEGER, частичный индекс
объявлен для обоих диалектов. Для перехода достаточно
`DATABASE_URL=postgresql+psycopg://...`, установить `psycopg[binary]` и выполнить
`trade-news db-upgrade`. На SQLite включены WAL и `busy_timeout`. Все записи идут через одну
блокировку процесса, потому что у SQLite один писатель.

Новая миграция после изменения схемы:
`uv run alembic revision --autogenerate -m "..."`. Сгенерированный файл нужно проверить глазами.

## Как добавить источник

1. Создать `trade_news/collectors/<name>.py`:

   ```python
   from trade_news.collectors.base import Batch, Context, RawItem, collector


   @collector("my_source", secrets=("MY_API_KEY",))
   def fetch(ctx: Context, cursor: dict | None) -> Batch:
       resp = ctx.get(
           "https://api.example.com/news",
           params={"since": (cursor or {}).get("since")},
           headers={"Authorization": ctx.secrets["MY_API_KEY"]},
       )
       items = [
           RawItem(
               source_item_id=str(n["id"]),
               title=n["title"],
               body=n.get("text"),
               url=n["url"],
               published_at=...,
               raw=n,
           )
           for n in resp.json()
       ]
       return Batch(items, cursor={"since": ...})
   ```

   `ctx.get` уже учитывает лимит и делает ретраи. `ctx.params` берётся из `config.yaml`,
   `ctx.secrets` содержит только объявленные переменные окружения. `published_at` должен быть tz-aware.
2. Добавить секцию в `config.yaml` → `sources.my_source` (`interval_seconds`, `rate_limit`, `params`)
   и при необходимости лимит в `rate_limits`.
3. Добавить ключ в `.env.example`.
4. Написать тест парсинга на сохранённом реальном ответе (`tests/fixtures/`) с фейковым `ctx.get`.
   Примеры в `tests/test_sec_edgar.py`.

Модули из `trade_news/collectors/` импортируются автоматически, регистрировать их где-то ещё не нужно.

## Источники этапа 1 и проверка документации

Проверено 2026-09-23 по официальной документации и живым запросам.

- **SEC EDGAR**, Atom-лента `browse-edgar?action=getcurrent`. Не больше 10 запросов в секунду
  на пользователя суммарно со всех машин, у нас стоит 5. User-Agent обязателен.
  Отличие от ожиданий: параметр `type` фильтрует **по префиксу**. `type=4` возвращает
  в основном 424B2, 485BPOS и т.п., поэтому формы отфильтровываются точно (`4` и `4/A`).
  Form 4 приходит двумя записями (Issuer и Reporting) с одним accession, мы оставляем Issuer.
  Лента иногда отвечает 10–20 секунд, это задержка на стороне SEC.
- **Finnhub**: `/news?category=…&minId=` для инкрементальной выборки и `/company-news` по списку
  тикеров. Бесплатный план: 60 запросов в минуту, у нас стоит 45. `/company-news` отдаёт
  историю за год и только по компаниям Северной Америки. Ключ передаётся заголовком
  `X-Finnhub-Token`, чтобы не попадал в URL и логи.

## Решения, зафиксированные по ходу

- SQLite вместо PostgreSQL на старте, схема переносима (см. выше).
- Запуск через systemd, а не через docker-compose (ТЗ, раздел 8), по решению заказчика.
- Telegram (ТЗ, раздел 6): интерактивная подписка `/start` и `/stop` без подтверждения и без ролей,
  владелец `TELEGRAM_CHAT_ID` получает рассылку всегда. Библиотеку для Bot API не брал:
  нужны три метода, они сделаны на httpx (`trade_news/telegram/api.py`).
- Gemini (этап 3): только `gemini-3.5-flash-lite` и `gemini-3.1-flash-lite`. Ключ общий
  с ict-monitor, а квоты Gemini считаются на проект, поэтому оба приложения делят дневной
  лимит. Локальные потолки в `config.yaml → llm` поставлены консервативно.
