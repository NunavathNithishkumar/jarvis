"""Real Windows desktop tests. They drive the VS Code probe window (never the host window).

Run: .venv\\Scripts\\python -m pytest -m desktop tests/integration -v
Requires the probe window: code -n artifacts\\probe-workspace
Tests here never approve a command: approval-gated execution (Test A, J) runs
through the UI with a human approving.
"""
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from jarvis_desktop.adapter import AdapterError
from jarvis_desktop.broker import BrokerError, Locator
from jarvis_desktop.emergency_stop import EmergencyStopActive, send_hotkey_for_test
from jarvis_desktop.policy import Action
from jarvis_desktop.runtime import ARTIFACTS, CODE_CLI, build_runtime, probe_config
from jarvis_desktop.workflows.vscode_probe import ProbeWorkflow, terminal_locator

pytestmark = pytest.mark.desktop
RESULTS = ARTIFACTS / "test_results" / "integration_observations.jsonl"


def note(test: str, **obs):
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS, "a", encoding="utf-8") as f:
        f.write(json.dumps({"test": test, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), **obs}, default=str) + "\n")


@pytest.fixture
def rt():
    sentinel = ARTIFACTS / "EMERGENCY_STOP"
    sentinel.unlink(missing_ok=True)
    r = build_runtime(hotkey=True)
    yield r
    r.close()
    sentinel.unlink(missing_ok=True)


def resolve_on_pending(rt, approve: bool):
    def loop():
        for _ in range(1200):
            p = rt.approvals.pending()
            if p:
                rt.approvals.resolve(p[0].request_id, approve, "pytest (deny-only)")
                return
            time.sleep(0.05)
    threading.Thread(target=loop, daemon=True).start()


def task_actions(rt, task_id):
    return [json.loads(l) for l in rt.audit.path.read_text(encoding="utf-8").splitlines()
            if json.loads(l).get("task_id") == task_id]


def test_B_denial_real(rt):
    before = set(Path(rt.broker.probe_dir).glob("run-*.txt")) if rt.broker.probe_dir.exists() else set()
    resolve_on_pending(rt, False)
    wf = ProbeWorkflow(rt.broker, probe_config())
    r = wf.run()
    after = set(Path(rt.broker.probe_dir).glob("run-*.txt"))
    acts = [a["action_type"] for a in task_actions(rt, r["task_id"])]
    note("B_denial", status=r["status"], approval=r["approval_status"], new_files=len(after - before),
         typed=any("type_command" in a for a in acts), steps=[s["name"] for s in r["steps"]])
    assert r["status"] == "CANCELLED" and r["approval_status"] == "denied"
    assert after == before
    assert not any(a.startswith("terminal_command") for a in acts)


def test_C_estop_sentinel_real(rt):
    def trip_when_waiting():
        for _ in range(1200):
            if rt.approvals.pending():
                (ARTIFACTS / "EMERGENCY_STOP").write_text("stop")
                return
            time.sleep(0.05)
    threading.Thread(target=trip_when_waiting, daemon=True).start()
    r = ProbeWorkflow(rt.broker, probe_config()).run()
    t0 = time.time()
    while not rt.estop.tripped and time.time() - t0 < 2:
        time.sleep(0.05)
    source = rt.estop.source
    calls = rt.broker.adapter.call_count
    with pytest.raises(EmergencyStopActive):
        rt.broker.submit(Action("list_windows", params={"app": "Code.exe"}))
    assert rt.broker.adapter.call_count == calls
    ok, msg = rt.broker.rearm("pytest")
    assert not ok                                 # sentinel still present
    (ARTIFACTS / "EMERGENCY_STOP").unlink()
    ok, _ = rt.broker.rearm("pytest")
    note("C_estop_sentinel", status=r["status"], source=source, rearm_ok=ok,
         stop_reason=r["stop_reason"])
    assert r["status"] == "CANCELLED" and ok and source == "sentinel_file"


def test_C_estop_hotkey_real(rt):
    assert rt.hotkey and rt.hotkey.registered.is_set(), rt.hotkey.error if rt.hotkey else "no hotkey"
    send_hotkey_for_test()
    t0 = time.time()
    while not rt.estop.tripped and time.time() - t0 < 3:
        time.sleep(0.05)
    note("C_estop_hotkey", tripped=rt.estop.tripped, source=rt.estop.source,
         latency_ms=round((time.time() - t0) * 1000), synthesized=True)
    assert rt.estop.tripped and rt.estop.source == "global_hotkey"
    with pytest.raises(EmergencyStopActive):
        rt.broker.submit(Action("list_windows"))
    assert rt.broker.rearm("pytest")[0]


def test_D_stale_reference_real(rt):
    wf = ProbeWorkflow(rt.broker, probe_config())
    wf.task.transition(wf.task.state.__class__.READY)
    wf.task.transition(wf.task.state.__class__.RUNNING)
    wf.detect()
    wf.inspect()
    wf.clean_terminals()
    wf.open_terminal()
    old_n = wf.terminal_n
    old = rt.broker.locate(None, wf.window_id, terminal_locator(old_n))
    # Change the UI: kill that terminal and open another one.
    wf.cleanup()
    wf.open_terminal()
    new_n = wf.terminal_n
    # The old ref now points at a removed element.
    raw = None
    try:
        rt.broker.adapter.clear(old["ref_id"])
    except AdapterError as e:
        raw = {"code": e.code, "retry": e.retry, "details": e.details}
    # Broker path: stale ref -> fresh lookup with the original locator -> no match -> refuse (no guessing).
    with pytest.raises(BrokerError) as ei:
        rt.broker.act(None, wf.window_id, "clear", terminal_locator(old_n), ref_hint=old["ref_id"])
    # Same flow with a locator that re-identifies a valid target succeeds after relocation.
    old_menu = rt.broker.locate(None, wf.window_id, Locator("menuitem", name="Terminal", exact=True))
    res = rt.broker.act(None, wf.window_id, "clear", terminal_locator(new_n))
    wf.cleanup()
    note("D_stale_ref", old_terminal=old_n, new_terminal=new_n, raw_adapter_error=raw,
         broker_error=ei.value.code, relocated_ok=res is not None)
    assert raw and raw["code"] in ("STALE_REF", "ELEMENT_NOT_FOUND", "AMBIGUOUS_TARGET")
    assert ei.value.code in ("ELEMENT_NOT_FOUND", "STALE_REF")


def test_E_missing_application_real(rt):
    # A probe scope whose window is genuinely not open.
    cfg = probe_config()
    cfg.window_title_regex = r"(^|\s-\s)probe-workspace-not-open\s-\sVisual Studio Code$"
    r = ProbeWorkflow(rt.broker, cfg).run()
    # Launch path: requested, then denied -> nothing launched.
    resolve_on_pending(rt, False)
    cfg2 = probe_config(launch_if_missing=True)
    cfg2.window_title_regex = cfg.window_title_regex
    r2 = ProbeWorkflow(rt.broker, cfg2).run()
    acts = [a["action_type"] for a in task_actions(rt, r2["task_id"])]
    note("E_missing_app", status=r["status"], error=r["error"], launch_path=r2["error"],
         launched="launch_app" in acts and any(a["action_type"] == "launch_app" and a["status"] == "succeeded"
                                               for a in task_actions(rt, r2["task_id"])))
    assert r["status"] == "FAILED" and r["error"]["code"] == "APP_NOT_FOUND"
    assert r2["error"]["code"] == "LAUNCH_NOT_APPROVED"
    assert not any(a["action_type"] == "launch_app" and a["status"] == "succeeded"
                   for a in task_actions(rt, r2["task_id"]))


def _probe_ids(rt):
    return {w["id"] for w in rt.broker.adapter.list_windows("Code.exe").data
            if "probe-workspace - Visual Studio Code" in w["title"]}


def test_F_ambiguous_window_real(rt):
    twin = ARTIFACTS / "ambiguity" / "probe-workspace"
    twin.mkdir(parents=True, exist_ok=True)
    before = _probe_ids(rt)
    subprocess.Popen([str(CODE_CLI), "-n", str(twin)])
    twin_ids = set()
    try:
        t0 = time.time()
        while time.time() - t0 < 40 and not twin_ids:
            time.sleep(1)
            twin_ids = _probe_ids(rt) - before
        assert twin_ids, "twin window did not open"
        time.sleep(3)
        r = ProbeWorkflow(rt.broker, probe_config()).run()
        note("F_ambiguous", status=r["status"], error=r["error"])
        assert r["status"] == "FAILED" and r["error"]["code"] == "AMBIGUOUS_WINDOW"
        assert len(r["error"]["details"]["candidates"]) >= 2
        # Explicit operator choice resolves it (detection only).
        orig = next(iter(before))
        r2 = ProbeWorkflow(rt.broker, probe_config(window_id=orig, stop_after="detect")).run()
        assert r2["status"] == "COMPLETED" and r2["window_id"] == orig
    finally:
        for wid in twin_ids:
            btn = rt.broker.adapter.find("Code.exe", wid, role="button", name="Close", exact=True).data["matches"]
            if len(btn) == 1:
                rt.broker.adapter.click(btn[0]["ref_id"])


def test_G_input_method_capabilities_real(rt):
    wf = ProbeWorkflow(rt.broker, probe_config())
    wf.task.transition(wf.task.state.__class__.READY)
    wf.task.transition(wf.task.state.__class__.RUNNING)
    wf.detect()
    wf.inspect()
    wf.clean_terminals()
    wf.open_terminal()
    term = rt.broker.locate(None, wf.window_id, terminal_locator(wf.terminal_n))
    obs = {}
    try:
        rt.broker.adapter.type_text(term["ref_id"], "# x", headed=False)
        obs["headless_type"] = "accepted"
    except AdapterError as e:
        obs["headless_type"] = e.code
    # Policy view: free-form typing and set-value are denied through the broker.
    with pytest.raises(BrokerError):
        rt.broker.submit(Action("type_text", window_id=wf.window_id, params={"ref": term["ref_id"]}))
    obs["broker_type_text"] = "POLICY_DENIED"
    wf.cleanup()
    note("G_input_methods", **obs)
    assert obs["headless_type"] == "POLICY_DENIED"
