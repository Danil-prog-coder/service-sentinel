"""Texts and inline keyboards of the control bot (pure functions, no I/O).

``callback_data`` is limited to 64 bytes by Telegram, so buttons carry a short service id
(:func:`monitor.registry.service_id`) instead of the name.
"""

from collections.abc import Sequence
from datetime import tzinfo
from typing import Any

from monitor.checker.http import status_text
from monitor.models import CheckOutcome, CheckResult, ServiceState, Status
from monitor.registry import ManagedService, ServiceSource
from monitor.telegram.formatting import format_time

Keyboard = dict[str, Any]

# callback_data prefixes
CB_MENU = "menu"
CB_LIST = "list"
CB_ADD = "add"
CB_CHECK_ALL = "checkall"
CB_HELP = "help"
CB_CARD = "card:"
CB_CHECK = "check:"
CB_TOGGLE = "toggle:"
CB_DELETE = "del:"
CB_DELETE_CONFIRM = "delyes:"

BOT_COMMANDS = [
    ("menu", "Главное меню"),
    ("status", "Статус всех сайтов"),
    ("list", "Список сайтов"),
    ("check", "Проверить все сайты сейчас"),
    ("add", "Добавить сайт: /add https://site.ru [название]"),
    ("help", "Справка"),
]

ADD_PROMPT = (
    "➕ Пришлите ссылку ответом на это сообщение.\n\n"
    "Например:\n"
    "https://site.ru\n"
    "https://site.ru Мой магазин\n\n"
    "Без названия оно возьмётся из домена."
)

HELP_TEXT = (
    "❓ service-sentinel\n\n"
    "Бот проверяет сайты по HTTP-статусу и пишет сюда, только когда статус меняется: "
    "сайт упал или снова работает.\n\n"
    "Кнопки:\n"
    "📋 Сайты — список и карточка каждого сайта\n"
    "➕ Добавить — добавить сайт по ссылке\n"
    "🔄 Проверить все — внеплановая проверка прямо сейчас\n\n"
    "Команды:\n"
    "/add <ссылка> [название] — добавить сайт\n"
    "/list — список сайтов\n"
    "/status — статус всех сайтов\n"
    "/check — проверить все сайты\n"
    "/menu — главное меню\n\n"
    "Статусы: 🟢 работает · 🔴 не работает · ⚪ ещё не проверялся · ⏸ на паузе\n\n"
    "Сайты из config.yaml помечены 📄: их можно смотреть и проверять, "
    "а менять — только в файле на сервере."
)

CONFIG_ONLY = "Этот сайт описан в config.yaml — пауза и удаление только через файл."

# Telegram limits the size of an inline keyboard; with more sites the list stays readable.
MAX_SITE_BUTTONS = 50

_STATUS_LABEL = {
    Status.UP: "работает",
    Status.DOWN: "не работает",
    Status.UNKNOWN: "ещё не проверялся",
}
_STATUS_ICON = {Status.UP: "🟢", Status.DOWN: "🔴", Status.UNKNOWN: "⚪"}


def _plural(number: int, forms: tuple[str, str, str]) -> str:
    """Russian plural form: 1 минута, 2 минуты, 5 минут."""
    tail = abs(number) % 100
    if 11 <= tail <= 14:
        return forms[2]
    tail %= 10
    if tail == 1:
        return forms[0]
    if 2 <= tail <= 4:
        return forms[1]
    return forms[2]


def format_interval(seconds: float) -> str:
    total = round(seconds)
    if total >= 3600 and total % 3600 == 0:
        hours = total // 3600
        return f"{hours} {_plural(hours, ('час', 'часа', 'часов'))}"
    if total >= 60:
        minutes = total // 60
        return f"{minutes} {_plural(minutes, ('минута', 'минуты', 'минут'))}"
    return f"{total} {_plural(total, ('секунда', 'секунды', 'секунд'))}"


def button(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}


def keyboard(*rows: Sequence[dict[str, str]]) -> Keyboard:
    return {"inline_keyboard": [list(row) for row in rows]}


def force_reply() -> Keyboard:
    # Groups have privacy mode on by default: the bot only sees commands and replies
    # to its own messages, so adding a site has to go through ForceReply.
    return {
        "force_reply": True,
        "input_field_placeholder": "https://site.ru Название",
        "selective": True,
    }


def check_banner(result: CheckResult | None, tz: tzinfo) -> str:
    """One line above the card telling the user what their manual check just returned."""
    if result is None:
        return "🔄 Проверка выполнена."
    when = format_time(result.checked_at, tz, seconds=True)
    code = status_text(result.status_code) if result.status_code is not None else None
    if result.outcome is CheckOutcome.SUCCESS:
        detail = code or "ответ получен"
        if result.response_time is not None:
            detail += f" за {result.response_time:.2f} с"
        return f"✅ Проверено {when} — {detail}"
    if result.outcome is CheckOutcome.IGNORED:
        # 4xx: the server answered, so the status does not change (see README).
        return f"⚠️ Проверено {when} — {code or 'ответ не учитывается'}, статус не меняется"
    return f"❌ Проверено {when} — {result.error or code or 'ошибка'}"


def checking_view(title: str) -> tuple[str, Keyboard]:
    """Placeholder shown while a check is running, so the message visibly changes."""
    return f"⏳ Проверяю {title}…", keyboard()


def status_icon(service: ManagedService, state: ServiceState | None) -> str:
    if not service.enabled:
        return "⏸"
    return _STATUS_ICON[state.status if state else Status.UNKNOWN]


def status_label(service: ManagedService, state: ServiceState | None) -> str:
    if not service.enabled:
        return "на паузе"
    return _STATUS_LABEL[state.status if state else Status.UNKNOWN]


Row = tuple[ManagedService, ServiceState | None]


def main_menu(rows: Sequence[Row], interval: float) -> tuple[str, Keyboard]:
    counts: dict[str, int] = {}
    for service, state in rows:
        icon = status_icon(service, state)
        counts[icon] = counts.get(icon, 0) + 1
    summary = " · ".join(f"{icon} {count}" for icon, count in counts.items()) or "пока пусто"
    text = (
        "🛰 service-sentinel\n\n"
        f"Сайтов: {len(rows)} — {summary}\n"
        f"Интервал проверки: {format_interval(interval)}."
    )
    return text, keyboard(
        [button("📋 Сайты", CB_LIST), button("➕ Добавить", CB_ADD)],
        [button("🔄 Проверить все", CB_CHECK_ALL), button("❓ Помощь", CB_HELP)],
    )


def sites_view(rows: Sequence[Row], tz: tzinfo) -> tuple[str, Keyboard]:
    if not rows:
        return (
            "📋 Сайтов пока нет.\n\nДобавьте первый — кнопкой ниже или командой\n"
            "/add https://site.ru",
            keyboard([button("➕ Добавить", CB_ADD)], [button("« Назад", CB_MENU)]),
        )

    lines = [f"📋 Сайты ({len(rows)})", ""]
    buttons: list[list[dict[str, str]]] = []
    for service, state in rows:
        icon = status_icon(service, state)
        mark = " 📄" if service.source is ServiceSource.CONFIG else ""
        lines.append(f"{icon} {service.name}{mark} — {status_label(service, state)}")
        if len(buttons) < MAX_SITE_BUTTONS:
            buttons.append([button(f"{icon} {service.name}", CB_CARD + service.id)])
    if len(rows) > MAX_SITE_BUTTONS:
        lines += ["", f"Кнопками открываются первые {MAX_SITE_BUTTONS} сайтов."]
    last_check = max(
        (state.last_check for _, state in rows if state and state.last_check), default=None
    )
    if last_check is not None:
        lines += ["", f"Последняя проверка: {format_time(last_check, tz, seconds=True)}"]
    buttons.append([button("🔄 Проверить все", CB_CHECK_ALL), button("➕ Добавить", CB_ADD)])
    buttons.append([button("« Назад", CB_MENU)])
    return "\n".join(lines), keyboard(*buttons)


def card_view(
    service: ManagedService, state: ServiceState | None, tz: tzinfo
) -> tuple[str, Keyboard]:
    icon = status_icon(service, state)
    lines = [f"{icon} {service.name}", "", f"Ссылка: {service.url}"]
    if service.check_url != service.url:
        lines.append(f"Проверяется: {service.check_url}")
    lines.append(f"Статус: {icon} {status_label(service, state)}")

    if state is not None:
        if state.status_since is not None:
            lines.append(f"С: {format_time(state.status_since, tz)}")
        if state.last_status_code is not None:
            lines.append(f"Последний код: {status_text(state.last_status_code)}")
        if state.response_time is not None:
            lines.append(f"Время ответа: {state.response_time:.2f} с")
        if state.last_error and state.status is not Status.UP:
            lines.append(f"Ошибка: {state.last_error}")
        lines.append(f"Последняя проверка: {format_time(state.last_check, tz, seconds=True)}")
    if service.source is ServiceSource.CONFIG:
        lines += ["", "📄 Сайт из config.yaml: пауза и удаление — только в файле."]

    controls = [button("🔄 Проверить", CB_CHECK + service.id)]
    if service.editable:
        controls.append(
            button("▶️ Включить", CB_TOGGLE + service.id)
            if not service.enabled
            else button("⏸ Пауза", CB_TOGGLE + service.id)
        )
        controls.append(button("🗑 Удалить", CB_DELETE + service.id))
    return "\n".join(lines), keyboard(controls, [button("« Назад", CB_LIST)])


def delete_confirm_view(service: ManagedService) -> tuple[str, Keyboard]:
    text = (
        f"🗑 Удалить «{service.name}»?\n\n"
        f"{service.url}\n\n"
        "Сайт и его история статусов будут удалены."
    )
    return text, keyboard(
        [
            button("🗑 Да, удалить", CB_DELETE_CONFIRM + service.id),
            button("« Отмена", CB_CARD + service.id),
        ]
    )
