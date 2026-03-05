from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

import httpx

from config import settings

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-20250514"

COVERAGE_PROMPT = """You are evaluating research coverage for an autonomous agent.

Goal: {goal_prompt}
Goal schema dimensions: {goal_schema_dimensions}

Queries executed ({queries_count} total):
{queries_list}

Pages visited ({pages_count} total) with extracted data:
{pages_summary}

Score how completely each dimension has been covered on a 0.0–1.0 scale.
Be conservative — only give high scores when data is actually present and reliable.

Respond with JSON only:
{{
  "overall_score": 0.0,
  "dimensions": {{
    "dimension_name": {{
      "score": 0.0,
      "note": "brief observation"
    }}
  }},
  "gaps": ["gap description 1", "gap description 2"],
  "recommendation": "continue | diminishing | stop"
}}"""


def _strip_code_fences(text: str) -> str:
    text = re.sub(r"^```(?:json)?\s*\n?", "", text.strip())
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


async def compute_coverage(
    goal_prompt: str,
    goal_schema: Optional[Dict] = None,
    queries: List[Dict] = None,
    pages: List[Dict] = None,
) -> Dict:
    dimensions_str = json.dumps(goal_schema) if goal_schema else "None specified"
    queries_list = "\n".join(f"- {q['phrase']}" for q in queries) if queries else "None yet"
    pages_summary = ""
    for p in pages:
        extracted = p.get("extracted", {})
        pages_summary += f"- {p.get('canonical_url', p.get('url', 'unknown'))}: {json.dumps(extracted)}\n"
    if not pages_summary:
        pages_summary = "None yet"

    prompt = COVERAGE_PROMPT.format(
        goal_prompt=goal_prompt,
        goal_schema_dimensions=dimensions_str,
        queries_count=len(queries),
        queries_list=queries_list,
        pages_count=len(pages),
        pages_summary=pages_summary,
    )

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        content = resp.json()["content"][0]["text"]
        return json.loads(_strip_code_fences(content))
