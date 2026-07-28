# O'zbek AI Telegram Bot

Telegram orqali ishlaydigan, Groq API (Llama 3.3 70B) asosidagi bepul AI chat bot.

## 1-qadam: Telegram bot yaratish

1. Telegramda [@BotFather](https://t.me/BotFather) ga yozing
2. `/newbot` buyrug'ini yuboring, nom va username bering
3. Sizga beriladigan **TOKEN**ni saqlab qo'ying

## 2-qadam: Groq API kalitini olish

1. https://console.groq.com/keys ga kiring (email bilan bepul ro'yxatdan o'ting)
2. "Create API Key" tugmasini bosing va kalitni saqlang

## 3-qadam: Railway'ga joylashtirish

1. https://railway.app ga GitHub akkaunt bilan kiring
2. Bu papkani (`uzbek-ai-bot`) o'zingizning GitHub repo'ingizga yuklang
3. Railway'da "New Project" → "Deploy from GitHub repo" tanlang
4. Loyihangizni tanlang
5. **Variables** bo'limiga kirib, quyidagi ikkita o'zgaruvchini qo'shing:
   - `TELEGRAM_TOKEN` — BotFather'dan olgan token
   - `GROQ_API_KEY` — Groq'dan olgan kalit
6. Railway avtomatik ravishda `Procfile`ni o'qib botni ishga tushiradi

Shu bilan bot 24/7 ishlaydi. Telegramda botingizga `/start` yozib sinab ko'ring.

## Mahalliy kompyuterda sinash (ixtiyoriy)

```bash
pip install -r requirements.txt
export TELEGRAM_TOKEN="sizning_tokeningiz"
export GROQ_API_KEY="sizning_kalitingiz"
python bot.py
```

## Keyingi qadamlar (ixtiyoriy yaxshilashlar)

- Suhbat tarixini xotirada emas, ma'lumotlar bazasida (masalan, SQLite yoki Redis) saqlash — server qayta ishga tushganda tarix o'chib ketmasligi uchun
- Rasm yoki ovozli xabarlarni qabul qilish (Groq Whisper orqali)
- Foydalanuvchilar sonini cheklash yoki obuna tizimi qo'shish
- Keyinroq shu botni mobil ilova yoki web interfeysga ulash
