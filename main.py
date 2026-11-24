import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REQ_FILE = BASE_DIR / "requirements.txt"

try:
    import FunPayAPI  # noqa: F401
    import aiogram  # noqa: F401
except ModuleNotFoundError:
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-cache-dir",
        "aiogram==3.13.1",
        "FunPayAPI==1.1.0",
    ]
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError:
        subprocess.check_call(cmd + ["--break-system-packages"])

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from FunPayAPI import Account, Runner, enums, types

logging.basicConfig(level=logging.INFO)

# === НАСТРОЙКИ ИЗ ENVIRONMENT VARIABLES ===
TG_TOKEN = os.getenv("TG_TOKEN", "").strip()

_admin_raw = os.getenv("ADMIN_ID", "0").strip()
ADMIN_ID = int(_admin_raw) if _admin_raw.isdigit() else 0

FUNPAY_GOLDEN_KEY = os.getenv("FUNPAY_GOLDEN_KEY", "").strip()

# Часть названия лота на FunPay для автовыдачи
LOT_TITLE_KEYWORD = os.getenv("LOT_TITLE_KEYWORD", "Google аккаунт").strip()

# Сообщение покупателю после оплаты
DELIVERY_TEMPLATE = (
    "Спасибо за покупку! Вот данные от вашего аккаунта:\n"
    "Логин: {login}\n"
    "Пароль: {password}\n\n"
    "Сразу после входа смените пароль и проверьте доступ."
)

STOCK_FILE = BASE_DIR / "stock.txt"
SENT_ORDERS_FILE = BASE_DIR / "sent_orders.txt"

file_lock = asyncio.Lock()


def ensure_files() -> None:
    if not STOCK_FILE.exists():
        STOCK_FILE.write_text("", encoding="utf-8")
    if not SENT_ORDERS_FILE.exists():
        SENT_ORDERS_FILE.write_text("", encoding="utf-8")


def get_stock_lines() -> list[str]:
    ensure_files()
    raw = STOCK_FILE.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in raw if line.strip()]


def save_stock_lines(lines: list[str]) -> None:
    content = "\n".join(lines)
    if content:
        content += "\n"
    STOCK_FILE.write_text(content, encoding="utf-8")


def is_order_already_processed(order_id: str) -> bool:
    ensure_files()
    processed = set(SENT_ORDERS_FILE.read_text(encoding="utf-8").splitlines())
    return order_id in processed


def mark_order_processed(order_id: str) -> None:
    ensure_files()
    with SENT_ORDERS_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{order_id}\n")


async def pop_one_account() -> tuple[str, str] | None:
    """Берёт первую строку login:password из stock.txt и удаляет её из очереди."""
    async with file_lock:
        lines = get_stock_lines()
        if not lines:
            return None
        item = lines.pop(0)
        save_stock_lines(lines)

    if ":" in item:
        login, password = item.split(":", 1)
    else:
        login, password = item, ""
    return login.strip(), password.strip()


async def add_accounts_from_text(text: str) -> int:
    """Добавляет новые строки login:password из сообщения админа в Telegram."""
    incoming = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("/")
    ]
    if not incoming:
        return 0
    async with file_lock:
        lines = get_stock_lines()
        lines.extend(incoming)
        save_stock_lines(lines)
    return len(incoming)


def is_admin(message: Message) -> bool:
    return ADMIN_ID != 0 and message.from_user is not None and message.from_user.id == ADMIN_ID


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()

    @dp.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        if not is_admin(message):
            return
        await message.answer(
            "Бот запущен и готов к работе.\n\n"
            "Отправляй сюда строки <code>login:password</code> (можно сразу пачкой) — "
            "я добавлю их в очередь на автовыдачу.\n\n"
            "Команды:\n"
            "/stock — сколько аккаунтов в наличии\n"
            "/clear — очистить базу",
            parse_mode="HTML",
        )

    @dp.message(Command("stock"))
    async def cmd_stock(message: Message) -> None:
        if not is_admin(message):
            return
        async with file_lock:
            count = len(get_stock_lines())
        await message.answer(f"В наличии аккаунтов: <b>{count}</b>", parse_mode="HTML")

    @dp.message(Command("clear"))
    async def cmd_clear(message: Message) -> None:
        if not is_admin(message):
            return
        async with file_lock:
            save_stock_lines([])
        await message.answer("База аккаунтов очищена.")

    @dp.message(F.text)
    async def handle_new_accounts(message: Message) -> None:
        if not is_admin(message):
            return
        added = await add_accounts_from_text(message.text or "")
        if added == 0:
            await message.answer(
                "Не нашёл строк для добавления. Пришли аккаунты в формате <code>login:password</code>.",
                parse_mode="HTML",
            )
            return
        async with file_lock:
            total = len(get_stock_lines())
        await message.answer(
            f"Добавлено: <b>{added}</b> шт.\nВсего в наличии: <b>{total}</b> шт.",
            parse_mode="HTML",
        )

    return dp


def _resolve_chat_id(account: Account, order: object) -> int:
    chat_id = getattr(order, "chat_id", None)
    if chat_id is not None:
        return int(chat_id)
    buyer_username = getattr(order, "buyer_username", None)
    if buyer_username:
        chat = account.get_chat_by_name(buyer_username, True)
        if chat is not None and getattr(chat, "id", None) is not None:
            return int(chat.id)
    raise RuntimeError("Не удалось определить chat_id покупателя")


def funpay_worker_loop(bot: Bot, loop: asyncio.AbstractEventLoop) -> None:
    account = Account(FUNPAY_GOLDEN_KEY).get()
    runner = Runner(account)

    for event in runner.listen(requests_delay=4.0):
        if event.type != enums.EventTypes.NEW_ORDER:
            continue

        order = event.order
        order_id = str(order.id)

        if is_order_already_processed(order_id):
            continue

        title = getattr(order, "title", "") or getattr(order, "description", "") or ""
        if LOT_TITLE_KEYWORD.lower() not in title.lower():
            asyncio.run_coroutine_threadsafe(
                bot.send_message(
                    ADMIN_ID,
                    f"Новый заказ #{order_id} по другому лоту: <b>{title}</b>\nВыдай вручную.",
                    parse_mode="HTML",
                ),
                loop,
            )
            mark_order_processed(order_id)
            continue

        creds = asyncio.run_coroutine_threadsafe(pop_one_account(), loop).result()
        if creds is None:
            mark_order_processed(order_id)
            asyncio.run_coroutine_threadsafe(
                bot.send_message(
                    ADMIN_ID,
                    f"Оплачен заказ #{order_id} (<b>{title}</b>), но аккаунты закончились! Выдай вручную.",
                    parse_mode="HTML",
                ),
                loop,
            )
            continue

        login, password = creds
        msg_text = DELIVERY_TEMPLATE.format(login=login, password=password)

        try:
            chat_id = _resolve_chat_id(account, order)
            account.send_message(chat_id, msg_text)
            mark_order_processed(order_id)

            left = asyncio.run_coroutine_threadsafe(
                _get_stock_count_async(), loop
            ).result()

            asyncio.run_coroutine_threadsafe(
                bot.send_message(
                    ADMIN_ID,
                    f"Выдан аккаунт по заказу <b>#{order_id}</b>\n"
                    f"Данные: <code>{login}:{password}</code>\n"
                    f"Осталось в базе: <b>{left}</b> шт.",
                    parse_mode="HTML",
                ),
                loop,
            )
        except Exception as e:
            asyncio.run_coroutine_threadsafe(
                bot.send_message(
                    ADMIN_ID,
                    f"Ошибка отправки по заказу #{order_id}: {e}\n"
                    f"Аккаунт: <code>{login}:{password}</code>",
                    parse_mode="HTML",
                ),
                loop,
            )


async def _get_stock_count_async() -> int:
    async with file_lock:
        return len(get_stock_lines())


async def main() -> None:
    if not TG_TOKEN:
        raise RuntimeError("Не задана переменная окружения TG_TOKEN!")
    if ADMIN_ID == 0:
        raise RuntimeError("Не задана переменная окружения ADMIN_ID!")
    if not FUNPAY_GOLDEN_KEY:
        raise RuntimeError("Не задана переменная окружения FUNPAY_GOLDEN_KEY!")

    ensure_files()
    bot = Bot(token=TG_TOKEN)
    dp = build_dispatcher()
    loop = asyncio.get_running_loop()
    worker_task = asyncio.create_task(asyncio.to_thread(funpay_worker_loop, bot, loop))

    try:
        await dp.start_polling(bot)
    finally:
        worker_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
