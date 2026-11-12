import json
import re
import threading
import time
from pathlib import Path

import pytest

from jarvis_desktop.adapter import AdapterError
from jarvis_desktop.audit import AUDIT_FIELDS
from jarvis_desktop.broker import BrokerError, Locator
from jarvis_desktop.emergency_stop import EmergencyStopActive
from jarvis_desktop.policy import Action
from jarvis_desktop.state import TaskState
from jarvis_desktop.workflows.vscode_probe import ProbeConfig, ProbeWorkflow

TITLE_RX = r"(^|\s-\s)probe-workspace\s-\sVisual Studio Code$"
CMD_RX = re.compile(r"Set-Content -LiteralPath '(?P<p>[^']+)' -Value '(?P<v>[0-9a-f]+)' -NoNewline")


def vscode_ui(env):
    """Simulated VS Code probe window: menus, terminal creation, shell that runs the typed command."""
    a = env.adapter
    base = [{"role": "menuitem", "name": "Terminal", "ref_id": "@s1:e1", "states": ["haspopup"]},
            {"role": "menuitem", "name": "New Terminal Ctrl+Shift+`", "ref_id": "@s1:e2"},
            {"role": "tab", "name": "Terminal (Ctrl+`)", "ref_id": "@s1:e3", "states": ["selected"]},
            {"role": "button", "name": "Kill Terminal", "ref_id": "@s1:e4"}]
    a.elements["w-probe"] = list(base)
    counter = {"n": 0}

    def on_call(cmd, args):
        els = a.elements["w-probe"]
        if cmd == "click" and args[0] == "@s1:e2":
            counter["n"] += 1
            els.append({"role": "textfield", "name": f"Terminal {counter['n']}, pwsh", "ref_id": f"@s1:t{counter['n']}",
                        "states": ["focused"]})
        if cmd == "click" and args[0] == "@s1:e4":
            a.elements["w-probe"] = [e for e in els if not e["name"].startswith("Terminal ") or e["role"] != "textfield"]
        if cmd == "press" and args[0] == "return" and a.typed:
            m = CMD_RX.fullmatch(a.typed[-1])
            if m:
                Path(m["p"]).write_text(m["v"], encoding="utf-8")
    a.on_call = on_call


def cfg(env, **kw):
    return ProbeConfig(probe_dir=env.probe_dir, window_title_regex=TITLE_RX, approval_timeout_s=5,
                       cleanup_existing_terminals=False, **kw)


def approve_when_pending(env, approve=True, delay=0.1):
    def loop():
        for _ in range(100):
            p = env.approvals.pending()
            if p:
                time.sleep(delay)
                env.approvals.resolve(p[0].request_id, approve, "test")
                return
            time.sleep(0.05)
    th = threading.Thread(target=loop, daemon=True)
    th.start()
    return th


# ---------------------------------------------------------- workflow (fake)
def test_A_normal_flow_verified(env):
    vscode_ui(env)
    approve_when_pending(env, True)
    r = ProbeWorkflow(env.broker, cfg(env)).run()
    assert r["status"] == "COMPLETED", r
    assert r["verification"]["ok"] and all(r["verification"]["checks"].values())
    assert r["input_method"] == "headed_sendinput"
    assert Path(r["probe_file"]).read_text() == r["run_id"]


def test_B_denial_executes_nothing(env):
    vscode_ui(env)
    approve_when_pending(env, False)
    r = ProbeWorkflow(env.broker, cfg(env)).run()
    assert r["status"] == "CANCELLED" and r["approval_status"] == "denied"
    assert env.adapter.typed == [] and env.adapter.pressed == []
    assert not any(env.probe_dir.iterdir())  # nothing typed or pressed at all


def test_B_timeout_is_denial(env):
    vscode_ui(env)
    c = cfg(env)
    c.approval_timeout_s = 0.5
    r = ProbeWorkflow(env.broker, c).run()
    assert r["status"] == "CANCELLED" and r["approval_status"] == "timeout"
    assert env.adapter.typed == []


def test_C_estop_during_approval_blocks_everything(env):
    vscode_ui(env)
    def trip():
        for _ in range(100):
            if env.approvals.pending():
                env.estop.trip("test")
                return
            time.sleep(0.05)
    threading.Thread(target=trip, daemon=True).start()
    r = ProbeWorkflow(env.broker, cfg(env)).run()
    assert r["status"] == "CANCELLED"
    assert env.adapter.typed == [] and env.adapter.pressed == []
    n = len(env.adapter.calls)
    with pytest.raises(EmergencyStopActive):
        env.broker.submit(Action("list_windows"))
    assert len(env.adapter.calls) == n            # rejected before reaching the adapter
    assert env.broker.refs == {}                  # outstanding refs invalidated
    assert "emergency_stop" in env.audit_lines()
    ok, _ = env.broker.rearm("test")
    assert ok and not env.estop.tripped


def test_C_rearm_refused_while_sentinel_present(env):
    env.estop.sentinel_path.write_text("")
    env.estop.trip("sentinel_file")
    ok, msg = env.broker.rearm("test")
    assert not ok and "sentinel" in msg


def test_estop_between_type_and_enter_never_presses_enter(env):
    vscode_ui(env)
    approve_when_pending(env, True)
    orig = env.adapter.on_call
    def hook(cmd, args):
        orig(cmd, args)
        if cmd == "type":
            env.estop.trip("test")
    env.adapter.on_call = hook
    r = ProbeWorkflow(env.broker, cfg(env)).run()
    assert r["status"] == "CANCELLED" and "return" not in env.adapter.pressed


def test_D_stale_ref_relocates_and_revalidates(env):
    vscode_ui(env)
    env.broker.locate(None, "w-probe", Locator("menuitem", name="Terminal", exact=True))
    env.adapter.stale_once.add("@s1:e1")
    res = env.broker.act(None, "w-probe", "click", Locator("menuitem", name="Terminal", exact=True), ref_hint="@s1:e1")
    assert res.retries == 1
    clicks = [c for c in env.adapter.calls if c[0] == "click"]
    assert len(clicks) == 2                        # one stale attempt, one after a fresh lookup
    assert "relocate" in env.audit_lines()


def test_D_relocate_refuses_to_guess_between_twins(env):
    env.adapter.elements["w-probe"] = [{"role": "button", "name": "New Terminal", "ref_id": "@s1:a"},
                                       {"role": "button", "name": "New Terminal", "ref_id": "@s1:b"}]
    with pytest.raises(BrokerError) as e:
        env.broker.act(None, "w-probe", "click", Locator("button", name="New Terminal", exact=True))
    assert e.value.code == "AMBIGUOUS_ELEMENT"
    assert not [c for c in env.adapter.calls if c[0] == "click"]


def test_D_unsafe_retry_not_repeated(env):
    vscode_ui(env)
    env.adapter.fail["click"] = AdapterError("STALE_REF", "x", delivery="unknown", retry="unsafe")
    with pytest.raises(AdapterError):
        env.broker.act(None, "w-probe", "click", Locator("menuitem", name="Terminal", exact=True))
    assert len([c for c in env.adapter.calls if c[0] == "click"]) == 1  # attempted once, never repeated
    assert "relocate" not in env.audit_lines()


def test_unissued_ref_rejected(env):
    with pytest.raises(BrokerError) as e:
        env.broker.submit(Action("click", window_id="w-probe", params={"ref": "@forged:e1"}))
    assert e.value.code == "STALE_REF_LOCAL"


def test_E_missing_app_fails_cleanly(env):
    env.adapter.windows = [w for w in env.adapter.windows if w["id"] != "w-probe"]
    r = ProbeWorkflow(env.broker, cfg(env)).run()
    assert r["status"] == "FAILED" and r["error"]["code"] == "APP_NOT_FOUND"
    assert not [c for c in env.adapter.calls if c[0] in ("launch", "click", "type", "press")]


def test_E_launch_requires_approval(env):
    env.adapter.windows = [w for w in env.adapter.windows if w["id"] != "w-probe"]
    approve_when_pending(env, False)
    r = ProbeWorkflow(env.broker, cfg(env, launch_if_missing=True)).run()
    assert r["error"]["code"] == "LAUNCH_NOT_APPROVED"
    assert not [c for c in env.adapter.calls if c[0] == "launch"]


def test_F_ambiguous_window_requires_choice(env):
    vscode_ui(env)
    env.adapter.windows.append(dict(env.adapter.windows[0], id="w-probe2", is_focused=False))
    r = ProbeWorkflow(env.broker, cfg(env)).run()
    assert r["status"] == "FAILED" and r["error"]["code"] == "AMBIGUOUS_WINDOW"
    assert len(r["error"]["details"]["candidates"]) == 2
    approve_when_pending(env, True)
    r2 = ProbeWorkflow(env.broker, cfg(env, window_id="w-probe")).run()
    assert r2["status"] == "COMPLETED" and r2["window_id"] == "w-probe"


def test_host_window_never_targeted(env):
    env.adapter.windows = [w for w in env.adapter.windows if w["id"] != "w-probe"]
    r = ProbeWorkflow(env.broker, cfg(env, window_id="w-host")).run()
    assert r["status"] == "FAILED"
    with pytest.raises(BrokerError):
        env.broker.submit(Action("focus_window", window_id="w-host"))


def test_foreground_mismatch_types_nothing(env):
    vscode_ui(env)
    approve_when_pending(env, True)
    orig_fw = env.adapter.focus_window
    env.adapter.focus_window = lambda wid: (orig_fw(wid), setattr(env.adapter, "foreground", "w-host"))[0]
    r = ProbeWorkflow(env.broker, cfg(env)).run()
    assert r["status"] == "FAILED" and r["error"]["code"] == "FOREGROUND_MISMATCH"
    assert env.adapter.typed == []


def test_pause_resume_does_not_repeat_command(env):
    vscode_ui(env)
    approve_when_pending(env, True)
    wf = ProbeWorkflow(env.broker, cfg(env))
    orig = env.adapter.on_call
    paused = threading.Event()
    def hook(cmd, args):
        orig(cmd, args)
        if cmd == "type" and not paused.is_set():
            paused.set()
            wf.task.pause()
            threading.Timer(0.6, wf.task.resume).start()
    env.adapter.on_call = hook
    r = wf.run()
    assert r["status"] == "COMPLETED"
    assert len(env.adapter.typed) == 1 and env.adapter.pressed == ["escape", "return"]
    assert any(h["to"] == "PAUSED" for h in wf.task.history)


def test_approval_cannot_be_reused_for_other_command(env):
    vscode_ui(env)
    env.broker.locate(None, "w-probe", Locator("menuitem", name="Terminal", exact=True))
    rid = "a" * 32
    cmd = f"Set-Content -LiteralPath '{env.probe_dir / f'run-{rid}.txt'}' -Value '{rid}' -NoNewline"
    req = env.broker.request_approval(Action("terminal_command", window_id="w-probe", params={"command": cmd}),
                                      task=None, summary="s", effects="e")
    env.approvals.resolve(req.request_id, True, "test")
    other = cmd.replace("a" * 32, "b" * 32)
    with pytest.raises(BrokerError) as e:
        env.broker.submit(Action("terminal_command", window_id="w-probe", params={"command": other, "ref": "@s1:e1"}),
                          approval_id=req.request_id)
    assert e.value.code == "APPROVAL_MISSING"


def test_probe_path_outside_probe_dir_denied(env):
    rid = "c" * 32
    cmd = f"Set-Content -LiteralPath 'C:\\Windows\\Temp\\run-{rid}.txt' -Value '{rid}' -NoNewline"
    env.broker.locate(None, "w-probe", Locator("menuitem", name="Terminal", exact=True)) if env.adapter.elements else None
    with pytest.raises(BrokerError) as e:
        env.broker.submit(Action("terminal_command", window_id="w-probe", params={"command": cmd}))
    assert e.value.code == "POLICY_DENIED"


def test_stale_output_file_not_counted(env):
    vscode_ui(env)
    wf = ProbeWorkflow(env.broker, cfg(env))
    cmd, path = wf.build_command()
    path.write_text(wf.run_id)                     # leftover from a "previous run"
    approve_when_pending(env, True)
    r = wf.run()
    assert r["status"] == "FAILED" and r["error"]["code"] == "PRECONDITION_FAILED"
    assert env.adapter.typed == []


# ---------------------------------------------------------- H: credentials
def test_H_sensitive_field_values_hidden_and_actions_denied(env):
    env.adapter.elements["w-probe"] = [{"role": "textfield", "name": "Password", "ref_id": "@s1:pw", "value": "hunter2"}]
    snap = env.broker.submit(Action("snapshot", window_id="w-probe"))
    assert "hunter2" not in json.dumps(snap.data)
    with pytest.raises(BrokerError) as e:
        env.broker.submit(Action("get", window_id="w-probe", params={"ref": "@s1:pw", "property": "value"}))
    assert e.value.code == "POLICY_DENIED"
    with pytest.raises(BrokerError):
        env.broker.submit(Action("click", window_id="w-probe", params={"ref": "@s1:pw"}))
    assert "hunter2" not in env.audit_lines()


def test_H_credential_handoff_suspends_automation(env):
    vscode_ui(env)
    h = env.guard.begin("Code.exe", "probe", "simulated sign-in")
    for a in [Action("snapshot", window_id="w-probe"), Action("find", window_id="w-probe", params={"role": "button"})]:
        with pytest.raises(BrokerError) as e:
            env.broker.submit(a)
        assert e.value.code == "CREDENTIAL_HANDOFF_ACTIVE"
    assert not [c for c in env.adapter.calls if c[0] in ("snapshot", "find")]
    threading.Timer(0.3, env.guard.confirm_complete, args=(h.handoff_id,)).start()
    assert env.guard.wait(h, 5) == "completed"
    env.broker.submit(Action("snapshot", window_id="w-probe"))


# ---------------------------------------------------------- I: audit completeness
def test_I_audit_records_complete(env):
    vscode_ui(env)
    approve_when_pending(env, True)
    r = ProbeWorkflow(env.broker, cfg(env)).run()
    assert r["status"] == "COMPLETED"
    recs = [json.loads(l) for l in env.audit_lines().splitlines()]
    assert recs
    for rec in recs:
        assert set(AUDIT_FIELDS) <= set(rec), set(AUDIT_FIELDS) - set(rec)
        assert rec["timestamp"] and rec["action_type"] and rec["status"]
    term = [x for x in recs if x["action_type"] == "terminal_command"]
    assert term and term[0]["approval_status"] == "approved" and term[0]["execution_duration_ms"] is not None
    assert term[0]["result_summary"]["command_sha256"]
    assert any(x["action_type"] == "approval_resolved" for x in recs)


def test_foreground_lost_after_typing_kills_terminal_no_enter(env):
    vscode_ui(env)
    approve_when_pending(env, True)
    orig = env.adapter.on_call
    def hook(cmd, args):
        orig(cmd, args)
        if cmd == "type":
            env.adapter.foreground = "w-host"     # user clicks another window mid-command
    env.adapter.on_call = hook
    r = ProbeWorkflow(env.broker, cfg(env)).run()
    assert r["status"] == "FAILED" and r["error"]["code"] == "FOREGROUND_LOST"
    assert "return" not in env.adapter.pressed
    assert ("click", "@s1:e4") in env.adapter.calls     # Kill Terminal clicked semantically
    assert not any(env.probe_dir.iterdir())
