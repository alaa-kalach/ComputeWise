"""Every scenario file must load, and its expected targets must exist."""
from evaluation.world.world import load_all


def test_all_scenarios_are_valid_and_expected_targets_exist():
    scenarios = load_all()
    assert len(scenarios) >= 18
    ids = [s["id"] for s in scenarios]
    assert len(ids) == len(set(ids)), "duplicate scenario id"
    for s in scenarios:
        known = ({t["id"] for t in s["teams"]} | {r["id"] for r in s["resources"]}
                 | {p["id"] for p in s.get("professors", [])})
        for e in s["expected"]:
            for part in str(e["target"]).split("->"):
                assert part in known, f"{s['id']}: unknown target {part}"


def test_held_out_split_exists_and_is_a_third_of_the_set():
    held = load_all(split="heldout")
    assert len(held) == 6
    assert len(load_all(split="tune")) == len(load_all()) - 6
