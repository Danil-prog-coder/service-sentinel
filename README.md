# service-sentinel

Лёгкий сервис мониторинга сайтов и API с уведомлениями в Telegram.

## 1. Что делает проект

- Каждые `interval` (по умолчанию 10 минут) параллельно проверяет список URL (обычно `GET /health`).
- Решает, работает ли сервис, **только по HTTP-статусу**. Тело ответа не разбирается.
- Защищает от ложных срабатываний: сервис становится `DOWN` только после `failure_threshold`
  неудачных проверок подряд, а снова `UP` — после `recovery_threshold` успешных.
- Пишет в Telegram **только при смене состояния**: `UP → DOWN` и `DOWN → UP`.
  Если сайт лежит 3 часа, придёт одно сообщение о падении и одно о восстановлении.
- Хранит состояние в SQLite, поэтому после перезапуска нет ни повторных алертов, ни потерянных.
- Пишет structured-логи (JSON) и маскирует секреты.
- Архитектура заранее готова к мониторингу серверов (CPU/RAM/Disk) — это второй этап.

| Результат проверки | Что происходит |
|---|---|
| `2xx` | успех |
| `3xx` | redirect выполняется, решение принимается по конечному ответу |
| `4xx` | **без алерта**: пишется warning в лог, состояние сервиса не меняется |
| `5xx` (в т.ч. 502/503/504 от nginx) | неудача |
| timeout, connection refused/reset, DNS, TLS/SSL, обрыв соединения, бесконечные redirect | неудача |

> Почему 4xx «нейтральный»: сервер отвечает, значит, скорее всего, ошибка в URL health-endpoint
> или в авторизации, а не падение. Учтите: если сервис в `DOWN` и после починки начал отдавать 4xx,
> он останется в `DOWN`, пока не вернёт 2xx.

## 2. Архитектура

```
            config.yaml (сайты, настройки)      .env (секреты)
                          │                        │
                          ▼                        ▼
                 main.py — CLI, сигналы, закрытие ресурсов
                          │
                 MonitorService — цикл раз в interval
          asyncio.TaskGroup + Semaphore(max_concurrency)
         ┌────────────┬───────────────┬────────────────┐
         ▼            ▼               ▼                ▼
    HttpChecker   state_machine    StateStore       heartbeat
    (httpx,       UP/DOWN, пороги, (SQLite,         (для Docker
    общий клиент) решение об алерте aiosqlite)       healthcheck)
                          │
                          ▼
                 Notifier ─── TelegramClient (Bot API через httpx)
                          └── DryRunNotifier (только лог)

    metrics/ (этап 2): MetricsProvider Protocol → Zabbix / свой agent / SSH / Timeweb API
```

**Как устроена защита от дублей.** У каждого сервиса хранится `notified_status` — статус, о котором
вы уже получили сообщение. Алерт уходит, только если текущий статус с ним не совпадает. Поэтому:
- при длительном падении нет повторов;
- если Telegram был недоступен, отправка повторится в следующем цикле;
- если процесс упал между сменой статуса и отправкой, алерт уйдёт после рестарта;
- после рестарта дубля не будет.

### Структура

```
src/monitor/
  main.py                    точка входа, CLI, graceful shutdown
  config.py                  YAML + ENV, валидация (pydantic)
  log.py                     JSON/text-логи, маскирование секретов
  healthcheck.py             heartbeat-файл и проверка для Docker
  checker/http.py            HTTP-проверка и классификация ошибок
  monitoring/state_machine.py  логика UP/DOWN (чистые функции)
  monitoring/service.py      цикл мониторинга
  telegram/client.py         Bot API sendMessage с ретраями, Notifier Protocol
  telegram/formatting.py     тексты сообщений
  storage/sqlite.py          сохранение состояния
  metrics/base.py            Server, Metrics, MetricsProvider (этап 2)
  metrics/thresholds.py      пороги RAM/Disk/CPU (этап 2)
  models/                    CheckResult, ServiceState, Status
tests/                       pytest, без реальной сети и Telegram
```

**Зависимости:** `httpx`, `PyYAML`, `aiosqlite`, `pydantic` (только для валидации конфига) и
`tzdata` (часовые пояса на Windows и в slim-образах).

**Почему нет HTTP-сервера / FastAPI.** Сервису не нужен входящий API. Здоровье контейнера
проверяется через heartbeat-файл: он обновляется после каждого цикла, а
`python -m monitor.healthcheck` проверяет, что файл не устарел (не старше `2 × interval + 120s`).
Это надёжнее простого `/health`: зависший цикл тоже будет замечен.

## 3. Как создать Telegram-бота через BotFather

1. Откройте в Telegram [@BotFather](https://t.me/BotFather) (проверьте синюю галочку).
2. Отправьте `/newbot`.
3. Введите отображаемое имя, например `My Sentinel`.
4. Введите username, который должен заканчиваться на `bot`, например `my_sentinel_bot`.

## 4. Как получить TELEGRAM_BOT_TOKEN

BotFather в ответ пришлёт токен вида `123456789:AAH...`. Это и есть `TELEGRAM_BOT_TOKEN`.
Положите его в `.env`. Если токен утёк, выпустите новый: `/revoke` в BotFather.

## 5. Как создать приватную группу с ботом

1. В Telegram создайте группу (New Group) и добавьте в неё бота по username.
2. Бота не обязательно делать администратором: для отправки сообщений достаточно быть участником.
3. Отправьте в группу любую команду, например `/start@my_sentinel_bot`. По умолчанию у бота
   включён privacy mode, и он видит только команды и упоминания.

## 6. Как узнать TELEGRAM_CHAT_ID

**Вариант А — встроенная команда** (после шага 5, токен уже в `.env`):

```bash
python -m monitor --telegram-chats
```

Команда выведет строки вида `TELEGRAM_CHAT_ID=-1001234567890    # supergroup: My Alerts`.

**Вариант Б — вручную.** Откройте в браузере
`https://api.telegram.org/bot<TOKEN>/getUpdates` и найдите `"chat":{"id":-100...}`.

Замечания:
- у групп ID отрицательный (`-100...` у супергрупп);
- если группа была преобразована в супергруппу, ID меняется — узнайте его заново;
- если `getUpdates` пустой, отправьте в группу ещё одно сообщение-команду и повторите.

Проверка, что всё настроено:

```bash
python -m monitor --test-telegram
```

## 7. Как добавить новый сайт

Добавьте запись в `config.yaml`:

```yaml
services:
  - name: "My Shop"                       # уникальное имя (ключ состояния в БД)
    url: "https://shop.example.ru"        # показывается в алертах
    health_url: "https://shop.example.ru/health"  # необязательно: что реально проверять
    enabled: true
    timeout: 10s                          # необязательно, иначе monitor.request_timeout
    failure_threshold: 3                  # необязательно
    recovery_threshold: 1                 # необязательно
    follow_redirects: true                # по умолчанию true
```

Затем проверьте конфиг и перезапустите сервис:

```bash
python -m monitor --check-config
docker compose restart monitor
```

Если у сервиса поменялся проверяемый URL, его состояние сбрасывается в `UNKNOWN`.
Чтобы временно отключить проверку, поставьте `enabled: false`.

## 8. Как настроить /health endpoint

Монитор смотрит только на HTTP-статус: `200` — всё хорошо, `5xx` (обычно `503`) — приложение
неработоспособно. Endpoint должен быть быстрым, без авторизации и без тяжёлых операций.

FastAPI:

```python
from fastapi import FastAPI, Response

app = FastAPI()


@app.get("/health")
async def health(response: Response):
    try:
        await db.execute("SELECT 1")  # только критичные зависимости
    except Exception:
        response.status_code = 503
        return {"status": "unhealthy"}
    return {"status": "ok"}
```

Flask:

```python
@app.get("/health")
def health():
    return {"status": "ok"}, 200
```

nginx (если приложение за reverse proxy): пробросьте `/health` в приложение. Если контейнер
приложения упал, nginx сам вернёт `502/504`, и монитор это поймает.

```nginx
location = /health {
    proxy_pass http://app:8000/health;
    access_log off;
}
```

## 9. Как запустить локально

Нужен Python 3.12+.

```bash
python -m venv .venv
```

```bash
.venv/bin/pip install -e ".[dev]"
```

(на Windows: `.venv\Scripts\pip install -e ".[dev]"`)

```bash
cp config.example.yaml config.yaml
```

```bash
cp .env.example .env
```

Заполните `.env` и URL в `config.yaml`, затем:

```bash
python -m monitor --check-config
```

```bash
python -m monitor --once --dry-run
```

```bash
python -m monitor
```

`.env` из текущей директории подхватывается автоматически, уже заданные переменные окружения
не перезаписываются.

Опции CLI:

| Опция | Что делает |
|---|---|
| `-c/--config PATH` | путь к YAML (иначе `CONFIG_PATH` или `config.yaml`) |
| `--once` | один цикл проверок и выход |
| `--dry-run` | алерты пишутся в лог, в Telegram не отправляются (токен не нужен) |
| `--check-config` | только проверить конфиг |
| `--test-telegram` | отправить тестовое сообщение |
| `--telegram-chats` | показать чаты, которые видел бот (для `TELEGRAM_CHAT_ID`) |
| `--env-file PATH` | другой `.env` |

Тесты и проверки:

```bash
pytest
```

```bash
ruff check . && ruff format --check . && mypy
```

## 10. Как запустить через Docker Compose

```bash
cp config.example.yaml config.yaml
```

```bash
cp .env.example .env
```

Заполните оба файла, затем:

```bash
docker compose up -d --build
```

```bash
docker compose ps
```

В `docker-compose.yml` уже настроены:
- `restart: unless-stopped`;
- запуск не от root (uid 10001);
- read-only файловая система, `cap_drop: ALL`, `no-new-privileges`;
- healthcheck;
- ротация логов;
- volume для БД.

`config.yaml` монтируется read-only. После его изменения выполните
`docker compose restart monitor`. Обновление кода: `git pull && docker compose up -d --build`.

Разовая проверка Telegram внутри контейнера:

```bash
docker compose run --rm monitor python -m monitor --test-telegram
```

## 11. Как посмотреть логи

```bash
docker compose logs -f monitor
```

Логи пишутся в JSON, по одной строке на событие. Например, только проблемы:

```bash
docker compose logs monitor | grep -E '"level": "(WARNING|ERROR)"'
```

Для удобного чтения при локальном запуске задайте `LOG_FORMAT=text`, для подробностей —
`LOG_LEVEL=DEBUG`.

Что логируется:
- старт с параметрами;
- каждая проверка (`status_code`, `response_time`, `error`);
- 4xx;
- 5xx;
- сетевые ошибки;
- смена статуса;
- recovery;
- отправка и ошибки Telegram;
- внутренние ошибки;
- shutdown.

## 12. Где хранится SQLite database

| Запуск | Путь |
|---|---|
| локально | `data/monitor.db` (или `DB_PATH`) |
| Docker | `/app/data/monitor.db` в named volume `monitor-data` |

Файл переживает `docker compose down` и пересоздание контейнера. Удаляется только через
`docker compose down -v`: после этого все сервисы стартуют в `UNKNOWN`.

Посмотреть состояние:

```bash
docker compose exec monitor python -c "import sqlite3; [print(r) for r in sqlite3.connect('/app/data/monitor.db').execute('select name, status, failure_count, down_since, last_error from service_state')]"
```

Бэкап:

```bash
docker compose cp monitor:/app/data/monitor.db ./monitor-backup.db
```

## 13. Как добавить новый server metrics provider (этап 2)

HTTP-мониторинг не зависит от метрик. Контракт описан в `src/monitor/metrics/base.py`:

```python
class MetricsProvider(Protocol):
    async def get_metrics(self, server: Server) -> Metrics: ...
```

`Metrics` содержит `cpu_percent`, `ram_percent`, `ram_available_bytes`, `disk_percent`,
`disk_available_bytes`, `load_average`, `uptime_seconds` и `containers`. Все поля необязательные:
провайдер заполняет то, что умеет получить.

Пример каркаса провайдера:

```python
# src/monitor/metrics/zabbix.py
class ZabbixProvider:
    def __init__(self, client: httpx.AsyncClient, url: str, token: str) -> None: ...

    async def get_metrics(self, server: Server) -> Metrics:
        # Zabbix JSON-RPC API: item.get по host + нужным item key
        # (например, system.cpu.util, vm.memory.utilization, vfs.fs.size[/,pused])
        ...
        return Metrics(collected_at=datetime.now(UTC), cpu_percent=..., ram_percent=...)
```

Что останется сделать на втором этапе:
1. Добавить в конфиг секцию `servers:` с порогами из `ResourceThresholds`.
2. Запустить отдельный цикл, аналогичный `MonitorService`: `get_metrics` →
   `find_breaches()` (`metrics/thresholds.py`) → превышение считается «неудачей» для той же
   логики порогов. «CPU > 90% в течение N минут» = `consecutive_checks_required(N, interval)`
   неудач подряд.
3. Хранить состояние сервера так же, как сервиса (отдельная таблица), с тем же принципом
   `notified_status`. Сообщения уже подготовлены: `format_server_warning` и
   `format_server_recovered`.

Варианты провайдеров:
- **Zabbix.** Если на серверах Timeweb стоит агент и у вас есть доступ к Zabbix API.
- **Timeweb API.** Использовать, только если в официальной документации Timeweb Cloud API
  действительно есть нужные метрики. В этом проекте Timeweb API не реализован и ничего о нём
  не предполагается.
- **Свой agent.** Маленький HTTP-endpoint на сервере (например, на `psutil`), закрытый токеном
  и firewall.
- **SSH (asyncssh).** Отдельный read-only пользователь без sudo, вход только по ключу
  (`PasswordAuthentication no`), по возможности `command=`-ограничение в `authorized_keys`.
  Приватный ключ монтируется в контейнер как Docker secret или read-only файл и не хранится
  в Git. Пароли root в конфиге недопустимы. Зависимость `asyncssh` добавляется только вместе
  с провайдером.

## 14. Какие секреты нельзя коммитить в Git

- `.env`, в том числе `TELEGRAM_BOT_TOKEN` (`TELEGRAM_CHAT_ID` тоже лучше не публиковать);
- SSH private keys (`id_rsa`, `id_ed25519`, `*.pem`, `*.key`);
- пароли, API-токены (Timeweb, Zabbix и др.);
- `config.yaml`, если в URL есть приватные адреса или токены в query string;
- `data/` и `*.db` (состояние, URL, тексты ошибок).

Всё перечисленное уже есть в `.gitignore` и `.dockerignore`. Логи маскируют токен бота, в том числе
внутри URL Bot API. Если секрет всё же попал в Git, считайте его скомпрометированным и
перевыпустите: удалить его из истории недостаточно.

## Переменные окружения

| Переменная | По умолчанию | Описание |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | токен бота (**обязательно**, кроме `--dry-run`) |
| `TELEGRAM_CHAT_ID` | — | ID чата/группы (**обязательно**, кроме `--dry-run`) |
| `CONFIG_PATH` | `config.yaml` | путь к YAML |
| `DB_PATH` | `data/monitor.db` | SQLite |
| `HEARTBEAT_PATH` | `data/heartbeat.json` | heartbeat для healthcheck |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `LOG_FORMAT` | `json` | `json` или `text` |
