"""Entry point: ``python -m monitor``."""

import argparse
import asyncio
import logging
import signal
import sys
from contextlib import AsyncExitStack
from pathlib import Path

import httpx

from monitor import __version__
from monitor.checker import HttpChecker
from monitor.config import AppConfig, ConfigError, EnvSettings, load_config, load_dotenv
from monitor.healthcheck import write_heartbeat
from monitor.log import configure_logging
from monitor.monitoring import MonitorService
from monitor.registry import ServiceRegistry
from monitor.storage import StateStore
from monitor.telegram import DryRunNotifier, Notifier, TelegramClient, TelegramError
from monitor.telegram.bot import TelegramBot

logger = logging.getLogger("monitor")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="monitor", description="HTTP monitor with Telegram alerts"
    )
    parser.add_argument("-c", "--config", type=Path, help="path to YAML config (env CONFIG_PATH)")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="local .env file")
    parser.add_argument("--once", action="store_true", help="run a single check cycle and exit")
    parser.add_argument(
        "--dry-run", action="store_true", help="log notifications instead of sending them"
    )
    parser.add_argument("--check-config", action="store_true", help="validate config and exit")
    parser.add_argument("--test-telegram", action="store_true", help="send a test message and exit")
    parser.add_argument(
        "--telegram-chats",
        action="store_true",
        help="print chats the bot has seen recently (to find TELEGRAM_CHAT_ID) and exit",
    )
    parser.add_argument(
        "--no-bot",
        action="store_true",
        help="do not run the Telegram control bot, only send alerts",
    )
    return parser.parse_args(argv)


def install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def request_stop(signame: str) -> None:
        if not stop.is_set():
            logger.info("shutdown signal received", extra={"signal": signame})
            stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop, sig.name)
        except NotImplementedError:  # Windows: no add_signal_handler
            signal.signal(
                sig,
                lambda signum, _frame: loop.call_soon_threadsafe(
                    request_stop, signal.Signals(signum).name
                ),
            )


async def _print_telegram_chats(telegram: TelegramClient) -> None:
    chats: dict[int, str] = {}
    for update in await telegram.get_updates():
        for key in ("message", "my_chat_member", "channel_post"):
            chat = (update.get(key) or {}).get("chat")
            if chat:
                title = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
                chats[chat["id"]] = f"{chat.get('type')}: {title}"
    if not chats:
        print("No chats found. Add the bot to the group, send a message there and retry.")
    for chat_id, description in chats.items():
        print(f"TELEGRAM_CHAT_ID={chat_id}    # {description}")


async def run(args: argparse.Namespace, env: EnvSettings, config: AppConfig) -> int:
    settings = config.monitor
    needs_telegram = not args.dry_run or args.test_telegram or args.telegram_chats
    if needs_telegram and not env.telegram_bot_token:
        logger.error("TELEGRAM_BOT_TOKEN is not set (use --dry-run to run without Telegram)")
        return 2
    if needs_telegram and not args.telegram_chats and not env.telegram_chat_id:
        logger.error("TELEGRAM_CHAT_ID is not set (use --dry-run to run without Telegram)")
        return 2

    async with AsyncExitStack() as stack:
        notifier: Notifier = DryRunNotifier()
        telegram: TelegramClient | None = None
        if env.telegram_bot_token:
            tg_http = await stack.enter_async_context(httpx.AsyncClient())
            telegram = TelegramClient(tg_http, env.telegram_bot_token, env.telegram_chat_id or "")
            if args.telegram_chats:
                await _print_telegram_chats(telegram)
                return 0
            if args.test_telegram:
                await telegram.send(f"✅ service-sentinel {__version__}: test message, bot works")
                logger.info("test message sent")
                return 0
            if not args.dry_run:
                notifier = telegram
        http = await stack.enter_async_context(
            httpx.AsyncClient(
                headers={"User-Agent": settings.user_agent},
                limits=httpx.Limits(max_connections=settings.max_concurrency),
            )
        )
        store = await stack.enter_async_context(StateStore(env.db_path))
        registry = ServiceRegistry(store, config.services)
        monitor = MonitorService(settings, registry, HttpChecker(http), store, notifier)
        await monitor.load_state()

        if args.once:
            await monitor.run_cycle()
            return 0

        bot: TelegramBot | None = None
        if telegram is not None and env.telegram_chat_id and not args.dry_run and not args.no_bot:
            bot = TelegramBot(telegram, monitor, env.telegram_chat_id)

        stop = asyncio.Event()
        install_signal_handlers(stop)
        write_heartbeat(env.heartbeat_path, settings.interval)
        # The bot and the monitoring loop share the process and the event loop, and both
        # end on the same stop event, so SIGTERM shuts everything down gracefully.
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(
                monitor.run_forever(
                    stop,
                    on_cycle_done=lambda: write_heartbeat(env.heartbeat_path, settings.interval),
                ),
                name="monitor",
            )
            if bot is not None:
                tasks.create_task(bot.run(stop), name="telegram-bot")
    logger.info("shutdown complete")
    return 0


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    load_dotenv(args.env_file)
    env = EnvSettings.from_env()
    configure_logging(env.log_level, env.log_format, secrets=[env.telegram_bot_token])

    config_path = args.config or env.config_path
    try:
        # config.yaml is optional: sites can live in Telegram (SQLite) only. A path given
        # explicitly with -c must exist, otherwise a typo would silently monitor nothing.
        config = load_config(config_path, required=args.config is not None)
    except ConfigError as exc:
        logger.error("configuration error: %s", exc)
        sys.exit(2)

    enabled = config.enabled_services
    logger.info(
        "service-sentinel starting",
        extra={
            "version": __version__,
            "config": str(config_path),
            "config_found": config_path.is_file(),
            "services_total": len(config.services),
            "services_enabled": len(enabled),
            "interval": config.monitor.interval,
            "failure_threshold": config.monitor.failure_threshold,
            "recovery_threshold": config.monitor.recovery_threshold,
            "telegram_configured": bool(env.telegram_bot_token and env.telegram_chat_id),
            "dry_run": args.dry_run,
        },
    )
    if args.check_config:
        logger.info("configuration is valid")
        sys.exit(0)
    if not enabled and not (args.test_telegram or args.telegram_chats):
        logger.info("no services in config.yaml, using the sites added from Telegram")

    try:
        code = asyncio.run(run(args, env, config))
    except KeyboardInterrupt:
        code = 130
    except TelegramError as exc:
        logger.error("telegram error: %s", exc)
        code = 1
    except Exception:
        logger.exception("monitor crashed")
        code = 1
    sys.exit(code)
