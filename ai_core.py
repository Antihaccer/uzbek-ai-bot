"""
Bot va veb-ilova (Mini App) uchun umumiy AI mantiqi.
Gemini -> Groq avtomatik almashinuv, rasm yaratish, ovozni matnga o'girish shu yerda.
"""
import re
import base64
import logging
from datetime import date
from typing import Awaitable, Callable, Optional

import httpx
from groq import Groq
from openai import OpenAI

logger = logging.getLogger(__name__)

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_thinking(raw: str) -> str:
    """Modelning ichki 'fikrlash' (<think>...</think>) qismini olib tashlaydi."""
    cleaned = THINK_RE.sub("", raw)
    idx = cleaned.find("<think>")
    if idx != -1:
        cleaned = cleaned[:idx]
    return cleaned.strip()


class AICore:
    def __init__(
        self,
        groq_api_key: str,
        gemini_api_key: str,
        model: str,
        gemini_model: str,
        gemini_base_url: str,
        cf_account_id: str,
        cf_api_token: str,
        cf_image_model: str,
        system_prompt: str,
    ):
        self.groq_client = Groq(api_key=groq_api_key)
        self.gemini_client = OpenAI(api_key=gemini_api_key, base_url=gemini_base_url) if gemini_api_key else None
        self.model = model
        self.gemini_model = gemini_model
        self.cf_account_id = cf_account_id
        self.cf_api_token = cf_api_token
        self.cf_image_model = cf_image_model
        self.system_prompt = system_prompt
        self._gemini_exhausted_date: Optional[str] = None

    def gemini_is_available(self) -> bool:
        if self.gemini_client is None:
            return False
        if self._gemini_exhausted_date == date.today().isoformat():
            return False
        return True

    def mark_gemini_exhausted(self):
        self._gemini_exhausted_date = date.today().isoformat()
        logger.warning("Gemini kunlik limiti tugadi — Groq'ga o'tildi.")

    def _build_providers(self) -> list[tuple]:
        providers = []
        if self.gemini_is_available():
            providers.append((self.gemini_client, self.gemini_model, {}, "gemini"))
        providers.append((self.groq_client, self.model, {"reasoning_effort": "none"}, "groq"))
        return providers

    async def stream_reply(
        self,
        messages: list[dict],
        on_chunk: Optional[Callable[[str], Awaitable[None]]] = None,
        on_total_failure: Optional[Callable[[str, Exception], Awaitable[None]]] = None,
    ) -> tuple[str, Optional[str]]:
        """
        AI'dan streaming javob oladi (avval Gemini, limit/xato bo'lsa Groq).
        on_chunk(display_text) — har safar ko'rsatiladigan matn yangilanganda chaqiriladi.
        on_total_failure(provider_name, error) — barcha provayderlar muvaffaqiyatsiz bo'lsa chaqiriladi.
        Qaytaradi: (full_text, ishlatilgan_provayder_nomi_yoki_None)
        """
        providers = self._build_providers()
        full_text = ""
        used_provider = None

        for client, model, extra_kwargs, provider_name in providers:
            raw_text = ""
            try:
                stream = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.7,
                    max_tokens=800,
                    stream=True,
                    **extra_kwargs,
                )

                for chunk in stream:
                    delta = chunk.choices[0].delta.content or ""
                    if not delta:
                        continue
                    raw_text += delta
                    display_text = strip_thinking(raw_text)
                    if display_text and on_chunk:
                        await on_chunk(display_text)

                full_text = strip_thinking(raw_text)
                if not full_text:
                    raise RuntimeError("Bo'sh javob qaytdi")

                used_provider = provider_name
                logger.info(f"✅ Javob '{provider_name}' orqali berildi.")
                break  # muvaffaqiyatli — boshqa provayderni sinashning hojati yo'q

            except Exception as e:
                if provider_name == "gemini":
                    self.mark_gemini_exhausted()
                    logger.info(f"Gemini ishlamadi ({e}), keyingisiga o'tilyapti...")
                    continue

                logger.error(f"{provider_name} xatosi: {e}")
                if provider_name == providers[-1][3]:  # oxirgi provayder ham ishlamadi
                    full_text = "Kechirasiz, hozir javob bera olmadim. Birozdan so'ng qayta urinib ko'ring. 🙏"
                    if on_total_failure:
                        await on_total_failure(provider_name, e)

        return full_text, used_provider

    def enhance_image_prompt(self, user_prompt: str) -> str:
        """Qisqa (o'zbekcha) tavsifni AI orqali batafsil, aniq inglizcha rasm-promptga aylantiradi."""
        try:
            response = self.groq_client.chat.completions.create(
                model=self.model,
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

    async def generate_image_bytes(self, prompt: str) -> bytes:
        """Berilgan tavsif asosida Cloudflare Workers AI orqali rasm yaratadi."""
        enhanced_prompt = self.enhance_image_prompt(prompt)
        cf_url = f"https://api.cloudflare.com/client/v4/accounts/{self.cf_account_id}/ai/run/{self.cf_image_model}"

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                cf_url,
                headers={"Authorization": f"Bearer {self.cf_api_token}"},
                json={"prompt": enhanced_prompt, "steps": 8},
            )
            response.raise_for_status()
            data = response.json()

        if not data.get("success"):
            raise RuntimeError(f"Cloudflare xatosi: {data.get('errors')}")

        return base64.b64decode(data["result"]["image"])

    def transcribe_audio(self, file_bytes: bytes, filename: str, first_name: str = "") -> str:
        """Ovoz faylini (Groq Whisper orqali) o'zbek tilida matnga o'giradi."""
        hint_prompt = "Bu o'zbek tilidagi ovozli xabar."
        if first_name:
            hint_prompt += f" Gapiruvchining ismi: {first_name}."

        transcript = self.groq_client.audio.transcriptions.create(
            file=(filename, file_bytes),
            model="whisper-large-v3",
            language="uz",
            prompt=hint_prompt,
            temperature=0,
        )
        return transcript.text.strip()
