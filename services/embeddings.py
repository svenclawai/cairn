from __future__ import annotations

from typing import List

import httpx

from config import settings

EMBED_URL = "https://api.openai.com/v1/embeddings"
EMBED_MODEL = "text-embedding-3-small"


async def embed(text: str) -> List[float]:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            EMBED_URL,
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            json={"input": text, "model": EMBED_MODEL},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
