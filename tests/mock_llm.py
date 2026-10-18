"""A fake OpenAI-compatible server for tests and offline demos.

It "translates" by prefixing [ZH] and applying any glossary lines it finds in the system prompt,
so tests can check that the glossary reached the model and that memory avoids repeat calls.

Failure modes (set on app.state):
  fail_queue      list of (status, body) returned for the next calls, e.g. a Gemini-style 429
  break_format    reply to multi-segment batches without markers
  ignore_glossary ignore the glossary unless the prompt is the strict retry

Run standalone:  uvicorn tests.mock_llm:app --port 9999   (API key: sk-test)
Simulate rate limits from a second terminal:  curl -X POST localhost:9999/_fail?count=3&status=429
"""

from __future__ import annotations

import json
import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

# Mandatory terms, optionally followed by "(note: …)". Context-dependent terms end in "(applies when: …)"
# and are ignored, like a model deciding the condition does not apply.
GLOSSARY_LINE = re.compile(r'^- "(.+?)" → "(.+?)"(?: \(note: .*\))?$', re.M)
MARK = re.compile(r"\[\[p(\d+)\]\]")


def reset() -> None:
    app.state.calls = 0
    app.state.last_messages = []
    app.state.batch_sizes = []
    app.state.fail_queue = []
    app.state.break_format = False
    app.state.ignore_glossary = False


reset()


def gemini_429(retry_delay: str = "0.2s") -> list:
    return [{
        "error": {
            "code": 429,
            "message": "You exceeded your current quota, please check your plan and billing details. "
                       "* Quota exceeded for metric: generate_content_free_tier_requests, limit: 15",
            "status": "RESOURCE_EXHAUSTED",
            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}],
        }
    }]


def fake_translate(text: str, glossary: list[tuple[str, str]]) -> str:
    out = text
    for src, tgt in glossary:
        # Accept spacing/hyphen variants and simple plurals or possessives, like a real model would.
        words = [re.escape(w) for w in re.split(r"[\s\-]+", src) if w]
        if words and words[-1].lower().endswith("y"):
            words[-1] = words[-1][:-1] + r"(?:y|ies)"
        pattern = r"[\s\- ]+".join(words) + r"(?:'s|s|es)?"
        out = re.sub(pattern, tgt, out, flags=re.I)
    return "[ZH] " + out


@app.post("/_fail")
async def queue_failures(count: int = 1, status: int = 429):
    body = gemini_429("2s") if status == 429 else {"error": {"message": "The model is overloaded."}}
    app.state.fail_queue.extend([(status, body)] * count)
    return {"queued": len(app.state.fail_queue)}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    if request.headers.get("authorization") != "Bearer sk-test":
        return JSONResponse({"error": {"message": "Incorrect API key provided"}}, status_code=401)
    app.state.calls += 1
    if app.state.fail_queue:
        status, payload = app.state.fail_queue.pop(0)
        return JSONResponse(payload, status_code=status)
    if body.get("model") == "no-such-model":
        return JSONResponse({"error": {"message": "models/no-such-model is not found"}}, status_code=404)

    messages = body["messages"]
    app.state.last_messages = messages
    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    user = "\n".join(m["content"] for m in messages if m["role"] == "user")
    glossary = GLOSSARY_LINE.findall(system)
    strict = "A previous translation ignored" in system
    if app.state.ignore_glossary and not strict:
        glossary = []

    if "JSON array of objects" in system:  # glossary suggestion request
        content = json.dumps(
            [
                {"source": "census date", "target": "退课截止日", "note": "UNSW term"},
                {"source": "term", "target": "学期", "note": ""},
            ],
            ensure_ascii=False,
        )
    elif "Each segment starts with a marker" in system:
        parts = MARK.split(user)
        segments = [parts[i + 1].strip() for i in range(1, len(parts), 2)]
        app.state.batch_sizes.append(len(segments))
        if app.state.break_format and len(segments) > 1:
            content = "Sure! " + " / ".join(fake_translate(s, glossary) for s in segments)
        else:
            content = "\n".join(f"[[p{i}]]\n{fake_translate(s, glossary)}" for i, s in enumerate(segments))
    else:
        # A client's own prompt (not the paragraph format): translate the whole user text.
        content = fake_translate(user, glossary)

    return {
        "id": "mock",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
    }
