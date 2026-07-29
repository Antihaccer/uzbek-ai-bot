import os
import time
import logging
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
MODEL = "llama-3.3-70b-versatile"  # Groq'dagi kuchli bepul model
MAX_HISTORY = 10  # har bir foydalanuvchi uchun saqlanadigan xabarlar soni

CHANNEL_USERNAME = "@FoydaliWebSahifalar"  # majburiy obuna uchun kanal
CHANNEL_URL = "https://t.me/FoydaliWebSahifalar"

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
        "/reset — suhbatni tozalash uchun."
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_histories[update.effective_user.id] = []
    await update.message.reply_text("Suhbat tarixi tozalandi. Yangidan boshlaymiz! 🔄")


MIN_EDIT_INTERVAL = 0.12  # ikkita tahrirlash orasidagi eng kam vaqt (flood limitdan saqlanish uchun)
CHAR_STEP = 15  # shuncha yangi belgi to'planganda darhol yangilaymiz
TYPING_CURSOR = " ▌"  # "yozilyapti" effekti uchun kursor belgisi


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not await is_subscribed(context, user_id):
        await send_subscribe_prompt(update)
        return

    user_text = update.message.text

    history = user_histories[user_id]
    history.append({"role": "user", "content": user_text})
    history[:] = history[-MAX_HISTORY:]  # tarixni cheklab turamiz

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # Bo'sh xabar bilan boshlaymiz, keyin uni tahrirlab boramiz
    sent_message = await update.message.reply_text("⏳")

    full_text = ""
    last_edit_time = 0.0
    last_edit_len = 0

    try:
        stream = groq_client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=800,
            stream=True,
        )

        for chunk in stream:
            delta = chunk.choices[0].delta.content or ""
            if not delta:
                continue
            full_text += delta

            now = time.monotonic()
            enough_time_passed = (now - last_edit_time) >= MIN_EDIT_INTERVAL
            enough_new_chars = (len(full_text) - last_edit_len) >= CHAR_STEP

            if enough_time_passed and enough_new_chars:
                last_edit_time = now
                last_edit_len = len(full_text)
                try:
                    await sent_message.edit_text(full_text + TYPING_CURSOR)
                except BadRequest:
                    pass  # matn o'zgarmagan bo'lsa yoki flood bo'lsa, e'tiborsiz qoldiramiz

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

    history.append({"role": "assistant", "content": full_text})


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CallbackQueryHandler(check_subscription_callback, pattern="^check_sub$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()
