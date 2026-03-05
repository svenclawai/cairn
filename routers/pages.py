from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query

from database import get_supabase
from models.page import (
    PageExtractRequest,
    PageExtractResponse,
    PageResponse,
    PagesCreate,
    PagesCreateResponse,
    PagesListResponse,
    PageUpdate,
)
from services.extractor import content_hash, extract_data, fetch_page
from services.url_utils import canonicalize_url

router = APIRouter(prefix="/sessions/{session_id}", tags=["pages"])


def _load_session(session_id: str, api_key: str) -> dict:
    sb = get_supabase()
    result = (
        sb.table("sessions").select("id").eq("id", session_id).eq("api_key", api_key).execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Session not found")
    return result.data[0]


@router.post("/pages", response_model=PagesCreateResponse, status_code=201)
async def register_pages(session_id: str, body: PagesCreate, x_api_key: str = Header(...)):
    _load_session(session_id, x_api_key)
    sb = get_supabase()

    existing_result = (
        sb.table("pages")
        .select("canonical_url")
        .eq("session_id", session_id)
        .execute()
    )
    existing_urls = {r["canonical_url"] for r in (existing_result.data or [])}

    registered = []
    skipped = 0

    for page_input in body.pages:
        canonical = canonicalize_url(page_input.url)
        if canonical in existing_urls:
            skipped += 1
            continue

        result = (
            sb.table("pages")
            .insert(
                {
                    "session_id": session_id,
                    "url": page_input.url,
                    "canonical_url": canonical,
                    "status": "queued",
                }
            )
            .execute()
        )
        row = result.data[0]
        existing_urls.add(canonical)
        registered.append(
            PageResponse(
                id=row["id"],
                canonical_url=row["canonical_url"],
                status=row["status"],
                queued_at=row["queued_at"],
            )
        )

    return PagesCreateResponse(
        registered=len(registered),
        skipped_duplicates=skipped,
        pages=registered,
    )


@router.patch("/pages")
async def update_page(session_id: str, body: PageUpdate, x_api_key: str = Header(...)):
    _load_session(session_id, x_api_key)
    sb = get_supabase()

    canonical = canonicalize_url(body.url)

    update_data: dict = {"status": body.status}
    if body.content_hash is not None:
        update_data["content_hash"] = body.content_hash
    if body.extracted is not None:
        update_data["extracted"] = body.extracted
    if body.error is not None:
        update_data["error"] = body.error

    now = datetime.utcnow().isoformat()
    if body.status == "visited":
        update_data["visited_at"] = now
    elif body.status == "summarized":
        update_data["summarized_at"] = now

    result = (
        sb.table("pages")
        .update(update_data)
        .eq("session_id", session_id)
        .eq("canonical_url", canonical)
        .execute()
    )
    if not result.data:
        # Try matching by raw URL
        result = (
            sb.table("pages")
            .update(update_data)
            .eq("session_id", session_id)
            .eq("url", body.url)
            .execute()
        )
    if not result.data:
        raise HTTPException(status_code=404, detail="Page not found")

    row = result.data[0]
    return {
        "id": row["id"],
        "canonical_url": row["canonical_url"],
        "status": row["status"],
        "visited_at": row.get("visited_at"),
        "summarized_at": row.get("summarized_at"),
    }


@router.get("/pages", response_model=PagesListResponse)
async def list_pages(
    session_id: str,
    status: Optional[str] = Query(None),
    x_api_key: str = Header(...),
):
    _load_session(session_id, x_api_key)
    sb = get_supabase()

    query = (
        sb.table("pages")
        .select("id, url, canonical_url, status, extracted, queued_at, visited_at, summarized_at")
        .eq("session_id", session_id)
    )
    if status:
        query = query.eq("status", status)

    result = query.order("queued_at").execute()
    pages = result.data or []

    return PagesListResponse(
        pages=[
            PageResponse(
                id=p["id"],
                url=p["url"],
                canonical_url=p["canonical_url"],
                status=p["status"],
                extracted=p["extracted"],
                queued_at=p["queued_at"],
                visited_at=p["visited_at"],
                summarized_at=p["summarized_at"],
            )
            for p in pages
        ],
        total=len(pages),
    )


@router.post("/pages/extract", response_model=PageExtractResponse)
async def extract_page(session_id: str, body: PageExtractRequest, x_api_key: str = Header(...)):
    session = _load_session(session_id, x_api_key)
    sb = get_supabase()

    # Load session goal_prompt and goal_schema
    session_result = (
        sb.table("sessions")
        .select("goal_prompt, goal_schema")
        .eq("id", session_id)
        .execute()
    )
    session_data = session_result.data[0]

    canonical = canonicalize_url(body.url)

    # Register page if not already present
    existing = (
        sb.table("pages")
        .select("id")
        .eq("session_id", session_id)
        .eq("canonical_url", canonical)
        .execute()
    )
    if existing.data:
        page_id = existing.data[0]["id"]
    else:
        insert_result = (
            sb.table("pages")
            .insert({
                "session_id": session_id,
                "url": body.url,
                "canonical_url": canonical,
                "status": "queued",
            })
            .execute()
        )
        page_id = insert_result.data[0]["id"]

    # Fetch page content
    try:
        raw_content = await fetch_page(body.url)
    except Exception as exc:
        sb.table("pages").update({"status": "failed", "error": str(exc)}).eq("id", page_id).execute()
        raise HTTPException(status_code=502, detail=f"Failed to fetch page: {exc}")

    # Extract structured data
    try:
        extracted = await extract_data(
            content=raw_content,
            goal_prompt=session_data["goal_prompt"],
            goal_schema=session_data["goal_schema"],
            url=body.url,
        )
    except Exception as exc:
        sb.table("pages").update({"status": "failed", "error": str(exc)}).eq("id", page_id).execute()
        raise HTTPException(status_code=502, detail=f"Extraction failed: {exc}")

    # Update page with results
    now = datetime.utcnow().isoformat()
    hash_val = content_hash(raw_content)
    update_result = (
        sb.table("pages")
        .update({
            "status": "visited",
            "content_hash": hash_val,
            "extracted": extracted,
            "visited_at": now,
        })
        .eq("id", page_id)
        .execute()
    )
    row = update_result.data[0]

    return PageExtractResponse(
        id=row["id"],
        canonical_url=row["canonical_url"],
        status=row["status"],
        content_hash=row["content_hash"],
        extracted=row["extracted"],
    )
