from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query

from config import settings
from database import get_supabase
from models.query import NextQueryResponse, QueryCreate, QueryListResponse, QueryResponse
from services.dedup import is_semantically_duplicate
from services.embeddings import embed
from services.query_generator import generate_query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions/{session_id}", tags=["queries"])


def _load_session(session_id: str, api_key: str) -> dict:
    sb = get_supabase()
    result = (
        sb.table("sessions").select("*").eq("id", session_id).eq("api_key", api_key).execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Session not found")
    return result.data[0]


def _fetch_latest_coverage(session_id: str) -> Optional[dict]:
    """Fetch the most recent coverage scores for a session from the DB."""
    sb = get_supabase()
    result = (
        sb.table("coverage_scores")
        .select("scores")
        .eq("session_id", session_id)
        .order("computed_at", desc=True)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]["scores"]
    return None


@router.get("/next-query", response_model=NextQueryResponse)
async def next_query(
    session_id: str,
    hint: Optional[str] = Query(None),
    enforce_dimensions: bool = Query(False),
    x_api_key: str = Header(...),
):
    session = _load_session(session_id, x_api_key)
    sb = get_supabase()

    prior_result = (
        sb.table("queries")
        .select("phrase")
        .eq("session_id", session_id)
        .order("executed_at")
        .execute()
    )
    prior_queries = [r["phrase"] for r in (prior_result.data or [])]

    # Fetch latest coverage to feed into query generation
    coverage_scores = _fetch_latest_coverage(session_id)

    rejected_candidates = []  # type: list[dict]

    max_attempts = settings.max_dedup_attempts + settings.max_relevance_retries

    for attempt in range(1, max_attempts + 1):
        result = await generate_query(
            goal_prompt=session["goal_prompt"],
            goal_schema=session["goal_schema"],
            prior_queries=prior_queries,
            rejected_candidates=rejected_candidates if rejected_candidates else None,
            coverage_scores=coverage_scores,
            enforce_dimensions=enforce_dimensions,
        )

        phrase = result.get("phrase")
        if phrase is None:
            return NextQueryResponse(
                phrase=None,
                reasoning=result.get("reasoning", "All major angles appear covered."),
                coverage_suggestion="Consider calling /coverage to assess completeness.",
            )

        # Check relevance score — reject if too low
        relevance_score = result.get("relevance_score")
        if relevance_score is not None and relevance_score < 0.7:
            logger.info(
                "Rejecting query '%s' for low relevance %.2f (attempt %d)",
                phrase, relevance_score, attempt,
            )
            rejected_candidates.append({
                "phrase": phrase,
                "similar_to": f"off-topic (relevance={relevance_score:.2f})",
            })
            continue

        # Check semantic dedup
        is_dup, similar, _ = await is_semantically_duplicate(
            session_id, phrase, settings.similarity_threshold
        )

        if not is_dup:
            return NextQueryResponse(
                phrase=phrase,
                reasoning=result.get("reasoning", ""),
                similar_prior_queries=similar if similar else [],
                attempts=attempt,
                relevance_score=relevance_score,
                target_dimensions=result.get("target_dimensions"),
            )

        rejected_candidates.append(
            {"phrase": phrase, "similar_to": similar[0]["phrase"] if similar else "prior query"}
        )

    return NextQueryResponse(
        phrase=None,
        reasoning=f"Could not generate a suitable query after {max_attempts} attempts.",
        coverage_suggestion="Consider calling /coverage to assess completeness.",
    )


@router.post("/queries", response_model=QueryResponse, status_code=201)
async def log_query(session_id: str, body: QueryCreate, x_api_key: str = Header(...)):
    _load_session(session_id, x_api_key)
    sb = get_supabase()

    embedding = await embed(body.phrase)

    result = (
        sb.table("queries")
        .insert(
            {
                "session_id": session_id,
                "phrase": body.phrase,
                "embedding": embedding,
                "result_count": body.result_count,
            }
        )
        .execute()
    )
    row = result.data[0]
    return QueryResponse(
        id=row["id"],
        phrase=row["phrase"],
        result_count=row["result_count"],
        executed_at=row["executed_at"],
    )


@router.get("/queries", response_model=QueryListResponse)
async def list_queries(session_id: str, x_api_key: str = Header(...)):
    _load_session(session_id, x_api_key)
    sb = get_supabase()

    result = (
        sb.table("queries")
        .select("id, phrase, result_count, executed_at")
        .eq("session_id", session_id)
        .order("executed_at")
        .execute()
    )
    queries = result.data or []
    return QueryListResponse(
        queries=[
            QueryResponse(
                id=q["id"],
                phrase=q["phrase"],
                result_count=q["result_count"],
                executed_at=q["executed_at"],
            )
            for q in queries
        ],
        total=len(queries),
    )
