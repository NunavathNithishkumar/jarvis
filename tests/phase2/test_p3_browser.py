"""Phase 3 step 1: open websites / play YouTube through the broker (fake browser, scripted model)."""
import json

import pytest
from p2harness import P2, AutoApprover, default_responder, text, tool_call

from jarvis_desktop.broker import BrokerError
from jarvis_desktop.intelligence.config import Budgets
from jarvis_desktop.policy import Action, Category, Policy
from jarvis_desktop.tools.broker_gateway import TOOL_SPECS

P = Policy.load()
YT_PLAN = {"summary": "Play the song on YouTube", "steps": [
    {"id": "s1", "title": "Play Baby on YouTube", "tool": "play_youtube", "instructions": "play it",
     "depends_on": [], "success_criteria": "YouTube shows the video"}]}
SITE_PLAN = {"summary": "Open Wikipedia", "steps": [
    {"id": "s1", "title": "Open Wikipedia", "tool": "open_website", "instructions": "open it",
     "depends_on": [], "success_criteria": "site visible"}]}


def responder(plan, args):
    base = default_responder(plan=plan)

    def r(role, messages, kwargs, eng):
        if role == "executor" and messages[-1].role.value != "tool":
            d = json.loads(next(m.content for m in messages if m.role.value == "user").split("\n", 1)[1])
            return tool_call(d["step"]["tool"], args)
        return base(role, messages, kwargs, eng)
    return r


def intent(p2, cap, text_):
    i = p2.intent(criteria=[{"kind": "window_title_contains", "text": text_}], caps=[cap],
                  objective="play a song" if cap == "play_media" else "open a site")
    i.requested_applications = ["browser"]
    return i


def run(p2, i, decide=lambda r: True):
    ap = AutoApprover(p2.env, decide)
    try:
        return p2.orch.run(i), ap
    finally:
        ap.stop()


@pytest.mark.parametrize("url,ok", [
    ("https://www.youtube.com", True), ("http://example.com/x?q=1", True), ("file:///C:/Windows", False),
    ("javascript:alert(1)", False), ("http://127.0.0.1:8765/", False), ("http://localhost", False),
    ("https://u:p@site.com", False), ("http://10.0.0.5/admin", False), ("http://192.168.1.1", False),
    ("ms-settings:bluetooth", False), ("https://evil.com/\"onload", False), ("", False),
])
def test_url_policy(url, ok):
    d = P.classify(Action("open_url", params={"url": url}))
    assert (d.category == Category.LOW_RISK) is ok, (url, d.reason)


def test_fast_mode_is_policy_owned_and_careful_mode_requires_approval():
    assert P.system_mode == "fast"
    cfg = dict(P.cfg)
    cfg["system_actions"] = dict(cfg["system_actions"], mode="careful")
    careful = Policy(cfg)
    assert careful.classify(Action("play_media", params={"query": "x"})).category == Category.APPROVAL_REQUIRED


def test_unlisted_system_action_denied():
    cfg = dict(P.cfg)
    cfg["system_actions"] = dict(cfg["system_actions"], low_risk=["open_url"])
    assert Policy(cfg).classify(Action("play_media", params={"query": "x"})).category == Category.DENIED


def test_play_youtube_end_to_end_fast_mode_no_approvals(env):
    p2 = P2(env, responder(YT_PLAN, {"query": "Justin Bieber Baby"}))
    r, ap = run(p2, intent(p2, "play_media", "YouTube"))
    assert r["outcome"] == "VERIFIED_SUCCESS", (r["error"], r["verification"])
    assert ap.seen == []                                     # fast mode: no plan review, no approval
    assert env.system.searches == ["Justin Bieber Baby"]
    assert env.system.opened == ["https://www.youtube.com/watch?v=j8n1pj8RfFs"]
    assert "Playing" in r["summary"] and "Baby" in r["summary"]
    checks = {c["check"]: c["ok"] for c in r["verification"]["checks"]}
    assert checks["observed:play_youtube"] and checks["window_title_contains:YouTube"]
    assert not any(c[0] in ("type", "press", "click") for c in env.adapter.calls)   # no keyboard/mouse
    lines = [json.loads(l) for l in env.audit_lines().splitlines()]
    assert any(l["action_type"] == "play_media" and l["status"] == "succeeded" for l in lines)


def test_open_website_end_to_end(env):
    p2 = P2(env, responder(SITE_PLAN, {"url": "https://www.wikipedia.org"}))
    r, _ = run(p2, intent(p2, "open_website", "Wikipedia"))
    assert r["outcome"] == "VERIFIED_SUCCESS", r["verification"]
    assert env.system.opened == ["https://www.wikipedia.org"]


def test_model_cannot_open_local_or_file_urls(env):
    """A (manipulated) model tries the local JARVIS UI; broker policy refuses, nothing opens."""
    p2 = P2(env, responder(SITE_PLAN, {"url": "http://127.0.0.1:8765/api/estop"}), budgets=Budgets(max_replans=0))
    r, _ = run(p2, intent(p2, "open_website", "xx"))
    assert r["outcome"] != "VERIFIED_SUCCESS" and env.system.opened == []
    assert any(t["code"] == "POLICY_DENIED" for t in r["tool_calls"])


def test_careful_mode_asks_and_denial_opens_nothing(env):
    env.broker.policy.system_mode = "careful"
    p2 = P2(env, responder(YT_PLAN, {"query": "baby"}))
    r, ap = run(p2, intent(p2, "play_media", "YouTube"), decide=lambda req: req.kind == "plan_review")
    assert r["outcome"] == "BLOCKED" and env.system.opened == [] and env.system.searches == []
    assert any(a.kind == "action" for a in ap.seen)


def test_emergency_stop_blocks_play(env):
    p2 = P2(env, responder(YT_PLAN, {"query": "baby"}))
    env.estop.trip("test")
    r, _ = run(p2, intent(p2, "play_media", "YouTube"))
    assert r["outcome"] == "BLOCKED" and env.system.opened == []


def test_search_failure_is_not_success(env):
    p2 = P2(env, responder(YT_PLAN, {"query": "baby"}), budgets=Budgets(max_replans=0))
    env.system.fail_search = "NETWORK_ERROR"
    r, _ = run(p2, intent(p2, "play_media", "YouTube"))
    assert r["outcome"] != "VERIFIED_SUCCESS" and env.system.opened == []


def test_new_tools_are_narrow():
    for n in ("open_website", "play_youtube"):
        props = TOOL_SPECS[n]["parameters"]["properties"]
        assert TOOL_SPECS[n]["parameters"]["additionalProperties"] is False and len(props) == 1
