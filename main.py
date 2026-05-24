import asyncio
import os
import logging
from datetime import datetime, date, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ChatJoinRequest
from aiogram.filters import CommandStart, Command
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Настройки ───────────────────────────────────────────
BOT_TOKEN     = os.getenv("BOT_TOKEN")
TOKEN_ADDRESS = os.getenv("TOKEN_ADDRESS", "")
MIN_BALANCE   = int(os.getenv("MIN_BALANCE", "1000"))
MAIN_GROUP_ID = os.getenv("MAIN_GROUP_ID", "")
CHAT_7D       = os.getenv("CHAT_7D", "")
CHAT_14D      = os.getenv("CHAT_14D", "")
CHAT_21D      = os.getenv("CHAT_21D", "")
CHAT_30D      = os.getenv("CHAT_30D", "")
CHAT_WHALE    = os.getenv("CHAT_WHALE", "")
WHALE_THRESHOLD = int(os.getenv("WHALE_THRESHOLD", "100000"))
DB_PATH       = "holdapp.db"

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

# ─── База данных ─────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id     INTEGER PRIMARY KEY,
                wallet_address  TEXT UNIQUE,
                join_date       TEXT,
                last_checkin    TEXT,
                checkin_streak  INTEGER DEFAULT 0,
                username        TEXT
            )
        """)
        await db.commit()
    logger.info("База данных готова ✅")

# ─── TON API ─────────────────────────────────────────────
async def get_jetton_balance(wallet_address: str) -> int:
    if not TOKEN_ADDRESS:
        return 9999  # тестовый режим
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://tonapi.io/v2/accounts/{wallet_address}/jettons/{TOKEN_ADDRESS}"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    balance  = int(data.get("balance", 0))
                    decimals = data.get("jetton", {}).get("decimals", 9)
                    return balance // (10 ** decimals)
    except Exception as e:
        logger.error(f"TON API ошибка: {e}")
    return 0

# ─── Вспомогательные функции ─────────────────────────────
def get_level(days: int) -> str:
    if days >= 30: return "💎 Diamond"
    if days >= 21: return "🗓️ 21 день"
    if days >= 14: return "📅 14 дней"
    if days >= 7:  return "⏱️ 7 дней"
    return "🌱 Новичок"

def calc_hold_days(join_date_str: str) -> int:
    if not join_date_str:
        return 0
    join = datetime.fromisoformat(join_date_str).date()
    return (date.today() - join).days

async def get_user(telegram_id: int = None, wallet_address: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if telegram_id:
            cur = await db.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        else:
            cur = await db.execute(
                "SELECT * FROM users WHERE wallet_address = ?", (wallet_address,))
        return await cur.fetchone()

# ─── Управление чатами по уровням ────────────────────────
async def update_user_chats(telegram_id: int, balance: int, hold_days: int):
    """Добавляет или удаляет пользователя из чатов по уровню"""
    levels = [
        (CHAT_7D,    7,   False),
        (CHAT_14D,   14,  False),
        (CHAT_21D,   21,  False),
        (CHAT_30D,   30,  False),
        (CHAT_WHALE, 0,   True),   # whale — по балансу
    ]
    for chat_id, days_needed, is_whale in levels:
        if not chat_id:
            continue
        try:
            if is_whale:
                eligible = balance >= WHALE_THRESHOLD
            else:
                eligible = balance >= MIN_BALANCE and hold_days >= days_needed

            member = await bot.get_chat_member(chat_id, telegram_id)
            in_chat = member.status not in ("left", "kicked")

            if eligible and not in_chat:
                await bot.unban_chat_member(chat_id, telegram_id)  # разрешаем вход
                invite = await bot.create_chat_invite_link(
                    chat_id, member_limit=1, expire_date=int(
                        (datetime.now() + timedelta(hours=1)).timestamp()))
                await bot.send_message(
                    telegram_id,
                    f"🎉 Ты открыл новый уровень!\n\nВойди в чат: {invite.invite_link}")

            elif not eligible and in_chat:
                await bot.ban_chat_member(chat_id, telegram_id)
                await bot.unban_chat_member(chat_id, telegram_id)  # кик без бана
                await bot.send_message(
                    telegram_id,
                    "⚠️ Твой баланс упал — ты удалён из чата уровня. "
                    "Пополни кошелёк чтобы вернуться.")
        except Exception as e:
            logger.warning(f"Ошибка управления чатом {chat_id}: {e}")

# ─── FastAPI ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    asyncio.create_task(start_bot())
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {"status": "Hold App работает ✅"}

@app.get("/api/user")
async def get_user_stats(address: str):
    balance = await get_jetton_balance(address)
    user    = await get_user(wallet_address=address)

    if not user:
        return {
            "balance":          balance,
            "hold_days":        0,
            "level":            "🌱 Новичок",
            "checked_in_today": False,
            "checkin_streak":   0,
            "eligible":         balance >= MIN_BALANCE
        }

    hold_days        = calc_hold_days(user["join_date"])
    checked_in_today = user["last_checkin"] == str(date.today())

    return {
        "balance":          balance,
        "hold_days":        hold_days,
        "level":            get_level(hold_days),
        "checked_in_today": checked_in_today,
        "checkin_streak":   user["checkin_streak"] or 0,
        "eligible":         balance >= MIN_BALANCE
    }

class CheckInRequest(BaseModel):
    address:     str
    telegram_id: int

@app.post("/api/checkin")
async def checkin(req: CheckInRequest):
    balance = await get_jetton_balance(req.address)
    if balance < MIN_BALANCE:
        raise HTTPException(status_code=403, detail="Недостаточно токенов")

    today     = str(date.today())
    yesterday = str(date.today() - timedelta(days=1))

    async with aiosqlite.connect(DB_PATH) as db:
        user = await get_user(wallet_address=req.address)

        if user:
            if user["last_checkin"] == today:
                return {"status": "already_checked_in"}

            new_streak = ((user["checkin_streak"] or 0) + 1
                          if user["last_checkin"] == yesterday else 1)

            await db.execute(
                "UPDATE users SET last_checkin=?, checkin_streak=?, telegram_id=? "
                "WHERE wallet_address=?",
                (today, new_streak, req.telegram_id, req.address))
        else:
            await db.execute(
                "INSERT INTO users "
                "(telegram_id, wallet_address, join_date, last_checkin, checkin_streak) "
                "VALUES (?,?,?,?,1)",
                (req.telegram_id, req.address, today, today))

        await db.commit()

    hold_days = calc_hold_days(today)
    asyncio.create_task(
        update_user_chats(req.telegram_id, balance, hold_days))

    return {"status": "ok", "streak": new_streak if user else 1}

# ─── Telegram Bot ─────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я бот Hold App.\n\n"
        "Подключи кошелёк в Mini App чтобы начать участвовать в розыгрышах.\n\n"
        "📋 Команды:\n"
        "/status — твой текущий статус\n"
        "/help — помощь"
    )

@dp.message(Command("status"))
async def cmd_status(message: Message):
    user = await get_user(telegram_id=message.from_user.id)
    if not user or not user["wallet_address"]:
        await message.answer(
            "❌ Кошелёк не подключён.\n"
            "Открой Mini App и подключи кошелёк.")
        return

    balance       = await get_jetton_balance(user["wallet_address"])
    hold_days     = calc_hold_days(user["join_date"])
    checked_today = user["last_checkin"] == str(date.today())

    await message.answer(
        f"📊 *Твой статус*\n\n"
        f"💼 Кошелёк: `{user['wallet_address'][:8]}...`\n"
        f"💰 Баланс: {balance:,} токенов\n"
        f"📅 Дней холда: {hold_days}\n"
        f"🏆 Уровень: {get_level(hold_days)}\n"
        f"🔥 Стрик: {user['checkin_streak'] or 0} дней\n"
        f"✅ Check-in сегодня: {'Да' if checked_today else 'Нет'}",
        parse_mode="Markdown"
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "ℹ️ *Hold App — как это работает*\n\n"
        "1. Купи токены и подключи кошелёк в Mini App\n"
        "2. Делай ежедневный Check In\n"
        "3. Чем дольше держишь — тем выше уровень\n"
        "4. Каждый уровень открывает новый чат с розыгрышами\n\n"
        "🏆 *Уровни:*\n"
        "🌱 Новичок — любой баланс\n"
        "⏱️ 7 дней холда\n"
        "📅 14 дней холда\n"
        "🗓️ 21 день холда\n"
        "💎 Diamond — 30 дней холда\n"
        f"🐋 Whale — {WHALE_THRESHOLD:,}+ токенов",
        parse_mode="Markdown"
    )

@dp.chat_join_request()
async def handle_join_request(request: ChatJoinRequest):
    user = await get_user(telegram_id=request.from_user.id)

    if not user or not user["wallet_address"]:
        await request.decline()
        await bot.send_message(
            request.from_user.id,
            "❌ Сначала подключи кошелёк в Mini App")
        return

    balance = await get_jetton_balance(user["wallet_address"])

    if balance >= MIN_BALANCE:
        await request.approve()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET join_date=? "
                "WHERE telegram_id=? AND join_date IS NULL",
                (str(date.today()), request.from_user.id))
            await db.commit()
        await bot.send_message(
            request.from_user.id,
            f"✅ Добро пожаловать! Твой баланс: {balance:,} токенов\n"
            f"Делай ежедневный Check In чтобы повышать уровень 🚀")
    else:
        await request.decline()
        await bot.send_message(
            request.from_user.id,
            f"❌ Недостаточно токенов.\n"
            f"Нужно минимум {MIN_BALANCE:,}, у тебя {balance:,}")

async def start_bot():
    logger.info("Бот запускается...")
    await dp.start_polling(bot)
