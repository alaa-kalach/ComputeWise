import json

from evaluation.baselines import single_llm_call
from evaluation.world.scoring import score
from evaluation.world.world import SCENARIO_DIR, load_scenario


class OneShotLLM:
    def __init__(self, text):
        self.text = text

    def chat(self, messages, tools=None, cache_tag=""):
        self.messages, self.tools = messages, tools
        return {"content": self.text, "tool_calls": [], "usage": {}}


def test_parse_actions_handles_fenced_json_and_garbage():
    text = 'Here you go:\n```json\n{"actions": [{"tool": "warn_user", "target": "team-beta"}]}\n```'
    assert single_llm_call.parse_actions(text) == [{"tool": "warn_user", "target": "team-beta"}]
    assert single_llm_call.parse_actions("no idea") == []
    assert single_llm_call.parse_actions('{"actions": "oops"}') == []


def test_single_call_baseline_uses_one_call_and_no_tools():
    scenario = load_scenario(SCENARIO_DIR / "forecast" / "forecast_02.json")
    llm = OneShotLLM(json.dumps({"actions": [{"tool": "warn_user", "target": "team-beta"}]}))
    actions = single_llm_call.run(scenario, llm)
    assert llm.tools is None
    assert score(actions, scenario)["passed"]
    assert "forecast" in llm.messages[1]["content"]   # the facts are in the prompt
