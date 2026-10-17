"""A fake OpenAI-compatible server for tests and offline demos.

It "translates" by prefixing [ZH] and applying any glossary lines it finds in the system prompt,
so tests can check that the glossary reached the model and that the cache avoids repeat calls.
Run standalone:  uvicorn tests.mock_llm:app --port 9999
"""

from __future__ import annotations

import json
import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()
app.state.calls = 0
app.state.last_messages = []
app.state.break_json = False

GLOSSARY_LINE = re.compile(r'^- "(.+?)" → "(.+?)"$', re.M)


def fake_translate(text: str, glossary: list[tuple[str, str]]) -> str:
    out = text
    for src, tgt in glossary:
        out = re.sub(re.escape(src), tgt, out, flags=re.I)
    return "[ZH] " + out


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    if request.headers.get("authorization") != "Bearer sk-test":
        return JSONResponse({"error": {"message": "Incorrect API key provided"}}, status_code=401)
    app.state.calls += 1
    messages = body["messages"]
    app.state.last_messages = messages
    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    user = "\n".join(m["content"] for m in messages if m["role"] == "user")
    glossary = GLOSSARY_LINE.findall(system)

    if "JSON array of objects" in system:  # glossary suggestion request
        content = json.dumps(
            [
                {"source": "census date", "target": "退课截止日", "note": "UNSW term"},
                {"source": "term", "target": "学期", "note": ""},
            ],
            ensure_ascii=False,
        )
    elif "JSON array of strings" in system:
        items = json.loads(user)
        content = json.dumps([fake_translate(t, glossary) for t in items], ensure_ascii=False)
        if app.state.break_json:
            content = "Sure! Here you go: " + content[:-1]
    else:
        # Proxied prompt from a client: translate whatever follows the last line break.
        content = fake_translate(user, glossary)

    return {
        "id": "mock",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
    }
