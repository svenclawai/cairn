from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel


class QueryCreate(BaseModel):
    phrase: str
    result_count: Optional[int] = None


class QueryResponse(BaseModel):
    id: str
    phrase: str
    result_count: Optional[int] = None
    executed_at: datetime


class QueryListResponse(BaseModel):
    queries: List[QueryResponse]
    total: int


class NextQueryResponse(BaseModel):
    phrase: Optional[str]
    reasoning: str
    similar_prior_queries: Optional[List[Dict]] = None
    attempts: Optional[int] = None
    coverage_suggestion: Optional[str] = None
