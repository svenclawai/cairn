from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from pydantic import BaseModel


class DimensionScore(BaseModel):
    score: float
    note: str


class CoverageResponse(BaseModel):
    session_id: str
    computed_at: datetime
    overall_score: float
    dimensions: Dict[str, DimensionScore]
    gaps: List[str]
    marginal_gain: float
    recommendation: str
    queries_snapshot: int
    pages_snapshot: int
