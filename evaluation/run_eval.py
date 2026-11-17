"""Score the three systems on every scenario.

From the repo root:
    python3 -m evaluation.run_eval --system rule
    export OPENROUTER_API_KEY=...
    python3 -m evaluation.run_eval --system single --runs 3
    python3 -m evaluation.run_eval --system agent  --runs 3
    python3 -m evaluation.run_eval --system agent --runs 1 --area keys_pods
"""
from __future__ import annotations

import argparse
from collections import defaultdict

from evaluation.world.scoring import score
from evaluation.world.sim_server import SimServer
from evaluation.world.world import load_all


def actions_for(system, scenario, k, llm):
    if system == "rule":
        from evaluation.baselines import rule_script
        return rule_script.run(scenario)
    if system == "single":
        from evaluation.baselines import single_llm_call
        return single_llm_call.run(scenario, llm, cache_tag=f"single-r{k}")
    from app.agent.agent import solve
    server = SimServer(scenario)
    solve(server, scenario, llm=llm, cache_tag=f"r{k}")
    return server.actions


def evaluate(system, runs, area=None, llm=None, split=None):
    scenarios = load_all(area, split)
    passes, reasons, totals, violations = defaultdict(int), defaultdict(list), [], 0
    for k in range(1, runs + 1):
        total = 0
        for s in scenarios:
            r = score(actions_for(system, s, k, llm), s)
            total += r["passed"]
            passes[s["id"]] += r["passed"]
            violations += len(r["violations"])
            if not r["passed"]:
                reasons[s["id"]].append(
                    f"missing={r['missing']} extra={r['unexpected']} violations={r['violations']}")
        totals.append(total)
    return {"system": system, "runs": runs, "n": len(scenarios), "totals": totals,
            "violations": violations, "passes": dict(passes), "reasons": dict(reasons)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--system", choices=["rule", "single", "agent"], required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--area", default=None)
    parser.add_argument("--split", choices=["tune", "heldout"], default=None,
                        help="tune while developing; run heldout only for the final numbers")
    args = parser.parse_args()

    runs = 1 if args.system == "rule" else args.runs
    llm = None
    if args.system != "rule":
        from app.agent.llm import LLM
        llm = LLM()
    res = evaluate(args.system, runs, args.area, llm, args.split)

    print(f"{res['system']}: per-run totals {res['totals']} of {res['n']}")
    print("safety violations:", res["violations"])
    print("passed all runs:", sum(v == runs for v in res["passes"].values()))
    for sid in sorted(res["passes"]):
        if res["passes"][sid] < runs:
            print(f"  {sid} {res['passes'][sid]}/{runs}  {res['reasons'][sid][0]}")


if __name__ == "__main__":
    main()
