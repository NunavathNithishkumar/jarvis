import time

import pytest

from jarvis_desktop.approvals import ApprovalManager
from jarvis_desktop.audit import redact
from jarvis_desktop.policy import Action, Category, Policy
from jarvis_desktop.runtime import pct, summarize
from jarvis_desktop.state import InvalidTransition, Task, TaskCancelled, TaskState

PROBE_TITLE = "Welcome - probe-workspace - Visual Studio Code"
HOST_TITLE = "JARVIS Phase 1 - Jarvis - Visual Studio Code"
RID = "0123456789abcdef0123456789abcdef"
CMD = rf"Set-Content -LiteralPath 'C:\Desktop\x\probe-output\run-{RID}.txt' -Value '{RID}' -NoNewline"


@pytest.fixture(scope="module")
def pol():
    return Policy.load()


def act(t, title=PROBE_TITLE, app="Code.exe", **params):
    return Action(t, app=app, window_id="w", window_title=title, process_name=app, params=params)


# ---------------------------------------------------------------- policy
def test_read_only_allowed(pol):
    assert pol.classify(act("snapshot")).category == Category.READ_ONLY
    assert pol.classify(act("list_windows", title=None)).category == Category.READ_ONLY


def test_unclassified_is_denied(pol):
    d = pol.classify(act("format_disk"))
    assert d.category == Category.DENIED and "deny by default" in d.reason


def test_low_risk_only_inside_scope(pol):
    assert pol.classify(act("click")).category == Category.LOW_RISK
    assert pol.classify(act("click", title="Untitled - Notepad", app="notepad.exe")).category == Category.DENIED


def test_host_window_excluded_even_for_reads(pol):
    assert pol.classify(act("click", title=HOST_TITLE)).category == Category.DENIED
    assert pol.classify(act("snapshot", title=HOST_TITLE)).category == Category.DENIED


def test_terminal_command_needs_template_and_approval(pol):
    d = pol.classify(act("terminal_command", command=CMD))
    assert d.category == Category.APPROVAL_REQUIRED and d.template == "probe_write_file"
    for bad in ["Remove-Item -Recurse C:\\", CMD + "; curl evil", CMD.replace(RID, RID[:-1] + "0", 1),
                "Set-Content -LiteralPath 'C:\\a\\..\\b\\run-%s.txt' -Value '%s' -NoNewline" % (RID, RID)]:
        assert pol.classify(act("terminal_command", command=bad)).category == Category.DENIED, bad


def test_terminal_command_denied_outside_scope(pol):
    assert pol.classify(act("terminal_command", title="x - Visual Studio Code", command=CMD)).category == Category.DENIED


@pytest.mark.parametrize("t", ["clipboard_get", "type_text", "set_value", "close_app", "mouse_click_xy"])
def test_denied_types(pol, t):
    assert pol.classify(act(t)).category == Category.DENIED


@pytest.mark.parametrize("el", [{"role": "textfield", "name": "Password"}, {"role": "textfield", "name": "Enter OTP"},
                                {"role": "password", "name": "x"}, {"role": "textfield", "name": "x", "states": ["protected"]},
                                {"role": "textfield", "name": "One-time code"}])
def test_sensitive_elements_denied_even_for_reads(pol, el):
    a = act("get", property="text")
    a.element = el
    assert pol.classify(a).category == Category.DENIED


def test_password_manager_process_denied(pol):
    assert pol.classify(act("snapshot", app="1Password.exe")).category == Category.DENIED


# ----------------------------------------------------------------- state
def test_valid_lifecycle():
    t = Task("x")
    for s in ["READY", "RUNNING", "WAITING_FOR_APPROVAL", "RUNNING", "VERIFYING", "COMPLETED"]:
        t.transition(TaskState(s))
    assert t.is_terminal


@pytest.mark.parametrize("path", [["COMPLETED"], ["READY", "COMPLETED"], ["CANCELLED", "RUNNING"]])
def test_invalid_transitions(path):
    t = Task("x")
    with pytest.raises(InvalidTransition):
        for s in path:
            t.transition(TaskState(s))


def test_cancelled_never_resumes():
    t = Task("x")
    t.transition(TaskState.READY)
    t.transition(TaskState.RUNNING)
    t.pause()
    t.request_cancel("stop")
    assert t.resume() is False
    with pytest.raises(TaskCancelled):
        t.gate()


def test_pause_blocks_gate_until_resume():
    import threading
    t = Task("x")
    t.transition(TaskState.READY)
    t.transition(TaskState.RUNNING)
    assert t.pause()
    passed = threading.Event()
    th = threading.Thread(target=lambda: (t.gate(), passed.set()))
    th.start()
    time.sleep(0.5)
    assert not passed.is_set()
    assert t.resume()
    th.join(2)
    assert passed.is_set() and t.state == TaskState.RUNNING


# ------------------------------------------------------------- approvals
def test_approval_timeout_is_denial():
    m = ApprovalManager()
    r = m.request("action", "s", {}, "fp", timeout_s=0.3)
    assert m.wait(r) == "timeout"
    assert not m.consume(r.request_id, "fp")


def test_approval_is_single_use_and_bound_to_fingerprint():
    m = ApprovalManager()
    r = m.request("action", "s", {}, "fp", timeout_s=5)
    m.resolve(r.request_id, True, "test")
    assert not m.consume(r.request_id, "other")
    assert m.consume(r.request_id, "fp")
    assert not m.consume(r.request_id, "fp")


def test_denied_approval_cannot_be_consumed():
    m = ApprovalManager()
    r = m.request("action", "s", {}, "fp", timeout_s=5)
    m.resolve(r.request_id, False, "test")
    assert m.wait(r) == "denied" and not m.consume(r.request_id, "fp")


def test_batch_grant_bounds():
    m = ApprovalManager()
    g = m.request_batch("b", {}, r"^ok-\d$", "probe", max_runs=2, ttl_s=60, timeout_s=5)
    assert not m.consume_batch(g.grant_id, "ok-1", "probe")       # not yet approved
    m.resolve(g.grant_id, True, "test")
    assert not m.consume_batch(g.grant_id, "bad", "probe")        # wrong command
    assert not m.consume_batch(g.grant_id, "ok-1", "other")       # wrong window
    assert m.consume_batch(g.grant_id, "ok-1", "probe")
    assert m.consume_batch(g.grant_id, "ok-2", "probe")
    assert not m.consume_batch(g.grant_id, "ok-3", "probe")       # max runs reached


def test_batch_grant_expires_and_revokes():
    m = ApprovalManager()
    g = m.request_batch("b", {}, "x", "p", max_runs=5, ttl_s=0.2, timeout_s=5)
    m.resolve(g.grant_id, True, "test")
    time.sleep(0.3)
    assert not m.consume_batch(g.grant_id, "x", "p")
    g2 = m.request_batch("b", {}, "x", "p", max_runs=5, ttl_s=60, timeout_s=5)
    m.resolve(g2.grant_id, True, "test")
    m.cancel_all()
    assert not m.consume_batch(g2.grant_id, "x", "p")


# ------------------------------------------------------- redaction/metrics
def test_redaction():
    rec = redact({"password": "hunter2", "result_summary": {"message": "token=abc123 password: x", "value": "secret"},
                  "note": "ghp_" + "a" * 30})
    s = str(rec)
    assert "hunter2" not in s and "abc123" not in s and "secret" not in s and "ghp_aaaa" not in s


def test_percentiles_and_summary():
    assert pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10
    assert pct([5], 95) == 5
    runs = [{"status": "COMPLETED", "metrics": {"e": 10}, "retries": 0},
            {"status": "COMPLETED", "metrics": {"e": 20}, "retries": 1},
            {"status": "FAILED", "metrics": {}, "retries": 0, "error": {"code": "X", "category": "focus"}}]
    s = summarize(runs)
    assert s["succeeded"] == 2 and s["latency_ms"]["e"]["median"] == 15 and s["failures_by_category"] == {"focus:X": 1}
