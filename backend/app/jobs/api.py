"""Job endpoints, called by pg_cron (B7), not by people. They take no login token: they
need the X-Job-Secret header, which must equal JOB_SECRET in .env.

    POST /jobs/sync        every 5 minutes (D4)
    POST /jobs/rollover    on the 1st of each month: leftovers carried over, budgets allocated
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session

from app.core.config import get_settings
from app.core.db import get_session
from app.jobs.rollover import run_new_month
from app.jobs.sync import run_sync

router = APIRouter(prefix="/jobs", tags=["system"])


def require_job_secret(x_job_secret: Annotated[str | None, Header()] = None) -> None:
    expected = get_settings().job_secret
    # No JOB_SECRET configured = the jobs are switched off, never open to everyone.
    if not expected or not x_job_secret or not hmac.compare_digest(x_job_secret, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or wrong job secret")


class SyncOut(BaseModel):
    resources_polled: int
    spend_entries: int
    skipped: bool


@router.post("/sync", response_model=SyncOut, dependencies=[Depends(require_job_secret)])
def sync(session: Annotated[Session, Depends(get_session)]) -> SyncOut:
    r = run_sync(session)
    return SyncOut(resources_polled=r.resources_polled, spend_entries=r.spend_entries, skipped=r.skipped)


class RolloverOut(BaseModel):
    period: str
    rollover_entries: int
    allocation_entries: int
    skipped: bool


@router.post("/rollover", response_model=RolloverOut, dependencies=[Depends(require_job_secret)])
def rollover(session: Annotated[Session, Depends(get_session)]) -> RolloverOut:
    r = run_new_month(session)
    return RolloverOut(period=r.period, rollover_entries=r.rollover_entries,
                       allocation_entries=r.allocation_entries, skipped=r.skipped)