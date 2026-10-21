"""Book identification via GPT-4o mini vision."""

import base64
import json
import logging

import httpx

from app.core.config import settings
from app.services.book_identifier import BookCandidate, ScanUnavailableError

logger = logging.getLogger(__name__)

OPENAI_URL = "https://api.openai.com/v1/chat/completions"

SYSTEM_PROMPT = """You are a book identification assistant.
Given a photo of a book cover, identify the book and return a JSON array of up to 3 candidates ranked by confidence.

Each candidate must have:
- title: string
- author: string
- confidence: float between 0 and 1
- isbn_13: string or null (any valid 13-digit ISBN for this book — any edition is fine)
- isbn_10: string or null (any valid 10-digit ISBN for this book — any edition is fine)

Provide at least one ISBN whenever you can identify the book, even if you are not certain which specific edition is shown. The ISBN will only be used as a search hint.
Return ONLY a valid JSON array, no other text. Example:
[{"title":"Dune","author":"Frank Herbert","confidence":0.97,"isbn_13":"9780441013593","isbn_10":null}]"""


class ChatGPTVisionIdentifier:
    async def identify(self, image_bytes: bytes) -> list[BookCandidate]:
        b64 = base64.standard_b64encode(image_bytes).decode()

        payload = {
            "model": settings.scan_model,
            "max_tokens": settings.openai_max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                        },
                        {"type": "text", "text": "Identify this book cover."},
                    ],
                },
            ],
        }

        try:
            async with httpx.AsyncClient(timeout=settings.scan_timeout_seconds) as client:
                resp = await client.post(
                    OPENAI_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                )
                resp.raise_for_status()
        except httpx.TimeoutException as exc:
            logger.warning("OpenAI vision request timed out: %s", exc)
            raise ScanUnavailableError("Vision API timed out") from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                logger.warning("OpenAI rate limit exceeded (429)")
            else:
                logger.warning(
                    "OpenAI vision request failed with HTTP %s",
                    exc.response.status_code,
                )
            raise ScanUnavailableError("Vision API returned an error") from exc

        content = resp.json()["choices"][0]["message"]["content"].strip()

        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            logger.warning("GPT returned non-JSON: %s", content)
            return []

        candidates = []
        for item in raw[:3]:
            try:
                candidates.append(
                    BookCandidate(
                        title=item["title"],
                        author=item["author"],
                        confidence=float(item.get("confidence", 0.5)),
                        isbn_13=item.get("isbn_13"),
                        isbn_10=item.get("isbn_10"),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue

        return candidates
