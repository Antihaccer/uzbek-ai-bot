import os
import logging
from collections import defaultdict

from groq import Groq
from telegram import Update
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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text

    history = user_histories[user_id]
    history.append({"role": "user", "content": user_text})
    history[:] = history[-MAX_HISTORY:]  # tarixni cheklab turamiz

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        response = groq_client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.7,
            max_tokens=800,
        )
        answer = response.choices[0].message.content
    except Exception as e:
        logger.error(f"Groq xatosi: {e}")
        answer = "Kechirasiz, hozir javob bera olmadim. Birozdan so'ng qayta urinib ko'ring. 🙏"

    history.append({"role": "assistant", "content": answer})
    await update.message.reply_text(answer)


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()
