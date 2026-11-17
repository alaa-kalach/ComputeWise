"""Model connection for the budget-analyst agent, through OpenRouter.

OpenRouter speaks the OpenAI API, so we use the OpenAI SDK with a different
base_url. Replies are cached on disk (same messages + same tools + same
cache_tag = same reply), which makes re-runs free and repeatable. Use a
different cache_tag per run (r1, r2, r3) to get three independent runs.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

CACHE_FILE = "agent_llm_cache.json"
DEFAULT_MODEL = "openai/gpt-4o-mini"
OPENROUTER_URL = "https://openrouter.ai/api/v1"
MAX_ATTEMPTS = 5
EMPTY_REPLY_ATTEMPTS = 3


def _load_cache(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


class LLM:
    def __init__(self, model=None, temperature=0, use_cache=True, client=None,
                 cache_file=CACHE_FILE):
        self.model = model or os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)
        self.temperature = temperature
        self.use_cache = use_cache
        self.cache_file = cache_file
        self.cache = _load_cache(cache_file) if use_cache else {}
        self._client = client  # tests can pass a fake client
        self.usage = {"calls": 0, "cached_calls": 0, "prompt_tokens": 0,
                      "completion_tokens": 0, "total_tokens": 0}
        self.calls = []

    def chat(self, messages, tools=None, cache_tag=""):
        key = self._cache_key(messages, tools, cache_tag)
        start = time.time()
        if key in self.cache:
            reply = self.cache[key]
            self._record(reply["usage"], time.time() - start, cached=True)
            return reply

        # A reply with no text and no tool call is a failed call (some models return
        # one when they are overloaded or confused). Ask again, and never cache it.
        for attempt in range(EMPTY_REPLY_ATTEMPTS):
            response = self._call_with_retries(messages, tools)
            msg = response.choices[0].message
            if msg.content or msg.tool_calls:
                break
            print(f"  (model returned an empty reply, asking again {attempt + 1}/{EMPTY_REPLY_ATTEMPTS})")
        reply = {
            "content": msg.content,
            "tool_calls": [
                {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                for tc in (msg.tool_calls or [])
            ],
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            },
        }
        self._record(reply["usage"], time.time() - start, cached=False)
        is_empty = not reply["content"] and not reply["tool_calls"]
        if self.use_cache and not is_empty:
            self.cache[key] = reply
            with open(self.cache_file, "w") as f:
                json.dump(self.cache, f, indent=1)
        return reply

    def _client_or_create(self):
        if self._client is None:
            from openai import OpenAI
            if not os.environ.get("OPENROUTER_API_KEY"):
                raise RuntimeError("set OPENROUTER_API_KEY first")
            self._client = OpenAI(base_url=OPENROUTER_URL,
                                  api_key=os.environ["OPENROUTER_API_KEY"])
        return self._client

    def _call_with_retries(self, messages, tools):
        import openai
        retryable = (openai.RateLimitError, openai.APITimeoutError,
                     openai.APIConnectionError, openai.InternalServerError)
        args = {"model": self.model, "messages": messages, "max_tokens": 1500}
        if self.temperature is not None:
            args["temperature"] = self.temperature
        if tools:
            args["tools"] = tools
            args["parallel_tool_calls"] = False

        for attempt in range(MAX_ATTEMPTS):
            try:
                return self._client_or_create().chat.completions.create(**args)
            except retryable as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                wait = 2 ** attempt * 2
                print(f"  (model API: {type(exc).__name__}, retrying in {wait}s)")
                time.sleep(wait)
            # any other error (bad key, bad model) is raised at once

    def _cache_key(self, messages, tools, cache_tag):
        raw = json.dumps([self.model, messages, tools, cache_tag], sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def _record(self, usage, seconds, cached):
        self.usage["calls"] += 1
        if cached:
            self.usage["cached_calls"] += 1
        else:
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                self.usage[k] += usage[k]
        self.calls.append({**usage, "seconds": round(seconds, 2), "cached": cached})
