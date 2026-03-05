from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Query

from config import settings
from database import get_supabase
from models.coverage import CoverageResponse, DimensionScore
from services.coverage import compute_coverage

router = APIRouter(prefix="/sessions/{session_id}", tags=["coverage"])


def _load_session(session_id: str, api_key: str) -> dict:
    sb = get_supabase()
    result = (
        sb.table("sessions").select("*").eq("id", session_id).eq("api_key", api_key).execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Session not found")
    return result.data[0]


def _compute_marginal_gain(session_id: str, new_score: float) -> float:
    sb = get_supabase()
    result = (
        sb.table("coverage_scores")
        .select("scores")
        .eq("session_id", session_id)
        .order("computed_at", desc=True)
        .limit(1)
        .execute()
    )
    if not result.data:
        return new_score
    previous = result.data[0]["scores"]
    return new_score - previous.get("overall_score", 0)


@router.get("/coverage", response_model=CoverageResponse)
async def get_coverage(
    session_id: str,
    recompute: bool = Query(False),
    x_api_key: str = Header(...),
):
    session = _load_session(session_id, x_api_key)
    sb = get_supabase()

    if not recompute:
        cache_cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=settings.coverage_cache_minutes)
        ).isoformat()
        cached = (
            sb.table("coverage_scores")
            .select("*")
            .eq("session_id", session_id)
            .gte("computed_at", cache_cutoff)
            .order("computed_at", desc=True)
            .limit(1)
            .execute()
        )
        if cached.data:
            row = cached.data[0]
            scores = row["scores"]
            return CoverageResponse(
                session_id=session_id,
                computed_at=row["computed_at"],
                overall_score=scores["overall_score"],
                dimensions={
                    k: DimensionScore(**v) for k, v in scores["dimensions"].items()
                },
                gaps=scores.get("gaps", []),
                marginal_gain=scores.get("marginal_gain", 0),
                recommendation=scores.get("recommendation", "continue"),
                queries_snapshot=row["queries_count"],
                pages_snapshot=row["pages_count"],
            )

    queries_result = (
        sb.table("queries")
        .select("phrase")
        .eq("session_id", session_id)
        .order("executed_at")
        .execute()
    )
    queries = queries_result.data or []

    pages_result = (
        sb.table("pages")
        .select("canonical_url, status, extracted")
        .eq("session_id", session_id)
        .in_("status", ["visited", "summarized"])
        .execute()
    )
    pages = pages_result.data or []

    scores = await compute_coverage(
        goal_prompt=session["goal_prompt"],
        goal_schema=session["goal_schema"],
        queries=queries,
        pages=pages,
    )

    overall = scores.get("overall_score", 0)
    marginal_gain = _compute_marginal_gain(session_id, overall)
    scores["marginal_gain"] = marginal_gain

    if marginal_gain < 0.02 and scores.get("recommendation") != "stop":
        scores["recommendation"] = "stop"
    elif marginal_gain < 0.05 and scores.get("recommendation") == "continue":
        scores["recommendation"] = "diminishing"

    sb.table("coverage_scores").insert(
        {
            "session_id": session_id,
            "scores": scores,
            "queries_count": len(queries),
            "pages_count": len(pages),
        }
    ).execute()

    return CoverageResponse(
        session_id=session_id,
        computed_at=datetime.now(timezone.utc).isoformat(),
        overall_score=overall,
        dimensions={
            k: DimensionScore(**v) for k, v in scores.get("dimensions", {}).items()
        },
        gaps=scores.get("gaps", []),
        marginal_gain=marginal_gain,
        recommendation=scores.get("recommendation", "continue"),
        queries_snapshot=len(queries),
        pages_snapshot=len(pages),
    )
