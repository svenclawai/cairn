from __future__ import annotations

import json
import hashlib
import logging
import re
from typing import Any, Dict, List, Union

import httpx

from config import settings

logger = logging.getLogger(__name__)

JINA_READER_URL = "https://r.jina.ai/"
EXA_CONTENTS_URL = "https://api.exa.ai/contents"

MAX_CONTENT_CHARS = 40000


async def fetch_page_exa(url: str) -> str:
    """Fetch page content as text via Exa Contents API."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            EXA_CONTENTS_URL,
            headers={
                "x-api-key": settings.exa_api_key,
                "Content-Type": "application/json",
            },
            json={
                "urls": [url],
                "text": {"maxCharacters": 50000},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results or not results[0].get("text"):
            raise ValueError("Exa returned no text content")
        return results[0]["text"]


async def fetch_page_jina(url: str) -> str:
    """Fetch page content as markdown via Jina Reader API."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{JINA_READER_URL}{url}",
            headers={"Accept": "text/markdown"},
        )
        resp.raise_for_status()
        return resp.text


async def fetch_page(url: str) -> str:
    """Fetch page content, trying Exa first with Jina as fallback."""
    if settings.exa_api_key:
        try:
            return await fetch_page_exa(url)
        except Exception as exc:
            logger.warning("Exa fetch failed for %s: %s, falling back to Jina", url, exc)
    return await fetch_page_jina(url)


def content_hash(content: str) -> str:
    """Return SHA-256 hex digest of content."""
    return hashlib.sha256(content.encode()).hexdigest()


def _parse_json_response(text: str) -> Union[Dict[str, Any], List[Any]]:
    """Parse JSON from Claude's response, handling markdown fences and surrounding text."""
    # Strip markdown code fences if present
    if "```" in text:
        match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find a JSON array or object in the text
    for pattern in [
        r"(\[[\s\S]*\])",   # JSON array
        r"(\{[\s\S]*\})",   # JSON object
    ]:
        match = re.search(pattern, text)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                # Try to repair truncated JSON by closing brackets
                candidate = match.group(1)
                repaired = _try_repair_json(candidate)
                if repaired is not None:
                    return repaired

    raise ValueError(f"Could not parse JSON from response: {text[:200]}")


def _try_repair_json(text: str) -> Union[Dict[str, Any], List[Any], None]:
    """Attempt to repair truncated JSON by closing open brackets/braces."""
    # Count unmatched brackets
    open_braces = text.count("{") - text.count("}")
    open_brackets = text.count("[") - text.count("]")

    if open_braces < 0 or open_brackets < 0:
        return None

    # Strip trailing comma or incomplete value
    repaired = text.rstrip()
    repaired = re.sub(r',\s*$', '', repaired)

    # Close any open strings (rough heuristic)
    in_string = False
    escaped = False
    for ch in repaired:
        if escaped:
            escaped = False
            continue
        if ch == '\\':
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
    if in_string:
        repaired += '"'

    # Remove trailing incomplete key-value pair
    repaired = re.sub(r',\s*"[^"]*"\s*:\s*$', '', repaired)

    repaired += "}" * open_braces + "]" * open_brackets

    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        return None


async def extract_data(
    content: str,
    goal_prompt: str,
    goal_schema: Dict[str, Any],
    url: str,
) -> Union[Dict[str, Any], List[Any]]:
    """Extract structured data from page content using Claude Haiku."""
    dimensions = goal_schema.get("dimensions", goal_schema)

    # Handle both simple string lists and dict-based dimension specs
    dimension_keys = []
    dimension_descriptions_list = []
    for d in dimensions:
        if isinstance(d, str):
            dimension_keys.append(d)
            dimension_descriptions_list.append(f"- {d}")
        elif isinstance(d, dict) and "name" in d:
            dimension_keys.append(d["name"])
            dimension_descriptions_list.append(
                f"- {d['name']}: {d.get('description', '')} (type: {d.get('type', 'string')})"
            )
    dimension_descriptions = "\n".join(dimension_descriptions_list)

    # Truncate content to prevent token overflow
    truncated_content = content[:MAX_CONTENT_CHARS]

    prompt = f"""You are extracting structured data from a web page.

Goal: {goal_prompt}
Source URL: {url}

This page may list MULTIPLE shops or items. Extract ALL of them.

Extract the following dimensions for EACH item found:
{dimension_descriptions}

If the page contains a SINGLE item, return a JSON object with these keys: {json.dumps(dimension_keys)}
If the page contains MULTIPLE items (a listicle or directory), return a JSON array of objects, each with these keys: {json.dumps(dimension_keys)}

Rules:
- For each dimension, extract the value from the page content.
- Use null if the information is not found on the page.
- For list/array type dimensions, return a JSON array of values.
- Return ONLY valid JSON, no other text.

Page content:
{truncated_content}"""

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        result = resp.json()

    text = result["content"][0]["text"]
    return _parse_json_response(text)
