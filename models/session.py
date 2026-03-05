from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel


class SessionCreate(BaseModel):
    goal_prompt: str
    goal_schema: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = {}


class SessionUpdate(BaseModel):
    status: str


class SessionStats(BaseModel):
    queries_executed: int = 0
    pages_queued: int = 0
    pages_visited: int = 0
    pages_summarized: int = 0


class SessionResponse(BaseModel):
    id: str
    goal_prompt: str
    goal_schema: Optional[Dict[str, Any]] = None
    status: str
    created_at: datetime


class SessionDetailResponse(BaseModel):
    id: str
    goal_prompt: str
    goal_schema: Optional[Dict[str, Any]] = None
    status: str
    stats: SessionStats
    latest_coverage: Optional[Dict[str, Any]] = None
    created_at: datetime
