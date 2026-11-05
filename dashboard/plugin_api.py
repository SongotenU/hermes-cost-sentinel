"""cost-sentinel backend routes — mounted at /api/plugins/cost-sentinel/.

Thin read-only wrappers around ``cost_sentinel.summary_payload`` / budget
updates, loaded from the sibling package root by explicit path so the backend
never depends on how Hermes mounted the dashboard folder.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)
router = APIRouter()


def _load_sentinel():
    """Import the package-root ``cost_sentinel.py`` regardless of mount context."""
    here = Path(__file__).resolve().parent.parent          # dashboard → package root
    module_path = here / "cost_sentinel.py"
    if not module_path.exists():
        raise HTTPException(status_code=500, detail="cost_sentinel.py missing beside dashboard/")
    spec = importlib.util.spec_from_file_location("cost_sentinel_backend", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cs = _load_sentinel()


class BudgetBody(BaseModel):
    daily_limit_usd: Optional[float] = Field(None, ge=0, le=1_000_000)
    enforce: Optional[str] = Field(None, pattern="^(warn|block|off)$")


@router.get("/summary")
async def summary(days: int = 7, include_errors: bool = False) -> dict[str, Any]:
    return cs.summary_payload(days=max(1, min(90, days)), include_errors=include_errors)


@router.get("/budget")
async def budget() -> dict[str, Any]:
    settings = cs.read_settings()
    return {"daily_limit_usd": cs._parse_limit(settings.get("daily_limit_usd", "0")),
            "enforce": settings.get("enforce", "warn")}


@router.post("/budget")
async def set_budget(body: BudgetBody) -> dict[str, Any]:
    updates: dict[str, str] = {}
    if body.daily_limit_usd is not None:
        updates["daily_limit_usd"] = str(round(body.daily_limit_usd, 4))
    if body.enforce:
        updates["enforce"] = body.enforce
    settings = cs.update_settings(**updates)
    return {"daily_limit_usd": cs._parse_limit(settings.get("daily_limit_usd", "0")),
            "enforce": settings.get("enforce", "warn")}
