import os
import re
import time
import httpx
import sqlite3
import logging
from io import BytesIO
from urllib.parse import quote
from datetime import date
from collections import defaultdict

from groq import Groq
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# ---------- SOZLAMALAR ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
MODEL = "qwen/qwen3.6-27b"  # matn va rasm bilan ishlaydigan yangi model (llama-3.3-70b eskirgani uchun)
MAX_HISTORY = 10  # har bir foydalanuvchi uchun saqlanadigan xabarlar soni

CHANNEL_USERNAME = "@FoydaliWebSahifalar"  # majburiy obuna uchun kanal
CHANNEL_URL = "https://t.me/FoydaliWebSahifalar"

ADMIN_ID = int(os.environ.get("ADMIN_ID", "7953346705"))  # statistika ko'ra oladigan admin Telegram ID'si
DB_PATH = "/data/stats.db"  # Railway Volume orqali doimiy saqlanadi

SYSTEM_PROMPT = (
    "Sen o'zbek tilida gaplashadigan foydali AI yordamchisan. "
    "Har doim o'zbek tilida, sodda va tushunarli tilda javob ber. "
    "Agar foydalanuvchi boshqa tilda yozsa, o'sha tilda javob berishing mumkin. "
    "Javoblaring qisqa, aniq va do'stona bo'lsin."
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

groq_client = Groq(api_key=GROQ_API_KEY)

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

    return "\n".join(lines)


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
        # Xatolik bo'lsa (masalan bot admin emas), foydalanuvchini bloklamaymiz
        return True


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_subscribed(context, update.effective_user.id):
        await send_subscribe_prompt(update)
        return

    user_histories[update.effective_user.id] = []
    await update.message.reply_text(
        "Assalomu alaykum! 👋\n"
        "Men sizning AI yordamchingizman. Menga istalgan savolingizni yozing.\n\n"
        "🎤 Ovozli xabar yuborishingiz ham mumkin\n"
        "🖼️ Rasm yuborsangiz, uni tavsiflab beraman\n"
        "🎨 /rasm <tavsif> — rasm chizib beraman\n"
        "/reset — suhbatni tozalash uchun."
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_histories[update.effective_user.id] = []
    await update.message.reply_text("Suhbat tarixi tozalandi. Yangidan boshlaymiz! 🔄")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return  # admin bo'lmagan foydalanuvchiga hech narsa demaymiz
    await update.message.reply_text(get_stats())


MIN_EDIT_INTERVAL = 0.12  # ikkita tahrirlash orasidagi eng kam vaqt (flood limitdan saqlanish uchun)
CHAR_STEP = 15  # shuncha yangi belgi to'planganda darhol yangilaymiz
TYPING_CURSOR = " ▌"  # "yozilyapti" effekti uchun kursor belgisi

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_thinking(raw: str) -> str:
    """Modelning ichki 'fikrlash' (<think>...</think>) qismini foydalanuvchiga ko'rsatmaslik uchun olib tashlaydi."""
    cleaned = THINK_RE.sub("", raw)
    # Agar hali yopilmagan <think> tegi bo'lsa (stream davom etayotganda), o'sha joygacha kesib tashlaymiz
    idx = cleaned.find("<think>")
    if idx != -1:
        cleaned = cleaned[:idx]
    return cleaned.strip()


async def _stream_to_telegram(update: Update, context: ContextTypes.DEFAULT_TYPE, messages: list[dict]) -> str:
    """Berilgan messages ro'yxatini Groq'ga yuboradi va javobni bosqichma-bosqich Telegram'da ko'rsatadi."""
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # Bo'sh xabar bilan boshlaymiz, keyin uni tahrirlab boramiz
    sent_message = await update.effective_message.reply_text("⏳")

    raw_text = ""
    last_edit_time = 0.0
    last_edit_len = 0

    try:
        stream = groq_client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=800,
            stream=True,
            reasoning_effort="none",  # "fikrlash" rejimini o'chiramiz — tezkor, toza javob uchun
        )

        for chunk in stream:
            delta = chunk.choices[0].delta.content or ""
            if not delta:
                continue
            raw_text += delta
            display_text = strip_thinking(raw_text)

            now = time.monotonic()
            enough_time_passed = (now - last_edit_time) >= MIN_EDIT_INTERVAL
            enough_new_chars = (len(display_text) - last_edit_len) >= CHAR_STEP

            if enough_time_passed and enough_new_chars and display_text:
                last_edit_time = now
                last_edit_len = len(display_text)
                try:
                    await sent_message.edit_text(display_text + TYPING_CURSOR)
                except BadRequest:
                    pass  # matn o'zgarmagan bo'lsa yoki flood bo'lsa, e'tiborsiz qoldiramiz

        full_text = strip_thinking(raw_text)
        if not full_text:
            full_text = "Kechirasiz, javob bera olmadim. Qayta urinib ko'ring. 🙏"

    except Exception as e:
        logger.error(f"Groq xatosi: {e}")
        full_text = "Kechirasiz, hozir javob bera olmadim. Birozdan so'ng qayta urinib ko'ring. 🙏"

    # Yakuniy to'liq matnni (kursorsiz) yuboramiz
    try:
        await sent_message.edit_text(full_text)
    except BadRequest:
        pass

    return full_text


async def stream_ai_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, user_text: str):
    """Matnli xabar uchun: suhbat tarixini hisobga olib javob beradi."""
    record_message(user_id, update.effective_user.username)

    history = user_histories[user_id]
    history.append({"role": "user", "content": user_text})
    history[:] = history[-MAX_HISTORY:]  # tarixni cheklab turamiz

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history
    full_text = await _stream_to_telegram(update, context, messages)
    history.append({"role": "assistant", "content": full_text})


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
        first_name = update.effective_user.first_name or ""
        hint_prompt = "Bu o'zbek tilidagi ovozli xabar."
        if first_name:
            hint_prompt += f" Gapiruvchining ismi: {first_name}."

        with open(ogg_path, "rb") as f:
            transcript = groq_client.audio.transcriptions.create(
                file=(os.path.basename(ogg_path), f.read()),
                model="whisper-large-v3",
                language="uz",
                prompt=hint_prompt,
                temperature=0,
            )
        recognized_text = transcript.text.strip()
    except Exception as e:
        logger.error(f"Whisper xatosi: {e}")
        await update.message.reply_text(
            "Kechirasiz, ovozli xabarni tushuna olmadim. Matn bilan yozib ko'ring. 🙏"
        )
        return
    finally:
        if os.path.exists(ogg_path):
            os.remove(ogg_path)

    if not recognized_text:
        await update.message.reply_text(
            "Ovozli xabarni tushuna olmadim, iltimos qayta urinib ko'ring yoki matn yozing. 🙏"
        )
        return

    await stream_ai_reply(update, context, user_id, recognized_text)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not await is_subscribed(context, user_id):
        await send_subscribe_prompt(update)
        return

    record_message(user_id, update.effective_user.username)

    # Eng yuqori sifatli nusxasini olamiz
    photo = update.message.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    image_url = tg_file.file_path  # Telegram to'liq havolani qaytaradi

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

    # Suhbat tarixiga qisqacha yozuv sifatida qo'shamiz (rasmning o'zini emas)
    history = user_histories[user_id]
    history.append({"role": "user", "content": f"[Rasm yubordi] {question}"})
    history.append({"role": "assistant", "content": full_text})
    history[:] = history[-MAX_HISTORY:]


IMAGE_GEN_TIMEOUT = 180.0  # yuqori sifatli katta rasm uchun 2-3 daqiqagacha kutamiz


def enhance_image_prompt(user_prompt: str) -> str:
    """Foydalanuvchining (o'zbekcha) qisqa tavsifini AI orqali batafsil, aniq inglizcha
    rasm-generatsiya promptiga aylantiradi. Xatolik bo'lsa, asl matnni qaytaradi."""
    try:
        response = groq_client.chat.completions.create(
            model=MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You convert short user requests (possibly in Uzbek) into a single, "
                        "detailed English prompt for an AI image generator.\n\n"
                        "STRICT RULES:\n"
                        "- Keep EXACTLY the subjects and scene the user described — do not add "
                        "new objects, characters, props, or scene elements that weren't mentioned "
                        "or clearly implied.\n"
                        "- You may ONLY add: art style, lighting, color mood, camera framing, and "
                        "quality descriptors (e.g. 'high detail', 'soft lighting', 'digital art').\n"
                        "- Do not reinterpret or embellish the scene creatively — stay literal.\n"
                        "- Respond with ONLY the final English prompt, nothing else — "
                        "no explanations, no quotes, no extra text."
                    ),
                },
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.8,
            max_tokens=200,
            reasoning_effort="none",
        )
        enhanced = response.choices[0].message.content or ""
        enhanced = strip_thinking(enhanced).strip()
        return enhanced if enhanced else user_prompt
    except Exception as e:
        logger.error(f"Promptni yaxshilashda xatolik: {e}")
        return user_prompt


async def generate_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not await is_subscribed(context, user_id):
        await send_subscribe_prompt(update)
        return

    prompt = " ".join(context.args).strip() if context.args else ""
    if not prompt:
        await update.message.reply_text(
            "🎨 Rasm yaratish uchun tavsif yozing.\n\n"
            "Masalan: /rasm qor bosgan tog'lar orasidagi kichik uy"
        )
        return

    record_message(user_id, update.effective_user.username)

    status_message = await update.message.reply_text(
        "🎨 Yuqori sifatli rasm chizilmoqda, bu 1-2 daqiqa vaqt olishi mumkin..."
    )
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")

    enhanced_prompt = enhance_image_prompt(prompt)
    image_url = (
        f"https://image.pollinations.ai/prompt/{quote(enhanced_prompt)}"
        f"?width=2048&height=2048&model=flux&enhance=true&nologo=true"
    )

    try:
        async with httpx.AsyncClient(timeout=IMAGE_GEN_TIMEOUT) as client:
            response = await client.get(image_url)
            response.raise_for_status()
            image_bytes = response.content

        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=BytesIO(image_bytes),
            caption=f"🎨 {prompt}",
        )
        await status_message.delete()

    except Exception as e:
        logger.error(f"Rasm yaratishda xatolik: {e}")
        try:
            await status_message.edit_text(
                "Kechirasiz, rasm yaratib bo'lmadi. Birozdan so'ng qayta urinib ko'ring. 🙏"
            )
        except BadRequest:
            pass


def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("rasm", generate_image))
    app.add_handler(CallbackQueryHandler(check_subscription_callback, pattern="^check_sub$"))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()
