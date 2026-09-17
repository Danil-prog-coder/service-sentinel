"""Control bot: long polling ``getUpdates`` + inline keyboards, no bot framework.

Runs in the same process (and event loop) as the monitoring cycle and talks to
:class:`MonitorService` only, so every change made from Telegram is picked up by the
next check cycle. Messages from any chat other than ``TELEGRAM_CHAT_ID`` are ignored.
"""

import asyncio
import logging
from collections import deque
from datetime import tzinfo
from typing import Any

from monitor.monitoring import MonitorService
from monitor.registry import ManagedService, RegistryError
from monitor.telegram import ui
from monitor.telegram.client import TelegramClient, TelegramError

logger = logging.getLogger(__name__)

POLL_TIMEOUT = 25.0
MIN_BACKOFF = 1.0
MAX_BACKOFF = 60.0
PENDING_ADD_LIMIT = 20


class TelegramBot:
    def __init__(
        self,
        client: TelegramClient,
        monitor: MonitorService,
        chat_id: str,
        *,
        poll_timeout: float = POLL_TIMEOUT,
    ) -> None:
        self._client = client
        self._monitor = monitor
        self._chat_id = str(chat_id)
        self._poll_timeout = poll_timeout
        self._offset: int | None = None
        # message_ids of our own ForceReply prompts, so a reply is recognised as "add a site".
        self._pending_add: deque[int] = deque(maxlen=PENDING_ADD_LIMIT)

    # --- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        """Register commands and drop updates that piled up while the bot was down."""
        try:
            await self._client.set_my_commands(ui.BOT_COMMANDS)
        except TelegramError as exc:
            logger.warning("failed to register bot commands", extra={"error": str(exc)})
        await self._skip_pending_updates()

    async def _skip_pending_updates(self) -> None:
        try:
            # offset=-1 returns only the last pending update; confirming it drops the rest.
            updates = await self._client.get_updates(offset=-1, timeout=0, max_attempts=1)
        except TelegramError as exc:
            logger.warning("could not skip old updates", extra={"error": str(exc)})
            return
        if updates:
            self._offset = int(updates[-1]["update_id"]) + 1
            logger.info("old telegram updates skipped", extra={"next_offset": self._offset})

    async def run(self, stop: asyncio.Event) -> None:
        await self.start()
        logger.info("telegram control bot started", extra={"chat_id": self._chat_id})
        backoff = MIN_BACKOFF
        while not stop.is_set():
            try:
                updates = await self._poll(stop)
            except TelegramError as exc:
                logger.warning(
                    "telegram polling failed, retrying",
                    extra={"error": str(exc), "retry_in": backoff},
                )
                await _wait(stop, backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            except Exception:
                # The bot is a convenience; monitoring must keep running whatever happens here.
                logger.exception("telegram polling crashed, retrying", extra={"retry_in": backoff})
                await _wait(stop, backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            backoff = MIN_BACKOFF
            for update in updates:
                if stop.is_set():
                    break
                await self._handle_safely(update)
        logger.info("telegram control bot stopped")

    async def _poll(self, stop: asyncio.Event) -> list[dict[str, Any]]:
        """Long-poll for updates, returning early (without them) when shutdown is requested."""
        poll = asyncio.create_task(
            self._client.get_updates(
                offset=self._offset, timeout=self._poll_timeout, max_attempts=1
            ),
            name="telegram-poll",
        )
        stop_waiter = asyncio.create_task(stop.wait(), name="telegram-stop-waiter")
        try:
            await asyncio.wait({poll, stop_waiter}, return_when=asyncio.FIRST_COMPLETED)
            if not poll.done():
                poll.cancel()
                await asyncio.gather(poll, return_exceptions=True)
                return []
            updates = poll.result()
        finally:
            stop_waiter.cancel()
        if updates:
            self._offset = int(updates[-1]["update_id"]) + 1
        return updates

    async def _handle_safely(self, update: dict[str, Any]) -> None:
        try:
            await self.handle_update(update)
        except TelegramError as exc:
            logger.warning(
                "telegram request failed while handling update", extra={"error": str(exc)}
            )
        except Exception:
            logger.exception("failed to handle telegram update")

    # --- routing -------------------------------------------------------------------

    async def handle_update(self, update: dict[str, Any]) -> None:
        if (callback := update.get("callback_query")) is not None:
            await self._on_callback(callback)
        elif (message := update.get("message")) is not None:
            await self._on_message(message)

    def _is_own_chat(self, chat: dict[str, Any] | None) -> bool:
        return chat is not None and str(chat.get("id")) == self._chat_id

    async def _on_message(self, message: dict[str, Any]) -> None:
        if not self._is_own_chat(message.get("chat")):
            logger.debug(
                "ignoring message from another chat",
                extra={"chat_id": str((message.get("chat") or {}).get("id"))},
            )
            return
        text = (message.get("text") or "").strip()
        if not text:
            return

        reply_to = (message.get("reply_to_message") or {}).get("message_id")
        if reply_to in self._pending_add:
            await self._add_site(text)
            return
        if text.startswith("/"):
            await self._on_command(text)

    async def _on_command(self, text: str) -> None:
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()  # /status@my_sentinel_bot
        argument = argument.strip()

        if command in ("/start", "/menu"):
            await self._send_menu()
        elif command in ("/list", "/status", "/sites"):
            await self._send_sites()
        elif command == "/check":
            await self._check_all()
        elif command == "/help":
            await self._client.send_message(ui.HELP_TEXT)
        elif command == "/add":
            if argument:
                await self._add_site(argument)
            else:
                await self._ask_for_url()
        else:
            await self._client.send_message("Не знаю такую команду. /menu — меню, /help — справка.")

    async def _on_callback(self, callback: dict[str, Any]) -> None:
        query_id = str(callback.get("id"))
        message = callback.get("message") or {}
        if not self._is_own_chat(message.get("chat")):
            await self._client.answer_callback_query(query_id)
            return

        data = str(callback.get("data") or "")
        message_id = int(message.get("message_id") or 0)
        logger.info("telegram button pressed", extra={"data": data})

        if data == ui.CB_MENU:
            await self._answer(query_id)
            await self._edit(message_id, *self._menu_view())
        elif data == ui.CB_LIST:
            await self._answer(query_id)
            await self._edit(message_id, *self._sites_view())
        elif data == ui.CB_HELP:
            await self._answer(query_id)
            await self._edit(
                message_id, ui.HELP_TEXT, ui.keyboard([ui.button("« Назад", ui.CB_MENU)])
            )
        elif data == ui.CB_ADD:
            await self._answer(query_id)
            await self._ask_for_url()
        elif data == ui.CB_CHECK_ALL:
            await self._answer(query_id, "Проверяю…")
            await self._check_all(message_id)
        elif data.startswith(ui.CB_CARD):
            await self._answer(query_id)
            await self._show_card(message_id, data.removeprefix(ui.CB_CARD))
        elif data.startswith(ui.CB_CHECK):
            await self._check_one(message_id, data.removeprefix(ui.CB_CHECK), query_id)
        elif data.startswith(ui.CB_TOGGLE):
            await self._toggle(message_id, data.removeprefix(ui.CB_TOGGLE), query_id)
        elif data.startswith(ui.CB_DELETE_CONFIRM):
            await self._delete(message_id, data.removeprefix(ui.CB_DELETE_CONFIRM), query_id)
        elif data.startswith(ui.CB_DELETE):
            await self._ask_delete(message_id, data.removeprefix(ui.CB_DELETE), query_id)
        else:
            await self._answer(query_id)

    # --- actions -------------------------------------------------------------------

    async def _send_menu(self) -> None:
        text, markup = self._menu_view()
        await self._client.send_message(text, reply_markup=markup)

    async def _send_sites(self) -> None:
        text, markup = self._sites_view()
        await self._client.send_message(text, reply_markup=markup)

    async def _check_all(self, message_id: int | None = None) -> None:
        text, markup = ui.checking_view("все сайты")
        if message_id is None:
            message_id = await self._client.send_message(text)
        else:
            await self._edit(message_id, text, markup)
        await self._monitor.run_cycle()
        listing, list_markup = self._sites_view()
        await self._edit(message_id, f"✅ Проверка завершена.\n\n{listing}", list_markup)

    async def _ask_for_url(self) -> None:
        message_id = await self._client.send_message(ui.ADD_PROMPT, reply_markup=ui.force_reply())
        if message_id:
            self._pending_add.append(message_id)

    async def _add_site(self, text: str) -> None:
        raw_url, _, name = text.strip().partition(" ")
        try:
            service = await self._monitor.add_service(raw_url, name.strip() or None)
        except RegistryError as exc:
            await self._client.send_message(f"⚠️ {exc}")
            return
        result = await self._monitor.check_service(service)
        card, markup = ui.card_view(service, self._monitor.state_of(service.name), self._tz)
        banner = ui.check_banner(result, self._tz)
        await self._client.send_message(
            f"✅ Сайт добавлен.\n{banner}\n\n{card}", reply_markup=markup
        )

    async def _show_card(self, message_id: int, sid: str) -> None:
        service = self._monitor.registry.by_id(sid)
        if service is None:
            await self._edit(message_id, *self._sites_view())
            return
        await self._edit(
            message_id, *ui.card_view(service, self._monitor.state_of(service.name), self._tz)
        )

    async def _check_one(self, message_id: int, sid: str, query_id: str) -> None:
        service = self._resolve(sid)
        if service is None:
            await self._answer(query_id, "Сайт не найден.", alert=True)
            await self._edit(message_id, *self._sites_view())
            return
        await self._answer(query_id, "Проверяю…")
        # Two edits on purpose: the first one makes the message visibly change even when
        # the result turns out identical, so pressing the button never looks ignored.
        await self._edit(message_id, *ui.checking_view(f"«{service.name}»"))
        result = await self._monitor.check_service(service)
        card, markup = ui.card_view(service, self._monitor.state_of(service.name), self._tz)
        await self._edit(message_id, f"{ui.check_banner(result, self._tz)}\n\n{card}", markup)

    async def _toggle(self, message_id: int, sid: str, query_id: str) -> None:
        service = self._resolve(sid)
        if service is None:
            await self._answer(query_id, "Сайт не найден.", alert=True)
            return
        try:
            updated = await self._monitor.set_enabled(service.name, not service.enabled)
        except RegistryError as exc:
            await self._answer(query_id, str(exc), alert=True)
            return
        await self._answer(query_id, "На паузе." if not updated.enabled else "Снова проверяется.")
        await self._edit(
            message_id, *ui.card_view(updated, self._monitor.state_of(updated.name), self._tz)
        )

    async def _ask_delete(self, message_id: int, sid: str, query_id: str) -> None:
        service = self._resolve(sid)
        if service is None:
            await self._answer(query_id, "Сайт не найден.", alert=True)
            return
        if not service.editable:
            await self._answer(query_id, ui.CONFIG_ONLY, alert=True)
            return
        await self._answer(query_id)
        await self._edit(message_id, *ui.delete_confirm_view(service))

    async def _delete(self, message_id: int, sid: str, query_id: str) -> None:
        service = self._resolve(sid)
        if service is None:
            await self._answer(query_id, "Сайт не найден.", alert=True)
            await self._edit(message_id, *self._sites_view())
            return
        try:
            await self._monitor.remove_service(service.name)
        except RegistryError as exc:
            await self._answer(query_id, str(exc), alert=True)
            return
        await self._answer(query_id, "Удалено.")
        text, markup = self._sites_view()
        await self._edit(message_id, f"🗑 «{service.name}» удалён.\n\n{text}", markup)

    # --- helpers -------------------------------------------------------------------

    @property
    def _tz(self) -> tzinfo:
        return self._monitor.settings.tz

    def _rows(self) -> list[ui.Row]:
        return [
            (service, self._monitor.state_of(service.name))
            for service in self._monitor.registry.all()
        ]

    def _menu_view(self) -> tuple[str, ui.Keyboard]:
        return ui.main_menu(self._rows(), self._monitor.settings.interval)

    def _sites_view(self) -> tuple[str, ui.Keyboard]:
        return ui.sites_view(self._rows(), self._tz)

    def _resolve(self, sid: str) -> ManagedService | None:
        return self._monitor.registry.by_id(sid)

    async def _answer(self, query_id: str, text: str | None = None, *, alert: bool = False) -> None:
        await self._client.answer_callback_query(query_id, text, show_alert=alert)

    async def _edit(self, message_id: int, text: str, markup: ui.Keyboard) -> None:
        await self._client.edit_message_text(self._chat_id, message_id, text, reply_markup=markup)


async def _wait(stop: asyncio.Event, delay: float) -> None:
    try:
        async with asyncio.timeout(delay):
            await stop.wait()
    except TimeoutError:
        pass
