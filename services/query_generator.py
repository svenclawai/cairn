from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

import httpx

from config import settings

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-20250514"

QUERY_GEN_PROMPT = """You are a research query generator for an autonomous agent.

Goal: {goal_prompt}
Goal schema (dimensions to cover): {goal_schema}

Queries already executed (do not repeat or semantically duplicate these):
{prior_queries_list}

Rejected candidates this round (too similar to prior queries):
{rejected_candidates}

Generate ONE search query that:
1. Has not been covered by prior queries
2. Directly advances coverage of the goal
3. Is specific enough to return useful results
4. Is 3-8 words

Respond with JSON only:
{{
  "phrase": "your search phrase here",
  "reasoning": "one sentence explaining what gap this covers",
  "target_dimensions": ["dimension1", "dimension2"]
}}

If all major angles have been covered and you cannot generate a meaningfully new query, respond with:
{{
  "phrase": null,
  "reasoning": "explanation of why all angles are covered"
}}"""


def _strip_code_fences(text: str) -> str:
    text = re.sub(r"^```(?:json)?\s*\n?", "", text.strip())
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


async def generate_query(
    goal_prompt: str,
    goal_schema: Optional[Dict] = None,
    prior_queries: List[str] = None,
    rejected_candidates: Optional[List[Dict]] = None,
) -> Dict:
    rejected_str = "None"
    if rejected_candidates:
        rejected_str = "\n".join(
            f"- \"{r['phrase']}\" (similar to: {r.get('similar_to', 'prior query')})"
            for r in rejected_candidates
        )

    prior_str = "\n".join(f"- {q}" for q in prior_queries) if prior_queries else "None yet"

    prompt = QUERY_GEN_PROMPT.format(
        goal_prompt=goal_prompt,
        goal_schema=json.dumps(goal_schema) if goal_schema else "None specified",
        prior_queries_list=prior_str,
        rejected_candidates=rejected_str,
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
