"""POST /jobs/agent (C5): run the budget-analyst agent for ONE team (or one professor) per call.

pg_cron calls this daily or weekly, once per team, because a serverless function has a time
limit. The endpoint is protected by a secret header (X-Job-Secret = JOB_SECRET in .env), so
only the scheduler can trigger it.

    POST /jobs/agent   {"team_id": "<group uuid>"}         review one team for its professor
    POST /jobs/agent   {"professor_id": "<professor uuid>"} department review of one professor

The agent only reads, warns and proposes (it creates escalations); a human approves.

HOW THE TOOLS PLUG IN. The agent's tools are plain functions in app/agent/tools/*.py. A tool
is found by name: a function called `tool_<name>` whose first argument is the database
session and whose other arguments are the tool's own, for example

    def tool_get_usage(session, team_id): ...      # in app/agent/tools/usage.py (Person 1)
    def tool_warn_user(session, team_id, message): ...  # in warn.py (Person 2)

and it returns a dict with a "status" ("ok" or "failed"). Until every tool the run needs
exists, the endpoint answers 503 and lists the missing ones, so nothing half-works.
"""

from __future__ import annotations

import calendar
import hmac
import uuid
from datetime import date
from importlib import import_module
from typing import Annotated, Any, Callable

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlmodel import Session

from app.agent import agent as agent_module
from app.agent.llm import LLM
from app.agent.tools_schema import ROLE_TOOLS
from app.core.db import get_session
from app.models import Group, Professor, User

router = APIRouter(prefix="/jobs", tags=["jobs"])

TOOL_MODULES = ["budgets", "usage", "idle_pods", "warn", "propose", "forecast", "anomalies"]


# ---------------------------------------------------------------- the secret header
class _JobSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), env_file_encoding="utf-8", extra="ignore")
    job_secret: str = ""


def require_job_secret(x_job_secret: Annotated[str | None, Header()] = None) -> None:
    expected = _JobSettings().job_secret
    if not expected:
        raise HTTPException(503, "JOB_SECRET is not set on the server")
    if not x_job_secret or not hmac.compare_digest(x_job_secret, expected):
        raise HTTPException(401, "missing or wrong X-Job-Secret")


# ---------------------------------------------------------------- tools
def discover_tools() -> dict[str, Callable[..., Any]]:
    """Every `tool_<name>` function found in app/agent/tools/*.py, by tool name."""
    found: dict[str, Callable[..., Any]] = {}
    for mod_name in TOOL_MODULES:
        module = import_module(f"app.agent.tools.{mod_name}")
        for attr in dir(module):
            if attr.startswith("tool_") and callable(getattr(module, attr)):
                found.setdefault(attr.removeprefix("tool_"), getattr(module, attr))
    return found


class LiveServer:
    """What the agent talks to: server.call(name, **args). Runs the real tool on the database."""

    def __init__(self, session: Session, tools: dict[str, Callable[..., Any]]):
        self.session = session
        self.tools = tools

    def call(self, name: str, **kwargs: Any) -> Any:
        tool = self.tools.get(name)
        if tool is None:
            return {"status": "failed", "detail": f"unknown tool {name}"}
        try:
            return tool(self.session, **kwargs)
        except TypeError:
            raise  # bad arguments: the loop reports it back to the model
        except Exception as exc:  # a tool bug must not crash the whole run
            return {"status": "failed", "detail": f"{name} failed: {type(exc).__name__}"}


# ---------------------------------------------------------------- the run
class ToolsNotReady(Exception):
    def __init__(self, missing: list[str]):
        super().__init__(", ".join(missing))
        self.missing = missing


class SubjectNotFound(Exception):
    pass


def _period(today: date) -> dict[str, int]:
    return {"day": today.day, "days": calendar.monthrange(today.year, today.month)[1]}


def run_agent(
    session: Session,
    tools: dict[str, Callable[..., Any]],
    *,
    team_id: uuid.UUID | None = None,
    professor_id: uuid.UUID | None = None,
    llm: Any = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Run the agent once for one team or one professor. Returns a short summary."""
    if (team_id is None) == (professor_id is None):
        raise ValueError("give exactly one of team_id or professor_id")
    role = "team" if team_id else "department"

    missing = [t for t in ROLE_TOOLS[role] if t not in tools]
    if missing:
        raise ToolsNotReady(missing)

    period = _period(today or date.today())
    # No disk cache in a live run: serverless disks are read-only, and a cached reply would
    # hide new spend.
    llm = llm or LLM(use_cache=False)
    server = LiveServer(session, tools)

    if team_id:
        group = session.get(Group, team_id)
        if group is None:
            raise SubjectNotFound(f"team {team_id}")
        subject = {"id": str(group.id), "name": group.name, "professor_id": str(group.professor_id)}
        result = agent_module.review_team(server, subject, period, llm=llm, cache_tag="live")
    else:
        prof = session.get(Professor, professor_id)
        if prof is None:
            raise SubjectNotFound(f"professor {professor_id}")
        user = session.get(User, prof.user_id)
        subject = {"id": str(prof.id), "name": user.name if user else None}
        result = agent_module.review_professor(server, subject, period, llm=llm, cache_tag="live")

    return {
        "subject": subject,
        "period": period,
        "stopped": result["stopped"],  # "done" or "max_steps"
        "steps": result["steps"],
        "summary": result["final"],
        "tools_used": [t["tool"] for t in result["trace"] if t["kind"] == "tool"],
    }


# ---------------------------------------------------------------- the endpoint
class AgentRunIn(BaseModel):
    team_id: uuid.UUID | None = None
    professor_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _one_subject(self):
        if (self.team_id is None) == (self.professor_id is None):
            raise ValueError("give exactly one of team_id or professor_id")
        return self


def get_tools() -> dict[str, Callable[..., Any]]:
    """A dependency, so tests can swap the tools."""
    return discover_tools()


def get_llm() -> Any:
    """A dependency, so tests can swap the model. None means the real one."""
    return None


@router.post("/agent", dependencies=[Depends(require_job_secret)])
def run_agent_job(
    body: AgentRunIn,
    session: Annotated[Session, Depends(get_session)],
    tools: Annotated[dict, Depends(get_tools)],
    llm: Annotated[Any, Depends(get_llm)],
) -> dict[str, Any]:
    try:
        return run_agent(session, tools, team_id=body.team_id, professor_id=body.professor_id, llm=llm)
    except ToolsNotReady as exc:
        raise HTTPException(503, {"error": "agent tools are not ready", "missing": exc.missing})
    except SubjectNotFound as exc:
        raise HTTPException(404, f"not found: {exc}")
