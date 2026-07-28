import os
import time
import logging
from collections import defaultdict

from groq import Groq
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)

# ---------- SOZLAMALAR ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
MODEL = "llama-3.3-70b-versatile"  # Groq'dagi kuchli bepul model
MAX_HISTORY = 10  # har bir foydalanuvchi uchun saqlanadigan xabarlar soni

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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()
