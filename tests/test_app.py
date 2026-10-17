import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app import engine
from tests import mock_llm


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    database = dbmod.Database(tmp_path / "test.db")
    dbmod.set_db(database)
    engine.MATCHER = engine.GlossaryMatcher()
    mock_llm.app.state.calls = 0
    mock_llm.app.state.break_json = False
    engine.set_http_client(httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_llm.app)))

    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    engine.set_http_client(None)
    dbmod.set_db(None)


def setup_admin(c):
    r = c.post("/api/setup", json={"password": "correct-horse", "team_name": "UNSW CSSA"})
    assert r.status_code == 200, r.text
    r = c.put(
        "/api/settings",
        json={"llm_base_url": "http://mock/v1", "llm_api_key": "sk-test", "llm_model": "mock-1", "default_target_lang": "zh-CN"},
    )
    assert r.status_code == 200, r.text
    return c.get("/api/overview").json()["team_key"]


def test_setup_login_flow(client):
    assert client.get("/api/me").json()["setup_required"] is True
    assert client.get("/api/overview").status_code == 401
    setup_admin(client)
    assert client.get("/api/me").json() == {
        "authenticated": True, "setup_required": False, "team_name": "UNSW CSSA", "version": "0.1.0",
    }
    client.post("/api/logout")
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"password": "correct-horse"}).status_code == 200
    assert client.post("/api/setup", json={"password": "another-one"}).status_code == 409


def test_env_password(client, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "from-zenith-env")
    assert client.get("/api/me").json()["setup_required"] is False
    assert client.post("/api/login", json={"password": "from-zenith-env"}).status_code == 200


def test_settings_mask_and_keep_key(client):
    setup_admin(client)
    s = client.get("/api/settings").json()
    assert s["has_api_key"] and "sk-test" not in json.dumps(s)
    client.put("/api/settings", json={"llm_api_key": "", "llm_model": "mock-2"})
    assert client.get("/api/settings").json()["llm_model"] == "mock-2"
    assert client.post("/api/settings/test").json()["ok"] is True


def test_immersive_endpoint_requires_key(client):
    setup_admin(client)
    r = client.post("/imme", json={"source_lang": "en", "target_lang": "zh-CN", "text_list": ["hi"]})
    assert r.status_code == 401
    r = client.post("/imme?token=wrong", json={"text_list": ["hi"]})
    assert r.status_code == 401


def test_immersive_glossary_and_cache(client):
    key = setup_admin(client)
    client.post("/api/glossary", json={"source": "census date", "target": "退课截止日"})
    body = {
        "source_lang": "auto",
        "target_lang": "zh-CN",
        "text_list": ["The census date is in week 4.", "Fees are due before the Census Date.", "123", "  "],
    }
    r = client.post(f"/imme?token={key}", json=body)
    assert r.status_code == 200, r.text
    out = r.json()["translations"]
    assert len(out) == 4
    assert out[0]["text"] == "[ZH] The 退课截止日 is in week 4."
    assert "退课截止日" in out[1]["text"]
    assert out[2]["text"] == "123" and out[3]["text"] == "  "
    assert out[0]["detected_source_lang"] == "en"
    assert mock_llm.app.state.calls == 1  # two paragraphs, one batched call

    # A teammate translates the same page: served from shared memory, no new upstream call.
    r = client.post("/imme", json=body, headers={"Authorization": f"Bearer {key}"})
    assert r.json()["translations"] == out
    assert mock_llm.app.state.calls == 1

    # Changing the term invalidates exactly the paragraphs that use it.
    term_id = client.get("/api/glossary").json()["terms"][0]["id"]
    client.put(f"/api/glossary/{term_id}", json={"source": "census date", "target": "人口普查截止日"})
    out2 = client.post(f"/imme?token={key}", json=body).json()["translations"]
    assert "人口普查截止日" in out2[0]["text"]
    assert mock_llm.app.state.calls == 2

    stats = client.get("/api/stats").json()
    assert stats["items"] == 6 and stats["cache_hits"] == 2
    assert stats["glossary_compliance"] == 1.0
    assert stats["top_terms"][0]["source"] == "census date"


def test_batch_falls_back_when_model_breaks_json(client):
    key = setup_admin(client)
    mock_llm.app.state.break_json = True
    r = client.post(f"/imme?token={key}", json={"target_lang": "zh-CN", "text_list": ["one apple", "two pears"]})
    assert r.json()["translations"][1]["text"] == "[ZH] two pears"
    assert mock_llm.app.state.calls == 3  # failed batch + 2 single calls


def test_openai_proxy_injects_glossary_and_caches(client):
    key = setup_admin(client)
    client.post("/api/glossary", json={"source": "tutorial", "target": "辅导课"})
    body = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "You are a professional, authentic machine translation engine."},
            {"role": "user", "content": "Translate the following source text to Simplified Chinese: Tutorial starts in week 2."},
        ],
    }
    h = {"Authorization": f"Bearer {key}"}
    r = client.post("/v1/chat/completions", json=body, headers=h)
    assert r.status_code == 200, r.text
    assert "辅导课" in r.json()["choices"][0]["message"]["content"]
    assert r.headers["x-glossa-cache"] == "miss"
    sent_system = mock_llm.app.state.last_messages[0]["content"]
    assert sent_system.startswith("You are a professional") and '"tutorial" → "辅导课"' in sent_system

    r = client.post("/v1/chat/completions", json=body, headers=h)
    assert r.headers["x-glossa-cache"] == "hit" and mock_llm.app.state.calls == 1

    r = client.post("/v1/chat/completions", json={**body, "stream": True}, headers=h)
    lines = [l for l in r.text.splitlines() if l.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    assert "辅导课" in json.loads(lines[0][6:])["choices"][0]["delta"]["content"]


def test_glossary_language_scoping(client):
    key = setup_admin(client)
    client.post("/api/glossary", json={"source": "lecture", "target": "講義", "target_lang": "ja"})
    out = client.post(f"/imme?token={key}", json={"target_lang": "zh-CN", "text_list": ["The lecture"]}).json()
    assert "講義" not in out["translations"][0]["text"]
    out = client.post(f"/imme?token={key}", json={"target_lang": "ja", "text_list": ["The lecture"]}).json()
    assert "講義" in out["translations"][0]["text"]


def test_word_boundaries(client):
    key = setup_admin(client)
    client.post("/api/glossary", json={"source": "term", "target": "学期"})
    out = client.post(f"/imme?token={key}", json={"target_lang": "zh-CN", "text_list": ["Determine the terminal state"]}).json()
    assert "学期" not in out["translations"][0]["text"]


def test_csv_roundtrip_and_suggest(client):
    setup_admin(client)
    r = client.post("/api/glossary/import", json={"csv": "source,target,note\nWAM,加权平均分,UNSW\nZID,学号,\n,empty,\n"})
    assert r.json() == {"added": 2, "skipped": 1}
    csv_text = client.get("/api/glossary/export").text
    assert "WAM,加权平均分,zh-CN,UNSW,0" in csv_text
    sugg = client.post("/api/glossary/suggest", json={"text": "The census date for this term is Sunday of week 4."}).json()
    assert sugg["terms"][0]["source"] == "census date"


def test_dashboard_translate_and_errors(client):
    setup_admin(client)
    r = client.post("/api/translate", json={"text": "Hello world\n\nSecond line"})
    items = r.json()["items"]
    assert [i["skipped"] for i in items] == [False, True, False]
    r = client.post("/api/translate", json={"text": "Hello world"})
    assert r.json()["items"][0]["cached"] is True
    # Wrong upstream key -> clear error, logged
    client.put("/api/settings", json={"llm_api_key": "sk-wrong"})
    r = client.post("/api/translate", json={"text": "Something new"})
    assert r.status_code == 502 and "Incorrect API key" in r.json()["detail"]
    assert client.get("/api/stats").json()["errors"] == 1


def test_rotate_key(client):
    old = setup_admin(client)
    new = client.post("/api/team-key/rotate").json()["team_key"]
    assert new != old
    assert client.post(f"/imme?token={old}", json={"text_list": ["x y"]}).status_code == 401
    assert client.post(f"/imme?token={new}", json={"text_list": ["x y"]}).status_code == 200


def test_healthz_and_index(client):
    assert client.get("/healthz").json()["ok"] is True
    assert client.get("/").status_code == 200
