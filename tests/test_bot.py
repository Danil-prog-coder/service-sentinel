"""The Telegram control bot against a mocked Bot API (MockTransport, no network)."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from conftest import CHAT_ID, BotApi, ScriptedSites
from monitor.checker import HttpChecker
from monitor.config import MonitorSettings, ServiceConfig
from monitor.models import Status
from monitor.monitoring import MonitorService
from monitor.registry import ServiceRegistry
from monitor.storage import StateStore
from monitor.telegram import ui
from monitor.telegram.bot import TelegramBot
from monitor.telegram.client import TelegramClient

CONFIG_SITE = "https://cfg.test/health"
SETTINGS = MonitorSettings(interval=600, failure_threshold=1, recovery_threshold=1)
CONFIG_SERVICES = [
    ServiceConfig(name="Config site", url="https://cfg.test", health_url=CONFIG_SITE)
]


@pytest.fixture
async def monitor(
    site_client: httpx.AsyncClient, bot_client: TelegramClient, db_path: Path
) -> AsyncIterator[MonitorService]:
    async with StateStore(db_path) as store:
        service = MonitorService(
            SETTINGS,
            ServiceRegistry(store, CONFIG_SERVICES),
            HttpChecker(site_client),
            store,
            bot_client,
        )
        await service.load_state()
        yield service


@pytest.fixture
def bot(bot_client: TelegramClient, monitor: MonitorService) -> TelegramBot:
    return TelegramBot(bot_client, monitor, CHAT_ID)


def message(text: str, *, chat_id: str = CHAT_ID, reply_to: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "message": {
            "message_id": 1,
            "chat": {"id": int(chat_id), "type": "supergroup"},
            "text": text,
        }
    }
    if reply_to is not None:
        payload["message"]["reply_to_message"] = {"message_id": reply_to}
    return payload


def press(data: str, *, chat_id: str = CHAT_ID, message_id: int = 500) -> dict[str, Any]:
    return {
        "callback_query": {
            "id": "cb-1",
            "data": data,
            "message": {
                "message_id": message_id,
                "chat": {"id": int(chat_id), "type": "supergroup"},
            },
        }
    }


async def add_site(bot: TelegramBot, url: str, name: str = "") -> None:
    await bot.handle_update(message(f"/add {url} {name}".strip()))


async def run_briefly(bot: TelegramBot, bot_api: BotApi, *updates: dict[str, Any]) -> None:
    """Run the polling loop, deliver ``updates`` to it and stop it again."""
    stop = asyncio.Event()
    task = asyncio.create_task(bot.run(stop))
    await asyncio.sleep(0.05)  # let start() finish before anything is queued
    for update in updates:
        bot_api.queue(update)
    await asyncio.sleep(0.15)
    stop.set()
    await asyncio.wait_for(task, timeout=2)


# --- menu and access ---------------------------------------------------------------


async def test_start_shows_main_menu(bot: TelegramBot, bot_api: BotApi) -> None:
    await bot.handle_update(message("/start"))
    assert "service-sentinel" in bot_api.last_text
    assert bot_api.buttons() == ["📋 Сайты", "➕ Добавить", "🔄 Проверить все", "❓ Помощь"]


async def test_menu_command_with_bot_username(bot: TelegramBot, bot_api: BotApi) -> None:
    await bot.handle_update(message("/menu@my_sentinel_bot"))
    assert bot_api.buttons()[0] == "📋 Сайты"


async def test_other_chats_are_ignored(bot: TelegramBot, bot_api: BotApi) -> None:
    await bot.handle_update(message("/start", chat_id="-100999"))
    await bot.handle_update(press(ui.CB_LIST, chat_id="-100999"))
    assert bot_api.sent == []
    assert bot_api.edits == []


async def test_unknown_command_is_answered(bot: TelegramBot, bot_api: BotApi) -> None:
    await bot.handle_update(message("/drop_table"))
    assert "Не знаю такую команду" in bot_api.last_text


# --- list and card -----------------------------------------------------------------


async def test_list_shows_statuses(
    bot: TelegramBot, bot_api: BotApi, sites: ScriptedSites, monitor: MonitorService
) -> None:
    sites.set(CONFIG_SITE, 200)
    sites.set("https://down.test", 503)
    sites.set("https://paused.test", 200)
    await add_site(bot, "https://down.test", "Упавший")
    await add_site(bot, "https://paused.test", "На паузе")
    await monitor.set_enabled("На паузе", False)
    await monitor.run_cycle()
    bot_api.sent.clear()

    await bot.handle_update(message("/list"))
    text = bot_api.last_text
    assert "📋 Сайты (3)" in text
    assert "🟢 Config site 📄 — работает" in text
    assert "🔴 Упавший — не работает" in text
    assert "⏸ На паузе — на паузе" in text
    assert bot_api.buttons()[:3] == ["🟢 Config site", "🔴 Упавший", "⏸ На паузе"]


async def test_status_command_works_like_list(bot: TelegramBot, bot_api: BotApi) -> None:
    await bot.handle_update(message("/status"))
    assert "Сайтов пока нет" in bot_api.last_text or "📋 Сайты" in bot_api.last_text


async def test_card_shows_details(
    bot: TelegramBot, bot_api: BotApi, sites: ScriptedSites, monitor: MonitorService
) -> None:
    sites.set(CONFIG_SITE, 200)
    sites.set("https://shop.test", 502)
    await add_site(bot, "https://shop.test", "Магазин")
    await monitor.run_cycle()
    bot_api.sent.clear()

    await bot.handle_update(message("/list"))
    card_data = bot_api.callback_data("🔴 Магазин")
    await bot.handle_update(press(card_data))

    text = bot_api.last_text
    assert text.startswith("🔴 Магазин")
    assert "Ссылка: https://shop.test" in text
    assert "Статус: 🔴 не работает" in text
    assert "С: " in text and "MSK" in text
    assert "Последний код: 502 Bad Gateway" in text
    assert "Время ответа: " in text
    assert "Ошибка: HTTP 502" in text
    assert bot_api.buttons() == ["🔄 Проверить", "⏸ Пауза", "🗑 Удалить", "« Назад"]


async def test_config_site_card_has_no_pause_or_delete(
    bot: TelegramBot, bot_api: BotApi, monitor: MonitorService
) -> None:
    service = monitor.registry.get("Config site")
    assert service is not None
    await bot.handle_update(press(ui.CB_CARD + service.id))
    assert bot_api.buttons() == ["🔄 Проверить", "« Назад"]
    assert "config.yaml" in bot_api.last_text

    # Even a stale keyboard cannot pause or delete it.
    await bot.handle_update(press(ui.CB_TOGGLE + service.id))
    await bot.handle_update(press(ui.CB_DELETE + service.id))
    assert all("config.yaml" in answer["text"] for answer in bot_api.answers if answer.get("text"))
    assert monitor.registry.get("Config site") is not None


# --- adding ------------------------------------------------------------------------


async def test_add_button_uses_force_reply(
    bot: TelegramBot, bot_api: BotApi, sites: ScriptedSites
) -> None:
    sites.set("https://new.test", 200)
    await bot.handle_update(press(ui.CB_ADD))
    prompt = bot_api.sent[-1]
    assert prompt["reply_markup"]["force_reply"] is True
    prompt_id = 101  # BotApi message ids start at 101

    await bot.handle_update(message("https://new.test Новый сайт", reply_to=prompt_id))
    assert "✅ Сайт добавлен." in bot_api.last_text
    assert "🟢 Новый сайт" in bot_api.last_text
    assert "https://new.test" in sites.calls  # checked right away


async def test_reply_to_a_foreign_message_is_not_a_url(bot: TelegramBot, bot_api: BotApi) -> None:
    await bot.handle_update(message("https://new.test", reply_to=999))
    assert bot_api.sent == []


async def test_add_command_takes_name_from_domain(
    bot: TelegramBot, bot_api: BotApi, sites: ScriptedSites, monitor: MonitorService
) -> None:
    sites.set("https://shop.test", 200)
    await add_site(bot, "shop.test")
    assert "🟢 shop.test" in bot_api.last_text
    service = monitor.registry.get("shop.test")
    assert service is not None and service.url == "https://shop.test"
    assert monitor.states["shop.test"].status is Status.UP


async def test_add_command_without_url_asks_for_one(bot: TelegramBot, bot_api: BotApi) -> None:
    await bot.handle_update(message("/add"))
    assert bot_api.sent[-1]["reply_markup"]["force_reply"] is True


async def test_add_rejects_garbage(bot: TelegramBot, bot_api: BotApi) -> None:
    await bot.handle_update(message("/add not a url"))
    assert bot_api.last_text.startswith("⚠️")


async def test_added_site_is_monitored_after_restart(
    bot: TelegramBot,
    sites: ScriptedSites,
    monitor: MonitorService,
    db_path: Path,
    site_client: httpx.AsyncClient,
    bot_client: TelegramClient,
) -> None:
    sites.set(CONFIG_SITE, 200)
    sites.set("https://kept.test", 200)
    await add_site(bot, "https://kept.test", "Сохранён")

    async with StateStore(db_path) as store:
        restarted = MonitorService(
            SETTINGS,
            ServiceRegistry(store, CONFIG_SERVICES),
            HttpChecker(site_client),
            store,
            bot_client,
        )
        await restarted.load_state()
        assert [s.name for s in restarted.registry.enabled()] == ["Config site", "Сохранён"]
        await restarted.run_cycle()
    assert restarted.states["Сохранён"].status is Status.UP


# --- check, pause, delete ----------------------------------------------------------


async def test_manual_check_updates_card(
    bot: TelegramBot, bot_api: BotApi, sites: ScriptedSites, monitor: MonitorService
) -> None:
    sites.set("https://shop.test", 500, 200)
    await add_site(bot, "https://shop.test", "Магазин")
    service = monitor.registry.get("Магазин")
    assert service is not None
    assert monitor.states["Магазин"].status is Status.DOWN

    await bot.handle_update(press(ui.CB_CHECK + service.id))
    assert monitor.states["Магазин"].status is Status.UP
    assert bot_api.last_text.startswith("🟢 Магазин")


async def test_check_all_button_runs_a_cycle(
    bot: TelegramBot, bot_api: BotApi, sites: ScriptedSites
) -> None:
    sites.set(CONFIG_SITE, 200)
    await bot.handle_update(press(ui.CB_CHECK_ALL))
    assert CONFIG_SITE in sites.calls
    assert "📋 Сайты" in bot_api.last_text


async def test_pause_and_resume(
    bot: TelegramBot, bot_api: BotApi, sites: ScriptedSites, monitor: MonitorService
) -> None:
    sites.set(CONFIG_SITE, 200)
    sites.set("https://shop.test", 200)
    await add_site(bot, "https://shop.test", "Магазин")
    service = monitor.registry.get("Магазин")
    assert service is not None

    await bot.handle_update(press(ui.CB_TOGGLE + service.id))
    assert "⏸ Магазин" in bot_api.last_text
    assert "▶️ Включить" in bot_api.buttons()
    assert [s.name for s in monitor.registry.enabled()] == ["Config site"]

    sites.calls.clear()
    await monitor.run_cycle()
    assert "https://shop.test" not in sites.calls

    await bot.handle_update(press(ui.CB_TOGGLE + service.id))
    assert "⏸ Пауза" in bot_api.buttons()
    assert [s.name for s in monitor.registry.enabled()] == ["Config site", "Магазин"]


async def test_delete_asks_for_confirmation(
    bot: TelegramBot, bot_api: BotApi, sites: ScriptedSites, monitor: MonitorService
) -> None:
    sites.set("https://shop.test", 200)
    await add_site(bot, "https://shop.test", "Магазин")
    service = monitor.registry.get("Магазин")
    assert service is not None

    await bot.handle_update(press(ui.CB_DELETE + service.id))
    assert "Удалить «Магазин»?" in bot_api.last_text
    assert bot_api.buttons() == ["🗑 Да, удалить", "« Отмена"]
    assert monitor.registry.get("Магазин") is not None  # nothing happened yet

    await bot.handle_update(press(ui.CB_DELETE_CONFIRM + service.id))
    assert "«Магазин» удалён" in bot_api.last_text
    assert monitor.registry.get("Магазин") is None
    assert "Магазин" not in monitor.states


# --- polling loop ------------------------------------------------------------------


async def test_start_registers_commands_and_skips_old_updates(
    bot: TelegramBot, bot_api: BotApi
) -> None:
    bot_api.queue(message("/start"))
    bot_api.queue(message("/start"))
    await bot.start()

    assert [c["command"] for c in bot_api.commands] == [name for name, _ in ui.BOT_COMMANDS]
    assert bot_api.get_updates_calls[0]["offset"] == -1
    assert bot_api.sent == []  # the old commands were dropped, not executed

    bot_api.pending.clear()
    await run_briefly(bot, bot_api, message("/menu"))
    assert "service-sentinel" in bot_api.last_text


async def test_run_stops_on_signal(bot: TelegramBot) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(bot.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2)  # the long poll is cancelled, no waiting 25s


async def test_broken_update_does_not_stop_the_bot(bot: TelegramBot, bot_api: BotApi) -> None:
    await run_briefly(bot, bot_api, {"callback_query": {"id": "x"}}, message("/menu"))
    assert "service-sentinel" in bot_api.last_text


# --- locking and alerts ------------------------------------------------------------


async def test_manual_check_does_not_duplicate_alerts(
    bot: TelegramBot, bot_api: BotApi, sites: ScriptedSites, monitor: MonitorService
) -> None:
    sites.set(CONFIG_SITE, 500)
    service = monitor.registry.get("Config site")
    assert service is not None

    await bot.handle_update(press(ui.CB_CHECK + service.id))  # manual check -> DOWN + alert
    alerts = [t for t in bot_api.texts if t.startswith("🔴 SERVICE DOWN")]
    assert len(alerts) == 1

    await monitor.run_cycle()
    await bot.handle_update(press(ui.CB_CHECK + service.id))
    await monitor.run_cycle()
    assert len([t for t in bot_api.texts if t.startswith("🔴 SERVICE DOWN")]) == 1


async def test_one_site_is_never_checked_twice_at_once(
    bot_client: TelegramClient, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = 0
    overlaps = 0

    async def slow_site(request: httpx.Request) -> httpx.Response:
        nonlocal running, overlaps
        running += 1
        overlaps = max(overlaps, running)
        await asyncio.sleep(0.05)
        running -= 1
        return httpx.Response(200)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(slow_site)) as client,
        StateStore(db_path) as store,
    ):
        monitor = MonitorService(
            SETTINGS,
            ServiceRegistry(store, CONFIG_SERVICES),
            HttpChecker(client),
            store,
            bot_client,
        )
        await monitor.load_state()
        service = monitor.registry.get("Config site")
        assert service is not None
        # A scheduled cycle and a manual check of the same site at the same time.
        await asyncio.gather(monitor.run_cycle(), monitor.check_service(service))
    assert overlaps == 1


async def test_bot_and_monitor_stop_together(
    bot: TelegramBot, bot_api: BotApi, sites: ScriptedSites, monitor: MonitorService
) -> None:
    """What main.py runs: one process, one stop event (SIGTERM) for both tasks."""
    sites.set(CONFIG_SITE, 200)
    stop = asyncio.Event()

    async def both() -> None:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(monitor.run_forever(stop))
            tasks.create_task(bot.run(stop))

    task = asyncio.create_task(both())
    await asyncio.sleep(0.05)
    bot_api.queue(message("/menu"))
    await asyncio.sleep(0.15)
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert monitor.states["Config site"].status is Status.UP  # the cycle ran
    assert "service-sentinel" in bot_api.last_text  # the bot answered


async def test_delete_during_a_check_does_not_resurrect_the_site(
    bot_client: TelegramClient, db_path: Path
) -> None:
    release = asyncio.Event()

    async def slow_site(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(500)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(slow_site)) as client,
        StateStore(db_path) as store,
    ):
        monitor = MonitorService(
            SETTINGS, ServiceRegistry(store, []), HttpChecker(client), store, bot_client
        )
        await monitor.load_state()
        service = await monitor.add_service("https://slow.test", "Медленный")

        checking = asyncio.create_task(monitor.check_service(service))
        await asyncio.sleep(0)
        removing = asyncio.create_task(monitor.remove_service("Медленный"))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(checking, removing)

        assert monitor.registry.get("Медленный") is None
        assert await store.list_services() == []
        assert (await store.load("Медленный", "https://slow.test")).status is Status.UNKNOWN
