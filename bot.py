import os
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.webhook import aiohttp_server as webhooks
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import asyncpg
from datetime import datetime, timedelta
import asyncio
import random
import requests
from apscheduler.schedulers.background import BackgroundScheduler

# Configure logging
logging.basicConfig(level=logging.INFO)

# Bot, Dispatcher, and Storage
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
TARGET_GROUP_ID = int(os.getenv("TARGET_GROUP_ID"))
DATABASE_URL = os.getenv("DATABASE_URL")

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# Database connection pool
db_pool = None

async def create_db_pool():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)

# States
class UserState(StatesGroup):
    language = State()
    gender = State()
    continent = State()
    age = State()
    profile_complete = State()
    in_session = State()

# Keepalive ping (every 14 minutes) - This is now handled by Render's internal mechanism or external pings
# The bot itself will not run a separate web server for ping

# --- Database Operations ---
async def create_tables():
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id BIGINT PRIMARY KEY,
                username TEXT,
                gender VARCHAR(12) CHECK(gender IN (\'Male\',\'Female\',\'Anonymous\')),
                language VARCHAR(2) CHECK(language IN (\'en\',\'ar\',\'hi\',\'id\')),
                continent VARCHAR(2),
                age INTEGER,
                is_vip BOOLEAN DEFAULT false,
                vip_expiry TIMESTAMP,
                is_banned BOOLEAN DEFAULT false
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id SERIAL PRIMARY KEY,
                user1_id BIGINT REFERENCES users(id),
                user2_id BIGINT REFERENCES users(id),
                start_time TIMESTAMP DEFAULT NOW()
            );
        """)

async def get_user(user_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)

async def create_user(user_id, username):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, username) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
            user_id, username
        )

async def update_user_profile(user_id, language=None, gender=None, continent=None, age=None):
    async with db_pool.acquire() as conn:
        if language: await conn.execute("UPDATE users SET language = $1 WHERE id = $2", language, user_id)
        if gender: await conn.execute("UPDATE users SET gender = $1 WHERE id = $2", gender, user_id)
        if continent: await conn.execute("UPDATE users SET continent = $1 WHERE id = $2", continent, user_id)
        if age: await conn.execute("UPDATE users SET age = $1 WHERE id = $2", age, user_id)

async def set_vip_status(user_id, months):
    async with db_pool.acquire() as conn:
        expiry_date = datetime.now() + timedelta(days=months*30)
        await conn.execute("UPDATE users SET is_vip = TRUE, vip_expiry = $1 WHERE id = $2", expiry_date, user_id)

async def ban_user(user_id):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_banned = TRUE WHERE id = $1", user_id)

async def unban_user(user_id):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_banned = FALSE WHERE id = $1", user_id)

async def get_active_users():
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM users WHERE is_banned = FALSE")

async def create_session(user1_id, user2_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "INSERT INTO sessions (user1_id, user2_id) VALUES ($1, $2) RETURNING session_id",
            user1_id, user2_id
        )

async def get_session(user_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM sessions WHERE user1_id = $1 OR user2_id = $1",
            user_id
        )

async def end_session(user_id):
    async with db_pool.acquire() as conn:
        session = await conn.fetchrow("SELECT * FROM sessions WHERE user1_id = $1 OR user2_id = $1", user_id)
        if session:
            await conn.execute("DELETE FROM sessions WHERE session_id = $1", session["session_id"])
            return session
        return None

# --- Handlers ---
@dp.message_handler(commands=["start"], state="*")
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or f"id_{user_id}"
    await create_user(user_id, username)
    user = await get_user(user_id)

    if user and user["is_banned"]:
        await message.answer("You are banned from using this bot.")
        return

    if user and user["language"] and user["gender"] and user["continent"] and user["age"]:
        await message.answer("Welcome back! Your profile is complete. You can use /profile to edit it or /find to start a chat.")
        await UserState.profile_complete.set()
    else:
        await message.answer("Welcome! Let\'s set up your profile. Please choose your language:", reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("English", callback_data="lang_en"),
            InlineKeyboardButton("العربية", callback_data="lang_ar"),
            InlineKeyboardButton("हिन्दी", callback_data="lang_hi"),
            InlineKeyboardButton("Bahasa Indonesia", callback_data="lang_id")
        ))
        await UserState.language.set()

@dp.callback_query_handler(lambda c: c.data.startswith("lang_"), state=UserState.language)
async def process_language(callback_query: types.CallbackQuery, state: FSMContext):
    lang = callback_query.data.split("_")[1]
    await update_user_profile(callback_query.from_user.id, language=lang)
    await callback_query.message.edit_text("Please choose your gender:", reply_markup=InlineKeyboardMarkup().add(
        InlineKeyboardButton("Male", callback_data="gender_Male"),
        InlineKeyboardButton("Female", callback_data="gender_Female"),
        InlineKeyboardButton("Anonymous", callback_data="gender_Anonymous")
    ))
    await UserState.gender.set()
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("gender_"), state=UserState.gender)
async def process_gender(callback_query: types.CallbackQuery, state: FSMContext):
    gender = callback_query.data.split("_")[1]
    await update_user_profile(callback_query.from_user.id, gender=gender)
    await callback_query.message.edit_text("Please choose your continent:", reply_markup=InlineKeyboardMarkup().add(
        InlineKeyboardButton("Africa", callback_data="continent_AF"),
        InlineKeyboardButton("Asia", callback_data="continent_AS"),
        InlineKeyboardButton("Europe", callback_data="continent_EU"),
        InlineKeyboardButton("North America", callback_data="continent_NA"),
        InlineKeyboardButton("South America", callback_data="continent_SA"),
        InlineKeyboardButton("Oceania", callback_data="continent_OC"),
        InlineKeyboardButton("Antarctica", callback_data="continent_AN")
    ))
    await UserState.continent.set()
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("continent_"), state=UserState.continent)
async def process_continent(callback_query: types.CallbackQuery, state: FSMContext):
    continent = callback_query.data.split("_")[1]
    await update_user_profile(callback_query.from_user.id, continent=continent)
    await callback_query.message.edit_text("Please enter your age (number):")
    await UserState.age.set()
    await callback_query.answer()

@dp.message_handler(state=UserState.age)
async def process_age(message: types.Message, state: FSMContext):
    try:
        age = int(message.text)
        if not (0 < age < 100):
            await message.answer("Please enter a valid age between 1 and 99.")
            return
        await update_user_profile(message.from_user.id, age=age)
        await message.answer("Profile complete! You can use /profile to edit it or /find to start a chat.")
        await UserState.profile_complete.set()
    except ValueError:
        await message.answer("Invalid age. Please enter a number.")

@dp.message_handler(commands=["profile"], state="*")
async def cmd_profile(message: types.Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if user:
        profile_text = f"Your Profile:\nLanguage: {user[\'language

@dp.message_handler(commands=["upgrade"], state="*")
async def cmd_upgrade(message: types.Message):
    text = """
    🔓 Upgrade to VIP (3 months):
    Pay $5 via:
    
    • Payeer: `P1060900640`
    • Bitcoin: `14qaZcSda7az1i9FFXp92vgpj9gj4wrK8z`
    
    Send payment proof to @AdminUsername
    """
    await message.answer(text, parse_mode="Markdown")

@dp.message_handler(commands=["find"], state=UserState.profile_complete)
async def cmd_find(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = await get_user(user_id)

    if not user or not (user["language"] and user["gender"] and user["continent"] and user["age"]):
        await message.answer("Please complete your profile first using /start.")
        return

    if await get_session(user_id):
        await message.answer("You are already in a chat session. Use /stop to end it.")
        return

    await message.answer("Searching for a partner...")

    # Simple matching logic (for now, just finds any available user not in session)
    # This needs to be more sophisticated for VIP filters
    active_users = await get_active_users()
    potential_partners = [u for u in active_users if u["id"] != user_id and not await get_session(u["id"])]

    if user["is_vip"]:
        # Implement VIP filtering here based on user preferences (not yet collected)
        # For now, VIP users also get random match
        pass

    if potential_partners:
        partner = random.choice(potential_partners)
        await create_session(user_id, partner["id"])
        await bot.send_message(user_id, f"Found a partner! Say hello to {partner[\'username\']}. Use /stop to end the chat.")
        await bot.send_message(partner["id"], f"Found a partner! Say hello to {user[\'username\']}. Use /stop to end the chat.")
        await UserState.in_session.set()
        async with state.proxy() as data:
            data["partner_id"] = partner["id"]
    else:
        await message.answer("No partners available right now. Please try again later.")

@dp.message_handler(commands=["stop"], state=UserState.in_session)
async def cmd_stop(message: types.Message, state: FSMContext):
    session = await end_session(message.from_user.id)
    if session:
        partner_id = session["user1_id"] if session["user2_id"] == message.from_user.id else session["user2_id"]
        await bot.send_message(message.from_user.id, "Chat session ended.")
        await bot.send_message(partner_id, "Your partner has ended the chat session.")
        await UserState.profile_complete.set()
        await state.finish()
    else:
        await message.answer("You are not in an active chat session.")

@dp.message_handler(commands=["ban"], state="*")
async def cmd_ban(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("You are not authorized to use this command.")
        return
    try:
        user_id_to_ban = int(message.get_args().split()[0])
        await ban_user(user_id_to_ban)
        await message.answer(f"User {user_id_to_ban} has been banned.")
    except (IndexError, ValueError):
        await message.answer("Usage: /ban <user_id>")

@dp.message_handler(commands=["unban"], state="*")
async def cmd_unban(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("You are not authorized to use this command.")
        return
    try:
        user_id_to_unban = int(message.get_args().split()[0])
        await unban_user(user_id_to_unban)
        await message.answer(f"User {user_id_to_unban} has been unbanned.")
    except (IndexError, ValueError):
        await message.answer("Usage: /unban <user_id>")

@dp.message_handler(commands=["vip"], state="*")
async def cmd_vip(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("You are not authorized to use this command.")
        return
    try:
        args = message.get_args().split()
        user_id_to_vip = int(args[0])
        months = int(args[1])
        await set_vip_status(user_id_to_vip, months)
        await message.answer(f"User {user_id_to_vip} has been granted VIP status for {months} months.")
    except (IndexError, ValueError):
        await message.answer("Usage: /vip <user_id> <months>")

@dp.message_handler(commands=["stats"], state="*")
async def cmd_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("You are not authorized to use this command.")
        return
    active_users = await get_active_users()
    num_active_users = len(active_users)
    await message.answer(f"Currently {num_active_users} active users.")

@dp.message_handler(content_types=types.ContentType.ANY, state=UserState.in_session)
async def handle_session_messages(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    session = await get_session(user_id)

    if session:
        partner_id = session["user1_id"] if session["user2_id"] == user_id else session["user2_id"]
        user_info = f"User:{message.from_user.id} @{message.from_user.username or f\'id_{message.from_user.id}\'}"
        partner_user = await get_user(partner_id)
        partner_info = f"Partner:{partner_user[\'id\']} @{partner_user[\'username\'] or f\'id_{partner_user[\'id\']}\'}"

        # Forward to partner
        if message.text:
            await bot.send_message(partner_id, message.text)
        elif message.photo:
            await bot.send_photo(partner_id, message.photo[-1].file_id, caption=message.caption)
        elif message.video:
            await bot.send_video(partner_id, message.video.file_id, caption=message.caption)
        elif message.document:
            await bot.send_document(partner_id, message.document.file_id, caption=message.caption)
        elif message.voice:
            await bot.send_voice(partner_id, message.voice.file_id, caption=message.caption)
        elif message.audio:
            await bot.send_audio(partner_id, message.audio.file_id, caption=message.caption)
        elif message.sticker:
            await bot.send_sticker(partner_id, message.sticker.file_id)
        elif message.animation:
            await bot.send_animation(partner_id, message.animation.file_id, caption=message.caption)
        elif message.video_note:
            await bot.send_video_note(partner_id, message.video_note.file_id)
        elif message.contact:
            await bot.send_contact(partner_id, message.contact.phone_number, message.contact.first_name, last_name=message.contact.last_name)
        elif message.location:
            await bot.send_location(partner_id, message.location.latitude, message.location.longitude)
        elif message.poll:
            await bot.send_poll(partner_id, message.poll.question, [o.text for o in message.poll.options], is_anonymous=message.poll.is_anonymous, type=message.poll.type, allows_multiple_answers=message.poll.allows_multiple_answers, correct_option_id=message.poll.correct_option_id, explanation=message.poll.explanation, open_period=message.poll.open_period, close_date=message.poll.close_date)
        elif message.venue:
            await bot.send_venue(partner_id, message.venue.latitude, message.venue.longitude, message.venue.title, message.venue.address, foursquare_id=message.venue.foursquare_id, foursquare_type=message.venue.foursquare_type)

        # Forward to target group
        if message.text:
            await bot.send_message(
                chat_id=TARGET_GROUP_ID,
                text=f"{user_info} → {partner_info}:\n{message.text}"
            )
        else:
            # For media, forward the original message to the target group
            await bot.forward_message(
                chat_id=TARGET_GROUP_ID,
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )
    else:
        await message.answer("You are not in an active chat session. Use /find to start one.")

@dp.message_handler(content_types=types.ContentType.ANY, state="*")
async def handle_all_messages(message: types.Message):
    # This handler catches messages when not in a specific state or session
    await message.answer("I don\'t understand that command. Please use /start to begin or /help for available commands.")


async def on_startup(dp):
    await create_db_pool()
    await create_tables()
    logging.info("Bot started and database connected!")
    WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL")
    WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
    WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
    await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"Webhook set to {WEBHOOK_URL}")

async def on_shutdown(dp):
    logging.warning(\'Shutting down..\')
    await bot.delete_webhook()
    await dp.storage.close()
    await dp.storage.wait_closed()
    logging.warning(\'Bye!\')

if __name__ == '__main__':
    webhooks.start_webhook(
        dispatcher=dp,
        webhook_path=f"/webhook/{BOT_TOKEN}",
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True,
        host=\'0.0.0.0\',
        port=int(os.getenv(\'PORT\', 8080))
    )


