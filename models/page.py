from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class PageInput(BaseModel):
    url: str


class PagesCreate(BaseModel):
    pages: List[PageInput]


class PageUpdate(BaseModel):
    url: str
    status: str
    content_hash: Optional[str] = None
    extracted: Any = None
    error: Optional[str] = None


class PageResponse(BaseModel):
    id: str
    url: Optional[str] = None
    canonical_url: str
    status: str
    extracted: Any = None
    queued_at: Optional[datetime] = None
    visited_at: Optional[datetime] = None
    summarized_at: Optional[datetime] = None


class PagesCreateResponse(BaseModel):
    registered: int
    skipped_duplicates: int
    pages: List[PageResponse]


class PagesListResponse(BaseModel):
    pages: List[PageResponse]
    total: int


class PageExtractRequest(BaseModel):
    url: str


class PageExtractResponse(BaseModel):
    id: str
    canonical_url: str
    status: str
    content_hash: str
    extracted: Any
