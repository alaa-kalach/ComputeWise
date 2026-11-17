"""Generic tool-calling loop (adapted from the AegisMDM project).

Ask the model; if it calls tools, run them through `server.call(name, **args)`
and feed the results back; stop when it answers without a tool call. The
optional `final_check` gate can send the agent back to work (at most
MAX_NUDGES times) before it is allowed to finish.
"""
from __future__ import annotations

import json

DEFAULT_MAX_STEPS = 20
MAX_NUDGES = 2
MAX_RESULT_CHARS = 4000


def run_loop(llm, server, system_prompt, task, tools, max_steps=DEFAULT_MAX_STEPS,
             cache_tag="", final_check=None):
    allowed = {t["function"]["name"] for t in tools}
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]
    trace = []
    nudges = 0

    for step in range(1, max_steps + 1):
        reply = llm.chat(messages, tools, cache_tag=cache_tag)
        trace.append({"step": step, "kind": "model", "content": reply["content"],
                      "tool_calls": [c["name"] for c in reply["tool_calls"]],
                      "usage": reply["usage"]})

        if not reply["tool_calls"]:
            nudge = final_check(trace) if final_check and nudges < MAX_NUDGES else None
            if nudge:
                nudges += 1
                messages.append({"role": "assistant", "content": reply["content"] or ""})
                messages.append({"role": "user", "content": nudge})
                trace.append({"step": step, "kind": "gate", "content": nudge})
                continue
            return _result(reply["content"], "done", step, trace, messages)

        calls = [{**c, "id": c["id"] or f"call_{step}_{i}"}
                 for i, c in enumerate(reply["tool_calls"])]
        messages.append({
            "role": "assistant",
            "content": reply["content"],
            "tool_calls": [{"id": c["id"], "type": "function",
                            "function": {"name": c["name"], "arguments": c["arguments"]}}
                           for c in calls],
        })

        for call in calls:
            args, result = _run_tool(server, call, allowed)
            trace.append({"step": step, "kind": "tool", "tool": call["name"], "args": args,
                          "status": result.get("status", "ok") if isinstance(result, dict) else "ok",
                          "result": result})
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": _to_text(result)})

    return _result(None, "max_steps", max_steps, trace, messages)


def _run_tool(server, call, allowed):
    name = call["name"]
    if name not in allowed:
        return {}, {"status": "failed", "detail": f"tool '{name}' is not available to this agent"}
    try:
        args = json.loads(call["arguments"] or "{}")
    except json.JSONDecodeError:
        return {}, {"status": "failed", "detail": "arguments were not valid JSON"}
    if not isinstance(args, dict):
        return {}, {"status": "failed", "detail": "arguments must be a JSON object"}
    try:
        return args, server.call(name, **args)
    except TypeError as exc:  # missing or unexpected argument
        return args, {"status": "failed", "detail": f"bad arguments for {name}: {exc}"}


def _to_text(result):
    text = json.dumps(result, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + " ...[trimmed]"
    return text


def _result(final, stopped, steps, trace, messages):
    return {"final": final, "stopped": stopped, "steps": steps,
            "trace": trace, "messages": messages}
