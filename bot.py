import os
import time
import json
import sqlite3
import logging
from io import BytesIO
from datetime import date
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, WebAppInfo, MenuButtonWebApp
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from ai_core import AICore

# ---------- SOZLAMALAR ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
MODEL = "qwen/qwen3.6-27b"  # matn va rasm bilan ishlaydigan yangi model — Groq (zaxira)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-3.1-flash-lite"  # bepul tarifda, yangi API kalitlar uchun ochiq model — asosiy
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

MAX_HISTORY = 10  # har bir foydalanuvchi uchun saqlanadigan xabarlar soni

CHANNEL_USERNAME = "@FoydaliWebSahifalar"  # majburiy obuna uchun kanal
CHANNEL_URL = "https://t.me/FoydaliWebSahifalar"

ADMIN_ID = int(os.environ.get("ADMIN_ID", "7953346705"))  # statistika ko'ra oladigan admin Telegram ID'si
DB_PATH = "/data/stats.db"  # Railway Volume orqali doimiy saqlanadi

CLOUDFLARE_ACCOUNT_ID = os.environ["CLOUDFLARE_ACCOUNT_ID"]
CLOUDFLARE_API_TOKEN = os.environ["CLOUDFLARE_API_TOKEN"]
CF_IMAGE_MODEL = "@cf/black-forest-labs/flux-1-schnell"

MINI_APP_URL = os.environ.get("MINI_APP_URL", "")  # Railway domeni, masalan https://xxx.up.railway.app

SYSTEM_PROMPT = (
    "Sen o'zbek tilida gaplashadigan foydali AI yordamchisan. "
    "Har doim o'zbek tilida, sodda va tushunarli tilda javob ber. "
    "Agar foydalanuvchi boshqa tilda yozsa, o'sha tilda javob berishing mumkin. "
    "Javoblaring qisqa, aniq va do'stona bo'lsin."
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- AI YADROSI (Gemini -> Groq, rasm, ovoz) ----------
ai = AICore(
    groq_api_key=GROQ_API_KEY,
    gemini_api_key=GEMINI_API_KEY,
    model=MODEL,
    gemini_model=GEMINI_MODEL,
    gemini_base_url=GEMINI_BASE_URL,
    cf_account_id=CLOUDFLARE_ACCOUNT_ID,
    cf_api_token=CLOUDFLARE_API_TOKEN,
    cf_image_model=CF_IMAGE_MODEL,
    system_prompt=SYSTEM_PROMPT,
)

# Har bir foydalanuvchi uchun alohida suhbat tarixi (xotirada saqlanadi)
user_histories: dict[int, list[dict]] = defaultdict(list)


# ---------- STATISTIKA (SQLite) ----------
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_seen TEXT,
            message_count INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_messages (
            day TEXT,
            user_id INTEGER,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (day, user_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversation_history (
            user_id INTEGER PRIMARY KEY,
            history TEXT
        )
    """)
    conn.commit()
    conn.close()


def load_all_histories():
    """Bot ishga tushganda, oldingi suhbat tarixlarini bazadan xotiraga yuklaydi."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT user_id, history FROM conversation_history").fetchall()
    conn.close()
    for user_id, history_json in rows:
        try:
            user_histories[user_id] = json.loads(history_json)
        except (json.JSONDecodeError, TypeError):
            continue
    if rows:
        logger.info(f"{len(rows)} ta foydalanuvchi suhbat tarixi bazadan yuklandi.")


def save_history(user_id: int):
    """Foydalanuvchining suhbat tarixini bazaga yozadi (doimiy saqlash uchun)."""
    history_json = json.dumps(user_histories[user_id], ensure_ascii=False)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO conversation_history (user_id, history)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET history = excluded.history
        """,
        (user_id, history_json),
    )
    conn.commit()
    conn.close()


def delete_history(user_id: int):
    """Foydalanuvchining suhbat tarixini bazadan o'chiradi (/reset uchun)."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM conversation_history WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def record_message(user_id: int, username: str | None):
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO users (user_id, username, first_seen, message_count)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(user_id) DO UPDATE SET
            message_count = message_count + 1,
            username = excluded.username
        """,
        (user_id, username, today),
    )
    conn.execute(
        """
        INSERT INTO daily_messages (day, user_id, count)
        VALUES (?, ?, 1)
        ON CONFLICT(day, user_id) DO UPDATE SET count = count + 1
        """,
        (today, user_id),
    )
    conn.commit()
    conn.close()


def get_stats() -> str:
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_messages = conn.execute("SELECT COALESCE(SUM(message_count), 0) FROM users").fetchone()[0]
    active_today = conn.execute(
        "SELECT COUNT(DISTINCT user_id) FROM daily_messages WHERE day = ?", (today,)
    ).fetchone()[0]
    messages_today = conn.execute(
        "SELECT COALESCE(SUM(count), 0) FROM daily_messages WHERE day = ?", (today,)
    ).fetchone()[0]
    new_today = conn.execute(
        "SELECT COUNT(*) FROM users WHERE first_seen = ?", (today,)
    ).fetchone()[0]
    top_users = conn.execute(
        "SELECT user_id, username, message_count FROM users ORDER BY message_count DESC LIMIT 5"
    ).fetchall()
    newest_users = conn.execute(
        "SELECT user_id, username, first_seen FROM users ORDER BY first_seen DESC, user_id DESC LIMIT 5"
    ).fetchall()
    conn.close()

    lines = [
        "📊 Bot statistikasi",
        "",
        f"👥 Jami foydalanuvchilar: {total_users}",
        f"✉️ Jami xabarlar: {total_messages}",
        "",
        f"📅 Bugun faol: {active_today}",
        f"📅 Bugungi xabarlar: {messages_today}",
        f"🆕 Bugun qo'shilgan yangi: {new_today}",
        "",
        "🏆 Eng faol 5 foydalanuvchi:",
    ]
    for i, (uid, uname, count) in enumerate(top_users, start=1):
        name = f"@{uname}" if uname else f"ID:{uid}"
        lines.append(f"{i}. {name} — {count} xabar")

    lines.append("")
    lines.append("🆕 Yangi qo'shilgan 5 foydalanuvchi:")
    for i, (uid, uname, first_seen) in enumerate(newest_users, start=1):
        name = f"@{uname}" if uname else f"ID:{uid}"
        lines.append(f"{i}. {name} — {first_seen}")

    return "\n".join(lines)


# ---------- OBUNA TEKSHIRUVI ----------
def subscribe_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Kanalga o'tish", url=CHANNEL_URL)],
        [InlineKeyboardButton("✅ Tekshirish", callback_data="check_sub")],
    ])


async def is_subscribed(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.error(f"Obunani tekshirishda xatolik: {e}")
        return True  # xatolik bo'lsa, foydalanuvchini bloklamaymiz


async def send_subscribe_prompt(update: Update):
    await update.effective_message.reply_text(
        "🚫 Botdan foydalanish uchun avval kanalimizga obuna bo'ling:\n\n"
        f"{CHANNEL_URL}\n\n"
        "Obuna bo'lgach, pastdagi \"✅ Tekshirish\" tugmasini bosing.",
        reply_markup=subscribe_keyboard(),
    )


async def check_subscription_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    if await is_subscribed(context, user_id):
        await query.answer("Obuna tasdiqlandi! ✅")
        await query.edit_message_text(
            "Rahmat! Endi botdan bemalol foydalanishingiz mumkin. 🎉\n\n"
            "Menga istalgan savolingizni yozing."
        )
    else:
        await query.answer("Siz hali kanalga obuna bo'lmagansiz. ❌", show_alert=True)


# ---------- ASOSIY BUYRUQLAR ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_subscribed(context, update.effective_user.id):
        await send_subscribe_prompt(update)
        return

    text = (
        "Assalomu alaykum! 👋\n"
        "Men sizning AI yordamchingizman. Menga istalgan savolingizni yozing.\n\n"
        "🎤 Ovozli xabar yuborishingiz ham mumkin\n"
        "🖼️ Rasm yuborsangiz, uni tavsiflab beraman\n"
        "🎨 /rasm <tavsif> — rasm chizib beraman\n"
        "/reset — suhbatni tozalash uchun."
    )

    if MINI_APP_URL:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✨ To'liq ilovada ochish", web_app=WebAppInfo(url=MINI_APP_URL))]
        ])
        await update.message.reply_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_histories[update.effective_user.id] = []
    delete_history(update.effective_user.id)
    await update.message.reply_text("Suhbat tarixi tozalandi. Yangidan boshlaymiz! 🔄")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(get_stats())


# ---------- STREAMING JAVOB (Telegram xabarini tahrirlab boradi) ----------
MIN_EDIT_INTERVAL = 0.12
CHAR_STEP = 15
TYPING_CURSOR = " ▌"


async def _stream_to_telegram(update: Update, context: ContextTypes.DEFAULT_TYPE, messages: list[dict]) -> str:
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    sent_message = await update.effective_message.reply_text("⏳")

    state = {"last_edit_time": 0.0, "last_edit_len": 0}

    async def on_chunk(display_text: str):
        now = time.monotonic()
        enough_time = (now - state["last_edit_time"]) >= MIN_EDIT_INTERVAL
        enough_chars = (len(display_text) - state["last_edit_len"]) >= CHAR_STEP
        if enough_time and enough_chars:
            state["last_edit_time"] = now
            state["last_edit_len"] = len(display_text)
            try:
                await sent_message.edit_text(display_text + TYPING_CURSOR)
            except BadRequest:
                pass

    async def on_total_failure(provider_name: str, error: Exception):
        await notify_admin(
            context,
            f"🚨 Barcha AI provayderlar ishlamayapti!\n\nOxirgi xatolik ({provider_name}): {error}",
        )

    full_text, _ = await ai.stream_reply(messages, on_chunk=on_chunk, on_total_failure=on_total_failure)

    try:
        await sent_message.edit_text(full_text)
    except BadRequest:
        pass

    return full_text


async def stream_ai_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, user_text: str):
    record_message(user_id, update.effective_user.username)

    history = user_histories[user_id]
    history.append({"role": "user", "content": user_text})
    history[:] = history[-MAX_HISTORY:]

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history
    full_text = await _stream_to_telegram(update, context, messages)
    history.append({"role": "assistant", "content": full_text})
    save_history(user_id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_subscribed(context, user_id):
        await send_subscribe_prompt(update)
        return
    await stream_ai_reply(update, context, user_id, update.message.text)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_subscribed(context, user_id):
        await send_subscribe_prompt(update)
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    voice = update.message.voice or update.message.audio
    tg_file = await context.bot.get_file(voice.file_id)
    ogg_path = f"/tmp/voice_{user_id}_{int(time.time())}.ogg"
    await tg_file.download_to_drive(ogg_path)

    try:
        with open(ogg_path, "rb") as f:
            file_bytes = f.read()
        recognized_text = ai.transcribe_audio(
            file_bytes, os.path.basename(ogg_path), update.effective_user.first_name or ""
        )
    except Exception as e:
        logger.error(f"Whisper xatosi: {e}")
        await update.message.reply_text("Kechirasiz, ovozli xabarni tushuna olmadim. Matn bilan yozib ko'ring. 🙏")
        return
    finally:
        if os.path.exists(ogg_path):
            os.remove(ogg_path)

    if not recognized_text:
        await update.message.reply_text("Ovozli xabarni tushuna olmadim, iltimos qayta urinib ko'ring yoki matn yozing. 🙏")
        return

    await stream_ai_reply(update, context, user_id, recognized_text)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_subscribed(context, user_id):
        await send_subscribe_prompt(update)
        return

    record_message(user_id, update.effective_user.username)

    photo = update.message.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    image_url = tg_file.file_path

    caption = (update.message.caption or "").strip()
    question = caption if caption else "Bu rasmda nima ko'rsatilgan? Batafsil, o'zbek tilida tushuntirib ber."

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        },
    ]

    full_text = await _stream_to_telegram(update, context, messages)

    history = user_histories[user_id]
    history.append({"role": "user", "content": f"[Rasm yubordi] {question}"})
    history.append({"role": "assistant", "content": full_text})
    history[:] = history[-MAX_HISTORY:]
    save_history(user_id)


async def generate_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_subscribed(context, user_id):
        await send_subscribe_prompt(update)
        return

    prompt = " ".join(context.args).strip() if context.args else ""
    if not prompt:
        await update.message.reply_text(
            "🎨 Rasm yaratish uchun tavsif yozing.\n\nMasalan: /rasm qor bosgan tog'lar orasidagi kichik uy"
        )
        return

    record_message(user_id, update.effective_user.username)

    status_message = await update.message.reply_text("🎨 Rasm chizilmoqda, biroz kuting...")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")

    try:
        image_bytes = await ai.generate_image_bytes(prompt)
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=BytesIO(image_bytes),
            caption=f"🎨 {prompt}",
        )
        await status_message.delete()
    except Exception as e:
        logger.error(f"Rasm yaratishda xatolik: {e}")
        await notify_admin(context, f"🚨 Rasm yaratishda xatolik (Cloudflare)!\n\n{e}")
        try:
            await status_message.edit_text("Kechirasiz, rasm yaratib bo'lmadi. Birozdan so'ng qayta urinib ko'ring. 🙏")
        except BadRequest:
            pass


# ---------- ADMIN VA XATOLIKLAR ----------
async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str):
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=text[:4000])
    except Exception as e:
        logger.error(f"Admin'ga xabar yuborib bo'lmadi: {e}")


async def global_error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Kutilmagan xatolik yuz berdi:", exc_info=context.error)
    error_text = f"⚠️ Botda xatolik!\n\n{type(context.error).__name__}: {context.error}"
    if isinstance(update, Update) and update.effective_user:
        u = update.effective_user
        error_text += f"\n\n👤 Foydalanuvchi: {u.id} (@{u.username or 'username yoq'})"
    await notify_admin(context, error_text)


async def setup_commands_and_menu(app):
    """Buyruqlar ro'yxatini va (agar mavjud bo'lsa) Mini App tugmasini sozlaydi."""
    await app.bot.set_my_commands([
        BotCommand("start", "Botni ishga tushirish"),
        BotCommand("reset", "Suhbat tarixini tozalash"),
        BotCommand("rasm", "AI orqali rasm chizish"),
    ])
    if MINI_APP_URL:
        await app.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Uzbek AI", web_app=WebAppInfo(url=MINI_APP_URL))
        )
        logger.info(f"Mini App menyu tugmasi sozlandi: {MINI_APP_URL}")
    else:
        logger.info("MINI_APP_URL sozlanmagan — Mini App tugmasi ko'rsatilmaydi.")


def build_application():
    """Telegram Application obyektini yaratadi va handlerlarni ro'yxatga oladi."""
    init_db()
    load_all_histories()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(setup_commands_and_menu).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("rasm", generate_image))
    app.add_handler(CallbackQueryHandler(check_subscription_callback, pattern="^check_sub$"))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(global_error_handler)
    return app


def main():
    app = build_application()
    logger.info("Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()
