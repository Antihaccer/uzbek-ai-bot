"""
Telegram Mini App uchun FastAPI backend.
Bot bilan bir xil AI yadrosi (ai_core.AICore) va bir xil suhbat tarixini ishlatadi,
shunda Telegram chat va Mini App orasida kontekst uzilmaydi.
"""
import os
import json
import time
import hmac
import base64
import hashlib
import logging
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from telegram import Bot

import bot as bot_module  # ai, user_histories, save_history, record_message va h.k.

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
_bot_instance: Bot | None = None


def get_bot() -> Bot:
    global _bot_instance
    if _bot_instance is None:
        _bot_instance = Bot(token=bot_module.TELEGRAM_TOKEN)
    return _bot_instance


def validate_init_data(init_data: str) -> dict | None:
    """Telegram Mini App initData'ni tekshiradi (HMAC-SHA256). Muvaffaqiyatli bo'lsa user ma'lumotini qaytaradi."""
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = pairs.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret_key = hmac.new(b"WebAppData", bot_module.TELEGRAM_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(computed_hash, received_hash):
            logger.warning("initData hash mos kelmadi — soxta so'rov bo'lishi mumkin.")
            return None

        # 24 soatdan eski so'rovlarni rad etamiz (replay hujumidan himoya)
        auth_date = int(pairs.get("auth_date", "0"))
        if time.time() - auth_date > 86400:
            return None

        user_json = pairs.get("user")
        if not user_json:
            return None
        return json.loads(user_json)
    except Exception as e:
        logger.error(f"initData tekshirishda xatolik: {e}")
        return None


async def check_subscribed(user_id: int) -> bool:
    try:
        member = await get_bot().get_chat_member(chat_id=bot_module.CHANNEL_USERNAME, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.error(f"Obunani tekshirishda xatolik (web): {e}")
        return True


def create_web_app() -> FastAPI:
    app = FastAPI()

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    if (STATIC_DIR / "assets").exists():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.post("/api/session")
    async def session(request: Request):
        """Foydalanuvchini tasdiqlaydi, obuna holatini va suhbat tarixini qaytaradi."""
        body = await request.json()
        user = validate_init_data(body.get("init_data", ""))
        if not user:
            return JSONResponse({"error": "invalid_init_data"}, status_code=401)

        user_id = user["id"]
        subscribed = await check_subscribed(user_id)
        history = bot_module.user_histories.get(user_id, [])

        return JSONResponse({
            "user": {
                "id": user_id,
                "first_name": user.get("first_name", ""),
                "username": user.get("username", ""),
            },
            "subscribed": subscribed,
            "channel_url": bot_module.CHANNEL_URL,
            "history": history,
        })

    @app.post("/api/chat")
    async def chat(request: Request):
        """Matnli xabarga SSE orqali bosqichma-bosqich javob qaytaradi."""
        body = await request.json()
        user = validate_init_data(body.get("init_data", ""))
        if not user:
            return JSONResponse({"error": "invalid_init_data"}, status_code=401)

        user_id = user["id"]
        user_text = (body.get("message") or "").strip()
        if not user_text:
            return JSONResponse({"error": "empty_message"}, status_code=400)

        if not await check_subscribed(user_id):
            return JSONResponse({"error": "not_subscribed", "channel_url": bot_module.CHANNEL_URL}, status_code=403)

        bot_module.record_message(user_id, user.get("username"))

        history = bot_module.user_histories[user_id]
        history.append({"role": "user", "content": user_text})
        history[:] = history[-bot_module.MAX_HISTORY:]
        messages = [{"role": "system", "content": bot_module.SYSTEM_PROMPT}] + history

        return StreamingResponse(_sse_stream(messages, user_id), media_type="text/event-stream")

    @app.post("/api/voice")
    async def voice(request: Request):
        """Ovoz faylini matnga o'giradi (keyin frontend shu matnni /api/chat'ga yuboradi)."""
        form = await request.form()
        init_data = form.get("init_data", "")
        user = validate_init_data(init_data)
        if not user:
            return JSONResponse({"error": "invalid_init_data"}, status_code=401)

        if not await check_subscribed(user["id"]):
            return JSONResponse({"error": "not_subscribed"}, status_code=403)

        audio_file = form.get("audio")
        if audio_file is None:
            return JSONResponse({"error": "no_audio"}, status_code=400)

        file_bytes = await audio_file.read()
        try:
            text = bot_module.ai.transcribe_audio(file_bytes, "voice.ogg", user.get("first_name", ""))
        except Exception as e:
            logger.error(f"Whisper xatosi (web): {e}")
            return JSONResponse({"error": "transcription_failed"}, status_code=500)

        return JSONResponse({"transcript": text})

    @app.post("/api/image")
    async def image(request: Request):
        """Tavsif asosida rasm yaratadi, natijani base64 formatida qaytaradi."""
        body = await request.json()
        user = validate_init_data(body.get("init_data", ""))
        if not user:
            return JSONResponse({"error": "invalid_init_data"}, status_code=401)

        if not await check_subscribed(user["id"]):
            return JSONResponse({"error": "not_subscribed"}, status_code=403)

        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return JSONResponse({"error": "empty_prompt"}, status_code=400)

        bot_module.record_message(user["id"], user.get("username"))

        try:
            image_bytes = await bot_module.ai.generate_image_bytes(prompt)
        except Exception as e:
            logger.error(f"Rasm yaratishda xatolik (web): {e}")
            return JSONResponse({"error": "image_generation_failed"}, status_code=500)

        image_b64 = base64.b64encode(image_bytes).decode()
        return JSONResponse({"image_base64": image_b64, "mime": "image/jpeg"})

    return app


async def _sse_stream(messages: list[dict], user_id: int):
    """AI javobini SSE (Server-Sent Events) formatida oqim sifatida yuboradi."""
    import asyncio

    queue: asyncio.Queue = asyncio.Queue()

    async def on_chunk(display_text: str):
        await queue.put(("chunk", display_text))

    async def on_total_failure(provider_name: str, error: Exception):
        await queue.put(("error", f"{provider_name}: {error}"))

    async def run():
        full_text, _ = await bot_module.ai.stream_reply(messages, on_chunk=on_chunk, on_total_failure=on_total_failure)
        history = bot_module.user_histories[user_id]
        history.append({"role": "assistant", "content": full_text})
        bot_module.save_history(user_id)
        await queue.put(("done", full_text))

    task = asyncio.create_task(run())

    try:
        while True:
            kind, payload = await queue.get()
            event = {"type": kind, "text": payload}
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            if kind == "done":
                break
    finally:
        if not task.done():
            task.cancel()
