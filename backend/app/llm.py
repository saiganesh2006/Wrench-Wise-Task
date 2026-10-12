"""Model layer with token streaming. Two interchangeable providers behind one neutral interface:

  anthropic : ANTHROPIC_API_KEY            (default model claude-sonnet-5-5, router model claude-haiku-5-5)
  openai    : OPENAI_API_KEY and/or OPENAI_BASE_URL - any OpenAI-compatible server (OpenAI, Groq, Together, local Ollama...)

Select with LLM_PROVIDER (auto-detected from env vars if unset). Override models with LLM_MODEL / LLM_ROUTER_MODEL.
With no provider configured, `available()` is False and the app uses its offline (template) mode.

Neutral message format used by agents:
  {"role":"user","content":str}
  {"role":"assistant","text":str,"tool_calls":[{"id","name","args"}]}
  {"role":"tool","results":[{"id","name","content":str}]}
"""
from __future__ import annotations

import json
import os

_clients: dict = {}


def provider() -> str:
    p = os.getenv("LLM_PROVIDER", "").lower()
    if p in ("anthropic", "openai", "none"):
        return p
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_BASE_URL"):
        return "openai"
    return "none"


def available() -> bool:
    return provider() != "none"


def model() -> str:
    m = os.getenv("LLM_MODEL") or os.getenv("ANTHROPIC_MODEL")
    if provider() == "openai":
        return os.getenv("LLM_MODEL") or "gpt-4o-mini"
    return m or "claude-sonnet-5-5"


def router_model() -> str:
    return os.getenv("LLM_ROUTER_MODEL") or ("claude-haiku-5-5" if provider() == "anthropic" else model())


def info() -> dict:
    return {"provider": provider(), "model": model() if available() else None}


def _client():
    p = provider()
    if p not in _clients:
        if p == "anthropic":
            import anthropic
            _clients[p] = anthropic.Anthropic(timeout=45.0, max_retries=1)
        else:
            import openai
            _clients[p] = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY") or "not-needed", timeout=45.0, max_retries=1)
    return _clients[p]


# ------------------------------------------------------------------ one-shot completion (router)
def complete(system: str, user: str, max_tokens: int = 250) -> str:
    if provider() == "anthropic":
        r = _client().messages.create(model=router_model(), max_tokens=max_tokens, system=system,
                                      messages=[{"role": "user", "content": user}])
        return "".join(b.text for b in r.content if b.type == "text").strip()
    r = _client().chat.completions.create(model=router_model(), max_tokens=max_tokens,
                                          messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    return (r.choices[0].message.content or "").strip()


# ------------------------------------------------------------------ streaming turn
def stream_turn(system: str, messages: list[dict], tools: list[dict] | None = None, max_tokens: int = 800):
    """Generator: yields ("text", delta)* then ("turn", {"text": str, "tool_calls": [...]})."""
    if provider() == "anthropic":
        yield from _stream_anthropic(system, messages, tools, max_tokens)
    else:
        yield from _stream_openai(system, messages, tools, max_tokens)


def _anthropic_msgs(messages):
    out = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            blocks = ([{"type": "text", "text": m["text"]}] if m.get("text") else []) + [
                {"type": "tool_use", "id": t["id"], "name": t["name"], "input": t["args"]} for t in m.get("tool_calls", [])]
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]}
                                                    for r in m["results"]]})
    return out


def _stream_anthropic(system, messages, tools, max_tokens):
    kw = dict(model=model(), max_tokens=max_tokens, system=system, messages=_anthropic_msgs(messages))
    if tools:
        kw["tools"] = tools
    with _client().messages.stream(**kw) as s:
        for t in s.text_stream:
            yield ("text", t)
        final = s.get_final_message()
    text = "".join(b.text for b in final.content if b.type == "text")
    calls = [{"id": b.id, "name": b.name, "args": dict(b.input)} for b in final.content if b.type == "tool_use"]
    yield ("turn", {"text": text, "tool_calls": calls})


def _openai_msgs(system, messages):
    out = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            msg = {"role": "assistant", "content": m.get("text") or None}
            if m.get("tool_calls"):
                msg["tool_calls"] = [{"id": t["id"], "type": "function",
                                      "function": {"name": t["name"], "arguments": json.dumps(t["args"])}} for t in m["tool_calls"]]
            out.append(msg)
        else:
            out += [{"role": "tool", "tool_call_id": r["id"], "content": r["content"]} for r in m["results"]]
    return out


def _stream_openai(system, messages, tools, max_tokens):
    kw = dict(model=model(), messages=_openai_msgs(system, messages), max_tokens=max_tokens, stream=True)
    if tools:
        kw["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                         "parameters": t["input_schema"]}} for t in tools]
    text, calls = "", {}
    for chunk in _client().chat.completions.create(**kw):
        if not chunk.choices:
            continue
        d = chunk.choices[0].delta
        if d.content:
            text += d.content
            yield ("text", d.content)
        for tc in (d.tool_calls or []):
            c = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
            if tc.id:
                c["id"] = tc.id
            if tc.function:
                c["name"] += tc.function.name or ""
                c["args"] += tc.function.arguments or ""
    out = []
    for i, c in sorted(calls.items()):
        try:
            args = json.loads(c["args"] or "{}")
        except json.JSONDecodeError:
            args = {}
        out.append({"id": c["id"] or f"call_{i}", "name": c["name"], "args": args})
    yield ("turn", {"text": text, "tool_calls": out})
