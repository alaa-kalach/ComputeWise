"""Entry point of the budget-analyst agent.

solve(server, scenario) runs the agent once per subject: per team for a "team"
scenario (the design says one team per call, because of the serverless time
limit), per professor for a "department" scenario. All runs are stored on
server.agent_results for the evaluation to read.
"""
from __future__ import annotations

from app.agent import prompts
from app.agent.gates import EvidenceGuard, FormatGuard, RetryGuard, department_final_check, final_check
from app.agent.llm import LLM
from app.agent.loop import run_loop
from app.agent.tools_schema import tools_for

MAX_STEPS = 20
_llm = None


def get_llm():
    global _llm
    if _llm is None:
        _llm = LLM()
    return _llm


def _run(server, role, task, llm, cache_tag):
    return run_loop(
        llm or get_llm(),
        RetryGuard(EvidenceGuard(FormatGuard(server))),
        system_prompt=prompts.build_system_prompt(role=role),
        task=task,
        tools=tools_for(role),
        max_steps=MAX_STEPS,
        cache_tag=cache_tag,
        final_check=department_final_check if role == "department" else final_check,
    )


def review_team(server, team, period, llm=None, cache_tag=""):
    return _run(server, "team", prompts.format_task(team, period), llm,
                f"{cache_tag}:{team['id']}")


def review_professor(server, professor, period, llm=None, cache_tag=""):
    return _run(server, "department", prompts.format_professor_task(professor, period), llm,
                f"{cache_tag}:{professor['id']}")


def solve(server, scenario, llm=None, cache_tag=""):
    if scenario.get("role", "team") == "department":
        results = [review_professor(server, p, scenario["period"], llm, cache_tag)
                   for p in scenario["professors"]]
    else:
        results = [review_team(server, t, scenario["period"], llm, cache_tag)
                   for t in scenario["teams"]]
    server.agent_results = results
    return results
