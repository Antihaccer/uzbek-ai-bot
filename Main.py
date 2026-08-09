"""
Telegram bot (polling) va Mini App uchun veb-server (FastAPI/uvicorn)
bir xil jarayonda, bir vaqtda ishlaydi.
"""
import os
import asyncio
import logging

import uvicorn

from bot import build_application, logger as bot_logger
from webapp import create_web_app

logger = logging.getLogger(__name__)


async def run():
    application = build_application()
    web_app = create_web_app()

    port = int(os.environ.get("PORT", "8080"))
    config = uvicorn.Config(app=web_app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)

    async with application:
        await application.start()
        await application.updater.start_polling()
        bot_logger.info(f"Bot polling va veb-server ({port}-port) ishga tushdi.")
        try:
            await server.serve()
        finally:
            await application.updater.stop()
            await application.stop()


if __name__ == "__main__":
    asyncio.run(run())
