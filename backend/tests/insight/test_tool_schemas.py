import inspect

from app.agent import prompts
from app.agent.tools_schema import ROLE_TOOLS, SCHEMAS, tools_for
from evaluation.world.sim_server import ACTIONS, READ, SimServer


def test_every_schema_has_a_server_method_with_matching_parameters():
    assert set(SCHEMAS) == set(READ + ACTIONS)
    for name, schema in SCHEMAS.items():
        params = inspect.signature(getattr(SimServer, name)).parameters
        expected = [p for p in params if p != "self"]
        assert list(schema["parameters"]["properties"]) == expected, name
        assert schema["parameters"]["required"] == expected, name


def test_roles_only_use_known_tools_and_never_forbidden_ones():
    for role, names in ROLE_TOOLS.items():
        for n in names:
            assert n in SCHEMAS
        assert not {"stop_pod", "disable_key", "raise_budget"} & set(names)
        assert len(tools_for(role)) == len(names)


def test_prompt_only_mentions_real_tools_in_backticks():
    import re
    for role in ("team", "department"):
        text = prompts.build_system_prompt(role=role)
        allowed = set(ROLE_TOOLS[role])
        for name in re.findall(r"`([a-z_]+)`", text):
            assert name in allowed, f"{role} prompt mentions {name}, not one of its tools"
