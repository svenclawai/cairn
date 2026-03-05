from datetime import datetime

from fastapi import APIRouter, Header, HTTPException

from database import get_supabase
from models.session import (
    SessionCreate,
    SessionDetailResponse,
    SessionResponse,
    SessionStats,
    SessionUpdate,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(body: SessionCreate, x_api_key: str = Header(...)):
    sb = get_supabase()
    result = (
        sb.table("sessions")
        .insert(
            {
                "api_key": x_api_key,
                "goal_prompt": body.goal_prompt,
                "goal_schema": body.goal_schema,
                "metadata": body.metadata,
            }
        )
        .execute()
    )
    row = result.data[0]
    return SessionResponse(
        id=row["id"],
        goal_prompt=row["goal_prompt"],
        goal_schema=row["goal_schema"],
        status=row["status"],
        created_at=row["created_at"],
    )


@router.get("/{session_id}", response_model=SessionDetailResponse)
async def get_session(session_id: str, x_api_key: str = Header(...)):
    sb = get_supabase()

    session_result = (
        sb.table("sessions").select("*").eq("id", session_id).eq("api_key", x_api_key).execute()
    )
    if not session_result.data:
        raise HTTPException(status_code=404, detail="Session not found")
    session = session_result.data[0]

    queries_result = (
        sb.table("queries")
        .select("id", count="exact")
        .eq("session_id", session_id)
        .execute()
    )
    queries_count = queries_result.count or 0

    pages_result = sb.table("pages").select("status").eq("session_id", session_id).execute()
    pages = pages_result.data or []
    stats = SessionStats(
        queries_executed=queries_count,
        pages_queued=sum(1 for p in pages if p["status"] == "queued"),
        pages_visited=sum(1 for p in pages if p["status"] == "visited"),
        pages_summarized=sum(1 for p in pages if p["status"] == "summarized"),
    )

    coverage_result = (
        sb.table("coverage_scores")
        .select("scores, computed_at")
        .eq("session_id", session_id)
        .order("computed_at", desc=True)
        .limit(1)
        .execute()
    )
    latest_coverage = coverage_result.data[0]["scores"] if coverage_result.data else None

    return SessionDetailResponse(
        id=session["id"],
        goal_prompt=session["goal_prompt"],
        goal_schema=session["goal_schema"],
        status=session["status"],
        stats=stats,
        latest_coverage=latest_coverage,
        created_at=session["created_at"],
    )


@router.patch("/{session_id}")
async def update_session(session_id: str, body: SessionUpdate, x_api_key: str = Header(...)):
    sb = get_supabase()
    if body.status not in ("active", "completed", "abandoned"):
        raise HTTPException(status_code=400, detail="Invalid status")

    result = (
        sb.table("sessions")
        .update({"status": body.status, "updated_at": datetime.utcnow().isoformat()})
        .eq("id", session_id)
        .eq("api_key", x_api_key)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Session not found")
    row = result.data[0]
    return {"id": row["id"], "status": row["status"], "updated_at": row["updated_at"]}
