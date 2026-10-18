import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app import engine
from tests import mock_llm

IMMERSIVE_SYSTEM = "You are a professional, authentic machine translation engine."


def immersive_prompt(paragraphs, lang="Simplified Chinese"):
    body = "".join(f"[[p{i}]]\n {p} \n" for i, p in enumerate(paragraphs))
    return {
        "model": "gpt-4.1-mini",
        "messages": [
            {"role": "system", "content": IMMERSIVE_SYSTEM},
            {"role": "user", "content": f"Translate to {lang} Language:\n\n{body}[[source_end]]"},
        ],
    }


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(engine, "COALESCE_SECONDS", 0.05)
    database = dbmod.Database(tmp_path / "test.db")
    dbmod.set_db(database)
    engine.MATCHER = engine.GlossaryMatcher()
    engine.reset_dispatcher()
    mock_llm.reset()
    engine.set_http_client(httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_llm.app)))

    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    engine.set_http_client(None)
    engine.reset_dispatcher()
    dbmod.set_db(None)


def setup_admin(c, max_rpm="0"):
    r = c.post("/api/setup", json={"password": "correct-horse", "team_name": "UNSW CSSA"})
    assert r.status_code == 200, r.text
    r = c.put(
        "/api/settings",
        json={"llm_base_url": "http://mock/v1", "llm_api_key": "sk-test", "llm_model": "mock-1",
              "default_target_lang": "zh-CN", "max_rpm": max_rpm},
    )
    assert r.status_code == 200, r.text
    return c.get("/api/overview").json()["team_key"]


def imme(c, key, texts, lang="zh-CN"):
    return c.post(f"/imme?token={key}", json={"source_lang": "auto", "target_lang": lang, "text_list": texts})


# ------------------------------------------------------------------ auth and settings
def test_setup_login_flow(client):
    assert client.get("/api/me").json()["setup_required"] is True
    r = client.get("/api/overview")
    assert r.status_code == 401 and r.json()["code"] == "not_signed_in"
    setup_admin(client)
    assert client.get("/api/me").json() == {
        "authenticated": True, "setup_required": False, "team_name": "UNSW CSSA", "version": "0.4.0",
    }
    client.post("/api/logout")
    r = client.post("/api/login", json={"password": "nope"})
    assert r.status_code == 401 and r.json()["code"] == "wrong_password"
    assert client.post("/api/login", json={"password": "correct-horse"}).status_code == 200
    assert client.post("/api/setup", json={"password": "another-one"}).json()["code"] == "already_set_up"


def test_env_password(client, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "from-zenith-env")
    assert client.get("/api/me").json()["setup_required"] is False
    assert client.post("/api/login", json={"password": "from-zenith-env"}).status_code == 200


def test_settings_mask_keep_key_and_rpm(client):
    setup_admin(client)
    s = client.get("/api/settings").json()
    assert s["has_api_key"] and "sk-test" not in json.dumps(s)
    client.put("/api/settings", json={"llm_api_key": "", "llm_model": "mock-2", "max_rpm": "10"})
    s = client.get("/api/settings").json()
    assert s["llm_model"] == "mock-2" and s["max_rpm"] == 10
    r = client.put("/api/settings", json={"max_rpm": "lots"})
    assert r.status_code == 400 and r.json()["code"] == "rpm_invalid"
    assert client.post("/api/settings/test").json()["ok"] is True


def test_rotate_key(client):
    old = setup_admin(client)
    new = client.post("/api/team-key/rotate").json()["team_key"]
    assert new != old
    assert imme(client, old, ["x y"]).status_code == 401
    assert imme(client, new, ["x y"]).status_code == 200


def test_immersive_endpoint_requires_key(client):
    setup_admin(client)
    r = client.post("/imme", json={"text_list": ["hi"]})
    assert r.status_code == 401 and r.json()["error"]["code"] == "wrong_team_key"


# ------------------------------------------------------------------ glossary and memory
def test_immersive_glossary_and_cache(client):
    key = setup_admin(client)
    client.post("/api/glossary", json={"source": "census date", "target": "退课截止日"})
    texts = ["The census date is in week 4.", "Fees are due before the Census Date.", "123", "  "]
    out = imme(client, key, texts).json()["translations"]
    assert out[0]["text"] == "[ZH] The 退课截止日 is in week 4."
    assert "退课截止日" in out[1]["text"]
    assert out[2]["text"] == "123" and out[3]["text"] == "  "
    assert out[0]["detected_source_lang"] == "en"
    assert mock_llm.app.state.calls == 1  # two paragraphs, one batched call

    # A teammate translates the same page: served from shared memory, no new upstream call.
    again = client.post("/imme", json={"target_lang": "zh-CN", "text_list": texts},
                        headers={"Authorization": f"Bearer {key}"}).json()["translations"]
    assert [t["text"] for t in again] == [t["text"] for t in out]
    assert mock_llm.app.state.calls == 1

    # Changing the term re-translates exactly the paragraphs that use it.
    term_id = client.get("/api/glossary").json()["terms"][0]["id"]
    client.put(f"/api/glossary/{term_id}", json={"source": "census date", "target": "人口普查截止日"})
    out2 = imme(client, key, texts).json()["translations"]
    assert "人口普查截止日" in out2[0]["text"]
    assert mock_llm.app.state.calls == 2

    stats = client.get("/api/stats").json()
    assert stats["items"] == 6 and stats["cache_hits"] == 2
    assert stats["glossary_compliance"] == 1.0
    assert stats["top_terms"][0]["source"] == "census date"
    assert stats["llm_calls"] == 2 and stats["calls_last_hour"] == 2


def test_plurals_hyphens_and_spaces_match(client):
    key = setup_admin(client)
    client.post("/api/glossary", json={"source": "census date", "target": "退课截止日"})
    client.post("/api/glossary", json={"source": "society", "target": "社团"})
    texts = [
        "Census dates are listed below.",
        "The census-date rule applies.",
        "The census date is Sunday.",
        "The census date's deadline is fixed.",
        "Two societies merged.",
    ]
    items = client.post("/api/translate", json={"text": "\n".join(texts)}).json()["items"]
    for item in items:
        assert item["terms"], item["source"]
        assert all(t["applied"] for t in item["terms"]), item
    assert "社团" in items[4]["text"]


def test_word_boundaries(client):
    key = setup_admin(client)
    client.post("/api/glossary", json={"source": "term", "target": "学期", "force": True})
    out = imme(client, key, ["Determine the terminal state"]).json()
    assert "学期" not in out["translations"][0]["text"]


def test_glossary_language_scoping(client):
    key = setup_admin(client)
    client.post("/api/glossary", json={"source": "lecture", "target": "講義", "target_lang": "ja"})
    assert "講義" not in imme(client, key, ["The lecture"], "zh-CN").json()["translations"][0]["text"]
    assert "講義" in imme(client, key, ["The lecture"], "ja").json()["translations"][0]["text"]


def test_exact_term_paragraphs_skip_the_ai(client):
    key = setup_admin(client)
    client.post("/api/glossary", json={"source": "Key dates", "target": "重要日期"})
    out = imme(client, key, ["Key dates", " Key dates: "]).json()["translations"]
    assert [t["text"] for t in out] == ["重要日期", "重要日期:"]
    assert mock_llm.app.state.calls == 0
    assert client.get("/api/stats").json()["glossary_direct"] == 2


def test_glossary_compliance_retry(client):
    key = setup_admin(client)
    client.post("/api/glossary", json={"source": "census date", "target": "退课截止日"})
    mock_llm.app.state.ignore_glossary = True  # the model ignores the glossary the first time
    out = imme(client, key, ["The census date is Sunday."]).json()["translations"]
    assert "退课截止日" in out[0]["text"]
    assert mock_llm.app.state.calls == 2


def test_batch_split_when_model_breaks_format(client):
    key = setup_admin(client)
    mock_llm.app.state.break_format = True
    out = imme(client, key, ["one apple", "two pears"]).json()["translations"]
    assert [t["text"] for t in out] == ["[ZH] one apple", "[ZH] two pears"]
    assert mock_llm.app.state.calls == 3  # broken batch, then each half on its own


# ------------------------------------------------------------------ OpenAI route
def test_openai_paragraph_mode_caches_per_paragraph(client):
    key = setup_admin(client)
    client.post("/api/glossary", json={"source": "census date", "target": "退课截止日"})
    h = {"Authorization": f"Bearer {key}"}
    r = client.post("/v1/chat/completions", json=immersive_prompt(["Student News", "Census dates", "Events"]), headers=h)
    assert r.status_code == 200, r.text
    content = r.json()["choices"][0]["message"]["content"]
    # "Census dates" is only a glossary term (plural), so it is answered from the glossary directly.
    assert content == "[[p0]]\n[ZH] Student News\n[[p1]]\n退课截止日\n[[p2]]\n[ZH] Events\n[[source_end]]"
    assert r.headers["x-glossa-cache"] == "partial"  # one of three needed no AI
    assert mock_llm.app.state.calls == 1

    # Same paragraphs, grouped differently (another teammate, another scroll position): all from memory.
    r = client.post("/v1/chat/completions", json=immersive_prompt(["Events", "Student News"]), headers=h)
    assert r.json()["choices"][0]["message"]["content"] == "[[p0]]\n[ZH] Events\n[[p1]]\n[ZH] Student News\n[[source_end]]"
    assert r.headers["x-glossa-cache"] == "hit" and mock_llm.app.state.calls == 1

    # A mix of known and new paragraphs only sends the new one.
    r = client.post("/v1/chat/completions", json=immersive_prompt(["Events", "Graduation"]), headers=h)
    assert r.headers["x-glossa-cache"] == "partial" and mock_llm.app.state.batch_sizes[-1] == 1

    # Try it shares the same memory.
    item = client.post("/api/translate", json={"text": "Student News"}).json()["items"][0]
    assert item["cached"] is True
    recent = client.get("/api/stats").json()["recent"]
    assert recent[-1]["items"] == 3  # real paragraph count, not 1 per request


def test_openai_stream_and_fallback_format(client):
    key = setup_admin(client)
    client.post("/api/glossary", json={"source": "tutorial", "target": "辅导课"})
    h = {"Authorization": f"Bearer {key}"}
    plain = {
        "model": "x",
        "messages": [
            {"role": "system", "content": IMMERSIVE_SYSTEM},
            {"role": "user", "content": "Translate the following source text to Simplified Chinese: Tutorial starts in week 2."},
        ],
    }
    r = client.post("/v1/chat/completions", json=plain, headers=h)
    assert "辅导课" in r.json()["choices"][0]["message"]["content"]
    sent_system = mock_llm.app.state.last_messages[0]["content"]
    assert sent_system.startswith("You are a professional") and '"tutorial" → "辅导课"' in sent_system
    r = client.post("/v1/chat/completions", json=plain, headers=h)
    assert r.headers["x-glossa-cache"] == "hit" and mock_llm.app.state.calls == 1

    r = client.post("/v1/chat/completions", json={**immersive_prompt(["Hello"]), "stream": True}, headers=h)
    lines = [l for l in r.text.splitlines() if l.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    assert "[ZH] Hello" in json.loads(lines[0][6:])["choices"][0]["delta"]["content"]


# ------------------------------------------------------------------ coalescing, limits, retries
def test_concurrent_requests_are_merged_into_one_call(client, monkeypatch):
    key = setup_admin(client)
    monkeypatch.setattr(engine, "COALESCE_SECONDS", 0.4)
    h = {"Authorization": f"Bearer {key}"}
    with ThreadPoolExecutor(3) as pool:
        futures = [
            pool.submit(client.post, "/v1/chat/completions", json=immersive_prompt(["Alpha", "Beta"]), headers=h),
            pool.submit(client.post, "/v1/chat/completions", json=immersive_prompt(["Gamma", "Delta"]), headers=h),
            pool.submit(client.post, "/v1/chat/completions", json=immersive_prompt(["Alpha", "Epsilon"]), headers=h),
        ]
        results = [f.result() for f in futures]
    assert all(r.status_code == 200 for r in results)
    assert mock_llm.app.state.calls == 1
    assert mock_llm.app.state.batch_sizes == [5]  # "Alpha" was needed twice but sent once


def test_rate_limit_retry_uses_provider_delay(client):
    key = setup_admin(client)
    mock_llm.app.state.fail_queue = [(429, mock_llm.gemini_429("0.2s"))]
    r = imme(client, key, ["Hello there"])
    assert r.status_code == 200 and r.json()["translations"][0]["text"] == "[ZH] Hello there"
    assert mock_llm.app.state.calls == 2
    stats = client.get("/api/stats").json()
    assert stats["failed_calls_last_hour"] == 1 and stats["errors_24h"] == 0


def test_rate_limit_exhausted_is_reported_as_429(client):
    key = setup_admin(client)
    mock_llm.app.state.fail_queue = [(429, mock_llm.gemini_429("0.1s"))] * 3
    r = imme(client, key, ["Hello there"])
    assert r.status_code == 429 and r.headers.get("retry-after") == "1"
    body = r.json()
    assert body["code"] == "upstream_rate_limit"
    assert "Quota exceeded for metric" in body["raw"]  # full provider message, not cut off
    stats = client.get("/api/stats").json()
    assert stats["errors_24h"] == 1
    assert stats["recent"][0]["error_code"] == "upstream_rate_limit"
    assert "limit: 15" in stats["recent"][0]["error"]


def test_outage_is_retried(client):
    key = setup_admin(client)
    mock_llm.app.state.fail_queue = [(503, {"error": {"message": "The model is overloaded."}})]
    r = imme(client, key, ["Hello again"])
    assert r.status_code == 200 and mock_llm.app.state.calls == 2


def test_failed_batch_does_not_sink_the_others(client, monkeypatch):
    setup_admin(client)
    monkeypatch.setattr(engine, "MAX_BATCH_ITEMS", 2)
    mock_llm.app.state.fail_queue = [(400, {"error": {"message": "bad request"}})]  # not retryable
    r = client.post("/api/translate", json={"text": "one\ntwo\nthree\nfour"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert sum(1 for i in items if i["error_code"]) == 2
    assert sum(1 for i in items if i["text"].startswith("[ZH]")) == 2
    # Retrying only sends the two that failed.
    items = client.post("/api/translate", json={"text": "one\ntwo\nthree\nfour"}).json()["items"]
    assert all(i["text"].startswith("[ZH]") for i in items)
    assert sum(1 for i in items if i["cached"]) == 2


def test_wrong_key_and_model_errors_have_codes(client):
    setup_admin(client)
    client.put("/api/settings", json={"llm_model": "no-such-model"})
    r = client.post("/api/translate", json={"text": "Something new"})
    assert r.status_code == 502 and r.json()["code"] == "upstream_not_found"
    client.put("/api/settings", json={"llm_model": "mock-1", "llm_api_key": "sk-wrong"})
    r = client.post("/api/translate", json={"text": "Something else"})
    assert r.status_code == 502 and r.json()["code"] == "upstream_auth"
    client.put("/api/settings", json={"llm_api_key": "sk-test"})
    assert client.post("/api/translate", json={"text": "Something else"}).status_code == 200


def test_rate_limiter_spaces_calls():
    now = [0.0]
    slept = []

    async def fake_sleep(s):
        slept.append(s)
        now[0] += s

    async def go():
        lim = engine.RateLimiter(clock=lambda: now[0], sleep=fake_sleep)
        for _ in range(3):
            await lim.acquire(2)
        lim.pause_for(5)
        await lim.acquire(0)

    asyncio.run(go())
    assert slept[0] == pytest.approx(60.05) and slept[1] == pytest.approx(5)


def test_parse_google_list_error():
    resp = httpx.Response(429, json=mock_llm.gemini_429("17s"))
    message, retry_after, raw = engine.parse_error_body(resp)
    assert message.startswith("You exceeded your current quota") and retry_after == 17.0
    assert "RESOURCE_EXHAUSTED" in raw


def test_parse_paragraph_prompt():
    text = "Translate to Simplified Chinese Language:\n\n[[p0]]\n Student News \n[[p1]]\n Events \n[[source_end]] "
    assert engine.parse_paragraph_prompt(text) == [(0, "Student News"), (1, "Events")]
    assert engine.parse_paragraph_prompt("Translate this: hello") is None


# ------------------------------------------------------------------ glossary safety (v0.3)
def test_common_or_broad_terms_need_confirmation(client):
    key = setup_admin(client)
    r = client.post("/api/glossary", json={"source": "date", "target": "日期"})
    assert r.status_code == 409 and r.json()["code"] == "term_warning"
    assert "common" in r.json()["params"]["warnings"]
    assert client.get("/api/glossary").json()["terms"] == []  # nothing saved without confirmation

    # A word that is not "common" but appears in most of what the team has read is flagged too.
    imme(client, key, [f"Enrolment step {i} for students" for i in range(12)])
    r = client.post("/api/glossary", json={"source": "enrolment", "target": "注册"})
    params = r.json()["params"]
    assert r.status_code == 409 and params["warnings"] == ["broad"]
    assert params["affected"] == 12 and params["total"] == 12 and params["samples"]

    assert client.post("/api/glossary", json={"source": "enrolment", "target": "注册", "force": True}).status_code == 200
    assert client.post("/api/glossary", json={"source": "census date", "target": "退课截止日"}).status_code == 200
    # Very short terms: flagged unless capitalisation must match (an acronym).
    assert client.post("/api/glossary", json={"source": "WAM", "target": "加权平均分"}).json()["params"]["warnings"] == ["short"]
    assert client.post("/api/glossary", json={"source": "WAM", "target": "加权平均分", "case_sensitive": True}).status_code == 200
    warn = {t["source"]: t["warn"] for t in client.get("/api/glossary").json()["terms"]}
    assert warn == {"census date": [], "enrolment": [], "WAM": []}


def test_notes_reach_the_ai_and_conditional_terms_are_not_forced(client):
    key = setup_admin(client)
    client.post("/api/glossary", json={"source": "census date", "target": "退课截止日", "note": "UNSW drop deadline"})
    client.post("/api/glossary", json={"source": "term", "target": "学期", "force": True, "conditional": True,
                                       "note": "only when it means a university teaching period"})
    out = imme(client, key, ["The census date for this term is Sunday."]).json()["translations"]
    system = mock_llm.app.state.last_messages[0]["content"]
    assert '"census date" → "退课截止日" (note: UNSW drop deadline)' in system
    assert '"term" → "学期" (applies when: only when it means a university teaching period)' in system
    # The mock leaves the conditional term out, as a model would when it does not apply.
    # That must not trigger the forced retry, and must not count against compliance.
    assert "退课截止日" in out[0]["text"] and "学期" not in out[0]["text"]
    assert mock_llm.app.state.calls == 1
    assert client.get("/api/stats").json()["glossary_compliance"] == 1.0
    # A paragraph that is only a conditional term goes to the AI instead of the glossary shortcut.
    imme(client, key, ["Term"])
    assert mock_llm.app.state.calls == 2

    r = client.post("/api/glossary", json={"source": "subject", "target": "科目", "conditional": True})
    assert r.status_code == 400 and r.json()["code"] == "condition_needs_note"


def test_editing_a_note_retranslates_but_old_memory_survives_the_upgrade(client):
    key = setup_admin(client)
    client.post("/api/glossary", json={"source": "census date", "target": "退课截止日"})
    imme(client, key, ["The census date is Sunday."])
    term = client.get("/api/glossary").json()["terms"][0]
    # Terms without notes keep the v0.2 memory key, so upgrading does not throw away cached pages.
    t = engine.Term(1, "census date", "退课截止日", "zh-CN", False, None, None)
    assert engine.signature([t]) == "census date␟退课截止日"
    client.put(f"/api/glossary/{term['id']}", json={**term, "note": "UNSW drop deadline"})
    imme(client, key, ["The census date is Sunday."])
    assert mock_llm.app.state.calls == 2  # the AI now sees the note, so the paragraph is redone once
    imme(client, key, ["The census date is Sunday."])
    assert mock_llm.app.state.calls == 2


def test_history_and_undo(client):
    key = setup_admin(client)
    client.post("/api/glossary", json={"source": "census date", "target": "退课截止日"})
    term = client.get("/api/glossary").json()["terms"][0]
    client.put(f"/api/glossary/{term['id']}", json={**term, "target": "人口普查日"})  # a bad edit
    groups = client.get("/api/glossary/history").json()["groups"]
    assert [g["kind"] for g in groups] == ["edit", "add"]
    assert groups[0]["entries"][0]["prev"]["target"] == "退课截止日"

    assert client.post(f"/api/glossary/history/{groups[0]['id']}/undo").json() == {"undone": 1}
    assert client.get("/api/glossary").json()["terms"][0]["target"] == "退课截止日"
    # Translations follow the restored term straight away.
    assert "退课截止日" in imme(client, key, ["The census date is Sunday."]).json()["translations"][0]["text"]
    r = client.post(f"/api/glossary/history/{groups[0]['id']}/undo")
    assert r.status_code == 409 and r.json()["code"] == "already_undone"

    # Deleting and undoing brings the term back.
    client.delete(f"/api/glossary/{term['id']}")
    delete_id = client.get("/api/glossary/history").json()["groups"][0]["id"]
    client.post(f"/api/glossary/history/{delete_id}/undo")
    assert [t["target"] for t in client.get("/api/glossary").json()["terms"]] == ["退课截止日"]

    # Undoing an old change after the term was edited again would overwrite newer work: refused.
    term = client.get("/api/glossary").json()["terms"][0]
    client.put(f"/api/glossary/{term['id']}", json={**term, "target": "截止日A"})
    old_edit = client.get("/api/glossary/history").json()["groups"][0]["id"]
    client.put(f"/api/glossary/{term['id']}", json={**term, "target": "截止日B"})
    r = client.post(f"/api/glossary/history/{old_edit}/undo")
    assert r.status_code == 409 and r.json()["code"] == "history_conflict"
    assert client.get("/api/glossary").json()["terms"][0]["target"] == "截止日B"


def test_undo_a_whole_import(client):
    setup_admin(client)
    client.post("/api/glossary", json={"source": "census date", "target": "退课截止日"})
    csv_text = "source,target\ncensus date,人口普查日\nWAM,加权平均分\nZID,学号\n"
    assert client.post("/api/glossary/import", json={"csv": csv_text}).json()["added"] == 3
    group = client.get("/api/glossary/history").json()["groups"][0]
    assert group["kind"] == "import" and group["count"] == 3
    assert client.post(f"/api/glossary/history/{group['id']}/undo").json() == {"undone": 3}
    terms = client.get("/api/glossary").json()["terms"]
    assert [(t["source"], t["target"]) for t in terms] == [("census date", "退课截止日")]
    # The undo is itself in the history and can be undone (redo).
    redo = client.get("/api/glossary/history").json()["groups"][0]
    assert redo["kind"] == "undo" and redo["count"] == 3
    client.post(f"/api/glossary/history/{redo['id']}/undo")
    assert len(client.get("/api/glossary").json()["terms"]) == 3


def test_teammates_propose_and_admin_reviews(client):
    key = setup_admin(client)
    client.post("/api/glossary", json={"source": "census date", "target": "人口普查日"})  # wrong
    h = {"Authorization": f"Bearer {key}"}
    glossary = client.get("/api/team/glossary", headers=h).json()
    assert glossary["terms"][0]["target"] == "人口普查日" and glossary["team_name"] == "UNSW CSSA"

    # No key, no access: neither the glossary nor proposals.
    client.post("/api/logout")
    assert client.get("/api/team/glossary").status_code == 401
    r = client.post("/api/team/proposals", json={"source": "x y", "target": "z"})
    assert r.status_code == 401 and r.json()["code"] == "wrong_team_key"
    assert client.post("/api/proposals/1/approve").status_code == 401  # teammates cannot approve

    p1 = client.post("/api/team/proposals", headers=h, json={
        "source": "Census Date", "target": "退课截止日", "note": "It's the drop deadline", "name": "Amy"}).json()["id"]
    again = client.post("/api/team/proposals", headers=h, json={"source": "census date", "target": "退课截止日"}).json()["id"]
    assert again == p1  # the same suggestion twice is kept once
    p2 = client.post("/api/team/proposals", headers=h, json={"source": "tutorial", "target": "辅导课"}).json()["id"]

    client.post("/api/login", json={"password": "correct-horse"})
    pending = client.get("/api/proposals").json()["proposals"]
    assert [p["id"] for p in pending] == [p1, p2]
    assert pending[0]["current"]["target"] == "人口普查日"  # shown next to the existing translation
    assert client.get("/api/overview").json()["pending_proposals"] == 2

    client.post(f"/api/proposals/{p1}/approve", json={})
    client.post(f"/api/proposals/{p2}/reject")
    terms = client.get("/api/glossary").json()["terms"]
    assert [(t["source"], t["target"]) for t in terms] == [("census date", "退课截止日")]  # existing spelling kept
    assert client.get("/api/proposals").json()["proposals"] == []
    assert client.post(f"/api/proposals/{p2}/approve").json()["code"] == "proposal_decided"
    latest = client.get("/api/glossary/history").json()["groups"][0]["entries"][0]
    assert latest["origin"] == "proposal:Amy" and latest["prev"]["target"] == "人口普查日"


def test_teammates_can_suggest_removing_a_term(client):
    key = setup_admin(client)
    client.post("/api/glossary", json={"source": "date", "target": "日期", "force": True})  # a bad term
    h = {"Authorization": f"Bearer {key}"}
    r = client.post("/api/team/proposals", headers=h, json={"kind": "remove", "source": "Date",
                                                           "note": "Too common, it breaks other sentences", "name": "Amy"})
    pid = r.json()["id"]
    again = client.post("/api/team/proposals", headers=h, json={"kind": "remove", "source": "date"}).json()["id"]
    assert again == pid  # no translation needed, and a repeat is kept once
    r = client.post("/api/team/proposals", headers=h, json={"kind": "remove", "source": "no such term"})
    assert r.status_code == 404 and r.json()["code"] == "term_not_found"

    p = client.get("/api/proposals").json()["proposals"][0]
    assert p["kind"] == "remove" and p["current"]["target"] == "日期"
    assert client.post(f"/api/proposals/{pid}/approve", json={}).json() == {"removed": True}
    assert client.get("/api/glossary").json()["terms"] == []
    entry = client.get("/api/glossary/history").json()["groups"][0]["entries"][0]
    assert entry["action"] == "delete" and entry["origin"] == "proposal:Amy"
    # Removed by mistake? The admin can undo it like any other change.
    client.post(f"/api/glossary/history/{entry['id']}/undo")
    assert [t["source"] for t in client.get("/api/glossary").json()["terms"]] == ["date"]


def test_teammates_can_try_translations(client):
    key = setup_admin(client)
    client.post("/api/glossary", json={"source": "census date", "target": "退课截止日"})
    h = {"Authorization": f"Bearer {key}"}
    client.post("/api/logout")  # teammates have no admin session
    assert client.post("/api/team/translate", json={"text": "The census date"}).status_code == 401
    r = client.post("/api/team/translate", headers=h, json={"text": "The census date is Sunday.\nKey dates"})
    assert r.status_code == 200, r.text
    first = r.json()["items"][0]
    assert first["text"] == "[ZH] The 退课截止日 is Sunday."
    assert first["terms"] == [{"source": "census date", "target": "退课截止日", "applied": True, "conditional": False}]
    # Same memory as the extension: a page a teammate already read costs nothing here.
    imme(client, key, ["Student News"])
    item = client.post("/api/team/translate", headers=h, json={"text": "Student News"}).json()["items"][0]
    assert item["cached"] is True
    r = client.post("/api/team/translate", headers=h, json={"text": "x" * 5001})
    assert r.status_code == 413 and r.json()["params"] == {"max": 5000}
    client.post("/api/login", json={"password": "correct-horse"})
    assert [r["endpoint"] for r in client.get("/api/stats").json()["recent"]] == ["member", "immersive", "member"]


# ------------------------------------------------------------------ disabling terms, sets and links (v0.4)
def test_disabling_a_term_keeps_its_memory(client):
    key = setup_admin(client)
    term = client.post("/api/glossary", json={"source": "census date", "target": "退课截止日"}).json()
    text = ["The census date is Sunday."]
    assert "退课截止日" in imme(client, key, text).json()["translations"][0]["text"]
    assert client.post(f"/api/glossary/{term['id']}/enabled", json={"enabled": False}).json()["enabled"] == 0
    assert "退课截止日" not in imme(client, key, text).json()["translations"][0]["text"]
    assert mock_llm.app.state.calls == 2
    # Turning it back on finds the earlier translation in memory: no new AI call.
    client.post(f"/api/glossary/{term['id']}/enabled", json={"enabled": True})
    assert "退课截止日" in imme(client, key, text).json()["translations"][0]["text"]
    assert mock_llm.app.state.calls == 2
    # Both switches are in the history, and undoing one works like any other change.
    groups = client.get("/api/glossary/history").json()["groups"]
    assert [g["entries"][0]["next"]["enabled"] for g in groups[:2]] == [1, 0]
    client.post(f"/api/glossary/history/{groups[0]['id']}/undo")
    assert client.get("/api/glossary").json()["terms"][0]["enabled"] == 0
    # Editing a disabled term keeps it disabled.
    t = client.get("/api/glossary").json()["terms"][0]
    assert client.put(f"/api/glossary/{t['id']}", json={**t, "note": "UNSW"}).json()["enabled"] == 0


def test_links_choose_which_sets_apply(client):
    key = setup_admin(client)
    h = {"Authorization": f"Bearer {key}"}
    stats = client.post("/api/sets", json={"name": "Statistics"}).json()
    client.post("/api/glossary", json={"source": "census date", "target": "退课截止日"})
    client.post("/api/glossary", json={"source": "census date", "target": "人口普查日", "set_id": stats["id"]})
    link = client.post("/api/links", json={"name": "Stats group", "set_ids": [1, stats["id"]]}).json()
    assert link["openai"].endswith(f"/g/{link['slug']}/v1/chat/completions")
    assert link["suggest_url"].endswith(f"&link={link['slug']}")

    text = ["The census date is Sunday.", "Student News"]
    default = imme(client, key, text).json()["translations"]
    assert "退课截止日" in default[0]["text"]
    r = client.post(f"/g/{link['slug']}/imme?token={key}", json={"target_lang": "zh-CN", "text_list": text})
    grouped = r.json()["translations"]
    assert "人口普查日" in grouped[0]["text"]  # Statistics comes before General in this link
    # Paragraphs without a term that differs are shared between links: only the census sentence was new.
    assert mock_llm.app.state.batch_sizes == [2, 1]

    r = client.post(f"/g/{link['slug']}/v1/chat/completions", json=immersive_prompt(["Census dates"]), headers=h)
    assert "人口普查日" in r.json()["choices"][0]["message"]["content"]
    r = client.post(f"/g/nope/v1/chat/completions", json=immersive_prompt(["Hi"]), headers=h)
    assert r.status_code == 404 and r.json()["code"] == "link_not_found"
    assert client.post(f"/g/{link['slug']}/imme", json={"text_list": ["x y"]}).status_code == 401

    # The dashboard's Try it can look through any link.
    item = client.post("/api/translate", json={"text": "The census date is Sunday.", "link_id": link["id"]}).json()
    assert item["items"][0]["cached"] is True and "人口普查日" in item["items"][0]["text"]


def test_teammates_work_inside_their_link(client):
    key = setup_admin(client)
    h = {"Authorization": f"Bearer {key}"}
    stats = client.post("/api/sets", json={"name": "Statistics"}).json()
    client.post("/api/glossary", json={"source": "census date", "target": "退课截止日"})
    client.post("/api/glossary", json={"source": "census date", "target": "人口普查", "set_id": stats["id"]})
    client.post("/api/glossary", json={"source": "tutorial", "target": "辅导课"})
    off = client.post("/api/glossary", json={"source": "lecture", "target": "讲座"}).json()
    client.post(f"/api/glossary/{off['id']}/enabled", json={"enabled": False})
    link = client.post("/api/links", json={"name": "Stats group", "set_ids": [stats["id"], 1]}).json()

    g = client.get(f"/api/team/glossary?link={link['slug']}", headers=h).json()
    assert g["link_name"] == "Stats group"
    assert [(t["source"], t["target"]) for t in g["terms"]] == [("census date", "人口普查"), ("tutorial", "辅导课")]
    default_terms = client.get("/api/team/glossary", headers=h).json()["terms"]
    assert ("census date", "退课截止日") in [(t["source"], t["target"]) for t in default_terms]

    # A fix suggested through the link changes the term in the set that link uses.
    pid = client.post("/api/team/proposals", headers=h, json={
        "link": link["slug"], "source": "census date", "target": "人口普查日"}).json()["id"]
    p = client.get("/api/proposals").json()["proposals"][0]
    assert p["set_id"] == stats["id"] and p["current"]["target"] == "人口普查"
    client.post(f"/api/proposals/{pid}/approve", json={})
    targets = {(t["set_id"], t["target"]) for t in client.get("/api/glossary").json()["terms"] if t["source"] == "census date"}
    assert targets == {(1, "退课截止日"), (stats["id"], "人口普查日")}
    # A new term suggested through the link goes into the link's first set.
    client.post("/api/team/proposals", headers=h, json={"link": link["slug"], "source": "sample mean", "target": "样本均值"})
    assert client.get("/api/proposals").json()["proposals"][0]["set_id"] == stats["id"]
    r = client.post("/api/team/translate", headers=h, json={"link": link["slug"], "text": "The census date"})
    assert "人口普查日" in r.json()["items"][0]["text"]


def test_set_and_link_rules(client):
    setup_admin(client)
    a = client.post("/api/sets", json={"name": "A"}).json()
    b = client.post("/api/sets", json={"name": "B"}).json()
    assert client.post("/api/sets", json={"name": "a"}).json()["code"] == "set_exists"
    assert client.delete("/api/sets/1").json()["code"] == "set_protected"
    client.post("/api/glossary", json={"source": "tutorial", "target": "辅导课", "set_id": a["id"]})
    r = client.delete(f"/api/sets/{a['id']}")
    assert r.status_code == 409 and r.json()["code"] == "set_not_empty" and r.json()["params"] == {"n": 1}
    assert client.delete("/api/links/1").json()["code"] == "link_protected"
    assert client.post("/api/links", json={"name": "X", "set_ids": []}).json()["code"] == "link_needs_set"
    assert client.post("/api/glossary", json={"source": "x y", "target": "z", "set_id": 99}).json()["code"] == "set_not_found"
    # Priority: General is always last; the others can be reordered.
    assert [s["id"] for s in client.get("/api/sets").json()["sets"]] == [a["id"], b["id"], 1]
    client.post(f"/api/sets/{b['id']}/move", json={"direction": -1})
    assert [s["id"] for s in client.get("/api/sets").json()["sets"]] == [b["id"], a["id"], 1]
    link = client.post("/api/links", json={"name": "Both", "set_ids": [1, a["id"], b["id"]]}).json()
    assert link["set_ids"] == [b["id"], a["id"], 1]
    # The same word may exist once per set, not twice in one set.
    t = client.post("/api/glossary", json={"source": "tutorial", "target": "导修课", "set_id": b["id"]}).json()
    r = client.put(f"/api/glossary/{t['id']}", json={**t, "set_id": a["id"]})
    assert r.json()["code"] == "term_exists"


def test_csv_keeps_sets_and_switches(client):
    setup_admin(client)
    csv_text = "source,target,set,enabled\ncensus date,人口普查日,Statistics,1\ntutorial,辅导课,,0\n"
    assert client.post("/api/glossary/import", json={"csv": csv_text}).json() == {"added": 2, "skipped": 0}
    sets = {s["name"]: s["id"] for s in client.get("/api/sets").json()["sets"]}
    terms = {t["source"]: t for t in client.get("/api/glossary").json()["terms"]}
    assert terms["census date"]["set_id"] == sets["Statistics"] and terms["tutorial"]["enabled"] == 0
    exported = client.get("/api/glossary/export").text
    assert "census date,人口普查日,zh-CN,,0,0,Statistics,1" in exported


def test_proposals_are_rate_limited(client, monkeypatch):
    from app import main as mainmod

    key = setup_admin(client)
    monkeypatch.setattr(mainmod.PROPOSAL_LIMITER, "limit", 2)
    mainmod.PROPOSAL_LIMITER.reset("testclient")
    h = {"Authorization": f"Bearer {key}"}
    for word in ("alpha one", "beta two"):
        assert client.post("/api/team/proposals", headers=h, json={"source": word, "target": "x"}).status_code == 200
    r = client.post("/api/team/proposals", headers=h, json={"source": "gamma three", "target": "x"})
    assert r.status_code == 429 and r.json()["code"] == "too_many_proposals"
    mainmod.PROPOSAL_LIMITER.reset("testclient")


# ------------------------------------------------------------------ misc
def test_csv_roundtrip_and_suggest(client):
    setup_admin(client)
    r = client.post("/api/glossary/import", json={"csv": "source,target,note\nWAM,加权平均分,UNSW\nZID,学号,\n,empty,\n"})
    assert r.json() == {"added": 2, "skipped": 1}
    assert "WAM,加权平均分,zh-CN,UNSW,0" in client.get("/api/glossary/export").text
    sugg = client.post("/api/glossary/suggest", json={"text": "The census date for this term is Sunday of week 4."}).json()
    assert sugg["terms"][0]["source"] == "census date"


def test_upgrade_from_v1_database(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, endpoint TEXT NOT NULL, "
        "items INTEGER NOT NULL DEFAULT 0, cache_hits INTEGER NOT NULL DEFAULT 0, llm_calls INTEGER NOT NULL DEFAULT 0, "
        "chars_in INTEGER NOT NULL DEFAULT 0, chars_cached INTEGER NOT NULL DEFAULT 0, "
        "terms_matched INTEGER NOT NULL DEFAULT 0, terms_applied INTEGER NOT NULL DEFAULT 0, "
        "ms INTEGER NOT NULL DEFAULT 0, ok INTEGER NOT NULL DEFAULT 1, error TEXT NOT NULL DEFAULT '');"
        "INSERT INTO events (ts, endpoint, items) VALUES (1, 'openai', 1);"
    )
    con.commit()
    con.close()
    database = dbmod.Database(path)
    database.log_event(endpoint="openai", items=3, glossary_direct=1, error_code="")
    rows = database.query("SELECT items, glossary_direct FROM events ORDER BY id")
    assert rows == [{"items": 1, "glossary_direct": 0}, {"items": 3, "glossary_direct": 1}]


def test_upgrade_from_v2_glossary(tmp_path):
    path = tmp_path / "v2.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE glossary (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, target TEXT NOT NULL, "
        "target_lang TEXT NOT NULL DEFAULT 'zh-CN', note TEXT NOT NULL DEFAULT '', "
        "case_sensitive INTEGER NOT NULL DEFAULT 0, hit_count INTEGER NOT NULL DEFAULT 0, "
        "created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE (source, target_lang));"
        "INSERT INTO glossary (source, target, created_at, updated_at) VALUES ('census date', '退课截止日', 1, 1);"
    )
    con.commit()
    con.close()
    database = dbmod.Database(path)
    row = database.find_term("census date", "zh-CN")
    assert row["conditional"] == 0 and row["target"] == "退课截止日"
    database.update_term(row["id"], target="截止日")
    assert database.list_history()[0]["entries"][0]["prev"]["target"] == "退课截止日"


def test_upgrade_from_v3_proposals(tmp_path):
    path = tmp_path / "v3.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE proposals (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, source TEXT NOT NULL, "
        "target TEXT NOT NULL, target_lang TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '', "
        "client TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending', decided_at REAL, "
        "term_id INTEGER NOT NULL DEFAULT 0);"
        "INSERT INTO proposals (ts, source, target, target_lang) VALUES (1, 'tutorial', '辅导课', 'zh-CN');"
    )
    con.commit()
    con.close()
    database = dbmod.Database(path)
    assert database.pending_proposals()[0]["kind"] == "term"


def test_healthz_and_index(client):
    assert client.get("/healthz").json()["ok"] is True
    assert client.get("/").status_code == 200


def test_upgrade_from_v31_glossary_keeps_ids_and_history(tmp_path):
    path = tmp_path / "v31.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE glossary (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, target TEXT NOT NULL, "
        "target_lang TEXT NOT NULL DEFAULT 'zh-CN', note TEXT NOT NULL DEFAULT '', "
        "case_sensitive INTEGER NOT NULL DEFAULT 0, conditional INTEGER NOT NULL DEFAULT 0, "
        "hit_count INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL, "
        "UNIQUE (source, target_lang));"
        "INSERT INTO glossary (id, source, target, hit_count, created_at, updated_at) VALUES (7, 'census date', '截止日', 5, 1, 1);"
        "CREATE TABLE glossary_history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, action TEXT NOT NULL, "
        "batch TEXT NOT NULL DEFAULT '', term_id INTEGER NOT NULL DEFAULT 0, prev_state TEXT NOT NULL DEFAULT '', "
        "next_state TEXT NOT NULL DEFAULT '', origin TEXT NOT NULL DEFAULT '', undo_of INTEGER NOT NULL DEFAULT 0, "
        "undone INTEGER NOT NULL DEFAULT 0);"
    )
    old = '{"case_sensitive": 0, "conditional": 0, "note": "", "source": "census date", "target_lang": "zh-CN", "target": '
    con.execute("INSERT INTO glossary_history (ts, action, term_id, prev_state, next_state) VALUES (1, 'edit', 7, ?, ?)",
                (old + '"退课截止日"}', old + '"截止日"}'))
    con.commit()
    con.close()
    database = dbmod.Database(path)
    row = database.get_term(7)
    assert (row["set_id"], row["enabled"], row["hit_count"]) == (1, 1, 5)
    # A change saved by v0.3 (no set or switch in it) can still be undone after the upgrade.
    entry = database.list_history()[0]["entries"][0]
    database.undo(entry["id"])
    assert database.get_term(7)["target"] == "退课截止日"
    assert database.list_links()[0]["set_ids"] == [1]

