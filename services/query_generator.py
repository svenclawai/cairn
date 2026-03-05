from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

import httpx

from config import settings

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-20250514"

QUERY_GEN_PROMPT = """You are a research query generator for an autonomous agent.
You MUST stay strictly on-topic. Every query you generate must be directly relevant to the stated goal.
Do NOT drift into tangential or loosely related topics.

Goal: {goal_prompt}
Goal schema (dimensions to cover): {goal_schema}

{coverage_section}

Queries already executed (do not repeat or semantically duplicate these):
{prior_queries_list}

Rejected candidates this round (too similar to prior queries):
{rejected_candidates}

{dimension_targeting}

Generate ONE search query that:
1. Is DIRECTLY and specifically relevant to the goal — reject any tangential ideas
2. Has not been covered by prior queries
3. Directly advances coverage of the goal dimensions, especially any low-scoring ones listed above
4. Is specific enough to return useful results
5. Is 3-8 words

Respond with JSON only:
{{
  "phrase": "your search phrase here",
  "reasoning": "one sentence explaining what gap this covers",
  "target_dimensions": ["dimension1", "dimension2"],
  "relevance_score": 0.95
}}

The relevance_score MUST be your honest self-assessment (0.0-1.0) of how directly relevant this query is to the stated goal. Be strict — score below 0.7 if the query is only tangentially related.

If all major angles have been covered and you cannot generate a meaningfully new query, respond with:
{{
  "phrase": null,
  "reasoning": "explanation of why all angles are covered"
}}"""


def _strip_code_fences(text: str) -> str:
    text = re.sub(r"^```(?:json)?\s*\n?", "", text.strip())
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _build_coverage_section(coverage_scores: Optional[Dict]) -> str:
    if not coverage_scores:
        return "No coverage data available yet."

    dimensions = coverage_scores.get("dimensions", {})
    if not dimensions:
        return "No coverage data available yet."

    lines = ["Current coverage scores:"]
    gaps = []
    for dim_name, dim_data in dimensions.items():
        score = dim_data.get("score", 0.0) if isinstance(dim_data, dict) else dim_data
        note = dim_data.get("note", "") if isinstance(dim_data, dict) else ""
        lines.append(f"  - {dim_name}: {score:.2f}" + (f" ({note})" if note else ""))
        if score < 0.5:
            gaps.append(f"{dim_name} ({score:.2f})")

    if gaps:
        lines.append("")
        lines.append("LOW COVERAGE DIMENSIONS (prioritize these): " + ", ".join(gaps))

    overall = coverage_scores.get("overall_score")
    if overall is not None:
        lines.append(f"Overall coverage: {overall:.2f}")

    return "\n".join(lines)


def _build_dimension_targeting(
    coverage_scores: Optional[Dict],
    enforce_dimensions: bool = False,
) -> str:
    if not coverage_scores:
        return ""

    dimensions = coverage_scores.get("dimensions", {})
    if not dimensions:
        return ""

    threshold = 0.7 if enforce_dimensions else 0.5
    low_dims = []
    for dim_name, dim_data in dimensions.items():
        score = dim_data.get("score", 0.0) if isinstance(dim_data, dict) else dim_data
        if score < threshold:
            low_dims.append(f"{dim_name} (score: {score:.2f})")

    if not low_dims:
        return ""

    if enforce_dimensions:
        return (
            "MANDATORY: Your next query MUST target one or more of these "
            "low-coverage dimensions ONLY: " + ", ".join(low_dims)
            + "\nDo NOT generate queries targeting dimensions with score >= 0.7."
        )
    else:
        return (
            "Your next query MUST target one or more of these low-coverage "
            "dimensions: " + ", ".join(low_dims)
        )


async def generate_query(
    goal_prompt: str,
    goal_schema: Optional[Dict] = None,
    prior_queries: Optional[List[str]] = None,
    rejected_candidates: Optional[List[Dict]] = None,
    coverage_scores: Optional[Dict] = None,
    enforce_dimensions: bool = False,
) -> Dict:
    rejected_str = "None"
    if rejected_candidates:
        rejected_str = "\n".join(
            f"- \"{r['phrase']}\" (similar to: {r.get('similar_to', 'prior query')})"
            for r in rejected_candidates
        )

    prior_str = "\n".join(f"- {q}" for q in prior_queries) if prior_queries else "None yet"

    coverage_section = _build_coverage_section(coverage_scores)
    dimension_targeting = _build_dimension_targeting(coverage_scores, enforce_dimensions)

    prompt = QUERY_GEN_PROMPT.format(
        goal_prompt=goal_prompt,
        goal_schema=json.dumps(goal_schema) if goal_schema else "None specified",
        prior_queries_list=prior_str,
        rejected_candidates=rejected_str,
        coverage_section=coverage_section,
        dimension_targeting=dimension_targeting,
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
