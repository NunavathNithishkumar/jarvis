"""Chat -> intake -> orchestration through the real FastAPI app, JobManager and HTTP approval
endpoints (scripted model, fake desktop)."""
import json
import time

import pytest
from fastapi.testclient import TestClient
from fakes import FakeAdapter
from p2harness import (CRITERIA, MODEL, TITLE_RX, ScriptedEngine, default_responder, fake_shell, text,
                       vscode_ui)

from jarvis_desktop.intelligence.config import Budgets, ModelConfig
from jarvis_desktop.intelligence.openai_provider import GuardedEngine
from jarvis_desktop.phase2_services import Phase2Services
from jarvis_desktop.runtime import build_runtime
from jarvis_desktop.ui.server import create_app
from jarvis_desktop.workflows.vscode_probe import ProbeConfig

TASK_INTENT = {"intent_type": "task", "normalized_objective": "Create, open and run a script computing sum, primes, fib",
               "user_visible_summary": "Sum, primes and fibonacci script", "acceptance_criteria": CRITERIA,
               "requested_applications": ["vscode"],
               "required_capabilities": ["vscode_workspace", "create_file", "open_in_editor", "run_python",
                                         "verify_output"]}
CHAT_INTENT = {"intent_type": "chat", "normalized_objective": "greeting", "user_visible_summary": "greeting",
               "assistant_reply": "Main badhiya hoon! How can I help?"}


@pytest.fixture
def app_env(tmp_path):
    class E:  # adapter for the harness helpers
        pass
    adapter = FakeAdapter()
    rt = build_runtime(artifacts=tmp_path, hotkey=False, sentinel=False, adapter=adapter, restrict_acl=False)
    ws = (tmp_path / "ws").resolve()
    ws.mkdir()
    rt.broker.path_roots["probe_workspace"] = ws
    e = E()
    e.adapter, e.approvals = adapter, rt.approvals
    vscode_ui(e)
    fake_shell(e)
    holder = {}

    def responder(role, messages, kwargs, eng):
        if role == "intake":
            return text(json.dumps(holder["intent"]))
        return default_responder()(role, messages, kwargs, eng)
    scripted = ScriptedEngine(responder)
    engine = GuardedEngine(ModelConfig("openai", MODEL), Budgets(), inner=scripted, sleep=lambda s: None)
    rt.phase2 = Phase2Services(rt, ModelConfig("openai", MODEL), Budgets(), engine=engine,
                               probe_cfg=ProbeConfig(probe_dir=tmp_path, window_title_regex=TITLE_RX,
                                                     approval_timeout_s=10, cleanup_existing_terminals=False),
                               workspace=ws, approval_timeout_s=10)
    client = TestClient(create_app(rt, token="t0k"))
    yield rt, client, holder, adapter, ws
    rt.close()


H = {"X-Jarvis-Token": "t0k"}


def wait_job(rt, approve=True, client=None, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for r in rt.approvals.pending():
            client.post(f"/api/approvals/{r.request_id}", json={"approve": approve}, headers=H)
        if rt.jobs.job and rt.jobs.job.status not in ("running",):
            return rt.jobs.job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_chat_message_answered_without_desktop(app_env):
    rt, client, holder, adapter, ws = app_env
    holder["intent"] = CHAT_INTENT
    assert client.post("/api/chat", json={"text": "kaise ho?"}, headers=H).status_code == 200
    wait_job(rt, client=client)
    p = client.get("/api/phase2/state").json()
    assert p["chat"][-1]["text"].startswith("Main badhiya")
    assert not any(c[0] in ("click", "type", "press", "find", "snapshot") for c in adapter.calls)


def test_chat_requires_token(app_env):
    rt, client, *_ = app_env
    assert client.post("/api/chat", json={"text": "hi"}).status_code == 403


def test_task_via_chat_end_to_end_with_http_approvals(app_env):
    rt, client, holder, adapter, ws = app_env
    holder["intent"] = TASK_INTENT
    client.post("/api/chat", json={"text": "Bhai, ek python script banao, run karke result dikhao"}, headers=H)
    job = wait_job(rt, client=client)
    res = job.runs[-1]
    assert res["outcome"] == "VERIFIED_SUCCESS", res.get("error")
    st = client.get("/api/state").json()          # Phase 1 state endpoint still works with Phase 2 jobs
    assert st["job"]["kind"] == "phase2_chat"
    p = client.get("/api/phase2/state").json()
    assert p["state"]["outcome"] == "VERIFIED_SUCCESS" and p["state"]["plan"]["steps"]
    kinds = {e["kind"] for e in p["events"]}
    assert {"plan", "tool_start", "tool_end", "approval_waiting", "openjarvis_event", "finished"} <= kinds
    lines = [json.loads(l) for l in rt.audit.path.read_text(encoding="utf-8").splitlines()]
    cid = res["correlation_id"]
    assert any(l["action_type"] == "intent_classified" and l.get("correlation_id") == cid for l in lines)
    assert any(l["action_type"] == "llm_call" and l.get("correlation_id") == cid for l in lines)


def test_deny_via_http_blocks(app_env):
    rt, client, holder, adapter, ws = app_env
    holder["intent"] = TASK_INTENT
    client.post("/api/chat", json={"text": "make a script"}, headers=H)
    job = wait_job(rt, approve=False, client=client)
    assert job.runs[-1]["outcome"] == "BLOCKED" and not list(ws.rglob("*.py"))


def test_estop_blocks_chat(app_env):
    rt, client, *_ = app_env
    client.post("/api/estop", headers=H)
    assert client.post("/api/chat", json={"text": "hi"}, headers=H).status_code == 409


def test_missing_model_config_fails_clearly(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_OPENAI_MODEL", raising=False)
    monkeypatch.setattr("jarvis_desktop.intelligence.config._user_env",
                        lambda n: None)
    rt = build_runtime(artifacts=tmp_path, hotkey=False, sentinel=False, adapter=FakeAdapter(), restrict_acl=False)
    client = TestClient(create_app(rt, token="t0k"))
    client.post("/api/chat", json={"text": "hi"}, headers=H)
    deadline = time.time() + 10
    while rt.jobs.job.status == "running" and time.time() < deadline:
        time.sleep(0.05)
    assert rt.jobs.job.status == "error" and "JARVIS_OPENAI_MODEL" in rt.jobs.job.message
    assert rt.broker.adapter.calls == []
    rt.close()


def test_pause_and_stop_controls_reach_phase2_task(app_env):
    rt, client, holder, adapter, ws = app_env
    holder["intent"] = TASK_INTENT
    client.post("/api/chat", json={"text": "make a script"}, headers=H)
    deadline = time.time() + 20
    while time.time() < deadline and not rt.approvals.pending():
        time.sleep(0.05)
    assert rt.jobs.current_task is not None                     # Phase2Run exposes the session Task
    assert client.post("/api/stop", headers=H).status_code == 200
    job = wait_job(rt, client=client)
    assert job.runs[-1]["outcome"] == "BLOCKED" and adapter.typed == []


def test_ui_page_has_chat_and_timeline(app_env):
    rt, client, *_ = app_env
    html = client.get("/").text
    assert "Talk to JARVIS" in html and "Phase 2 plan" in html and "t0k" in html
    assert client.get("/api/phase2/state").json()["state"]["phase"] == "idle"
