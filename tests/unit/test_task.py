import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from jarvis_desktop.approvals import ApprovalManager
from jarvis_desktop.policy import Action, Category, Policy
from jarvis_desktop.workflows.vscode_task import SCRIPT, BuildAndRunTask, expected_output
from test_broker_workflow import TITLE_RX, cfg, vscode_ui

TITLE = "app.py - probe-workspace - Visual Studio Code"


def task_commands(ws: str):
    return [f"New-Item -ItemType File -Force -Path '{ws}\\app.py' -Value '{SCRIPT}' | Out-Null",
            f"code -r '{ws}\\app.py'",
            f"python '{ws}\\app.py' | Tee-Object -FilePath '{ws}\\output.txt'"]


def test_script_produces_expected_output():
    out = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, check=True).stdout
    import json
    data = json.loads(out)
    assert all(data[k] == v for k, v in expected_output().items())


def test_task_commands_match_templates_and_variants_denied():
    p = Policy.load()
    ws = r"C:\Desktop\x\probe-workspace\task-0123abcd"
    for c in task_commands(ws):
        d = p.classify(Action("terminal_command", app="Code.exe", window_title=TITLE, params={"command": c}))
        assert d.category == Category.APPROVAL_REQUIRED and d.template.startswith("task_"), c
    for bad in [task_commands(ws)[2] + "; Remove-Item C:\\x",
                task_commands(ws)[0].replace(SCRIPT, "x' ; rm -r C:\\ ; '"),
                task_commands(ws)[0].replace(SCRIPT, "print($env:SECRET)"),
                f"code -r '{ws}\\..\\..\\app.py'",
                f"python '{ws}\\app.py' | Tee-Object -FilePath 'C:\\Windows\\out.txt'".replace("C:\\Windows", "C:\\Windows\\task-0123abcd")]:
        d = p.classify(Action("terminal_command", app="Code.exe", window_title=TITLE, params={"command": bad}))
        if d.category != Category.DENIED:
            # Template matched; the broker's path-root check must then refuse it.
            assert "Windows" in bad, bad


def test_plan_grant_each_command_once():
    m = ApprovalManager()
    g = m.request_plan("p", {}, ["a b", "c.d"], "probe", ttl_s=60, timeout_s=5)
    m.resolve(g.grant_id, True, "t")
    assert not m.consume_batch(g.grant_id, "cxd", "probe")     # escaped: '.' is literal
    assert m.consume_batch(g.grant_id, "a b", "probe")
    assert not m.consume_batch(g.grant_id, "a b", "probe")     # no replay
    assert m.consume_batch(g.grant_id, "c.d", "probe")


def test_task_path_outside_workspace_denied(env):
    from jarvis_desktop.broker import BrokerError
    env.broker.workspace_dir = (env.tmp / "ws").resolve()
    env.broker.path_roots["probe_workspace"] = env.broker.workspace_dir
    vscode_ui(env)
    env.broker.locate(None, "w-probe", __import__("jarvis_desktop.broker", fromlist=["Locator"]).Locator("menuitem", name="Terminal", exact=True))
    cmd = f"code -r 'C:\\Windows\\task-0123abcd\\app.py'"
    with pytest.raises(BrokerError) as e:
        env.broker.submit(Action("terminal_command", window_id="w-probe", params={"command": cmd, "ref": "@s1:e1"}))
    assert e.value.code == "POLICY_DENIED"


def test_build_and_run_flow_fake(env, tmp_path):
    """Fake shell: executes the three typed commands' effects; checks the workflow verifies each one."""
    ws = (tmp_path / "ws").resolve()
    ws.mkdir()
    env.broker.workspace_dir = ws
    env.broker.path_roots["probe_workspace"] = ws
    vscode_ui(env)
    a = env.adapter
    orig = a.on_call

    def shell(cmd, args):
        orig(cmd, args)
        if cmd != "press" or args[0] != "return" or not a.typed:
            return
        c = a.typed[-1]
        m = re.match(r"New-Item .* -Path '([^']+)' -Value '([^']+)'", c)
        if m:
            Path(m[1]).parent.mkdir(parents=True, exist_ok=True)
            Path(m[1]).write_text(m[2], encoding="utf-8")
        elif c.startswith("code -r"):
            name = re.search(r"(app_[0-9a-f]+\.py)'", c)[1]
            a.elements["w-probe"].append({"role": "tab", "name": "app.py", "ref_id": "@s1:old"})  # restored twin
            a.elements["w-probe"].append({"role": "tab", "name": name, "ref_id": "@s1:tab", "states": ["selected"]})
        elif c.startswith("python"):
            m = re.match(r"python '([^']+)' \| Tee-Object -FilePath '([^']+)'", c)
            out = subprocess.run([sys.executable, m[1]], capture_output=True, text=True).stdout
            Path(m[2]).write_text(out, encoding="utf-8")
    a.on_call = shell

    def approve():
        for _ in range(200):
            p = env.approvals.pending()
            if p:
                assert p[0].kind == "plan" and len(p[0].details["commands"]) == 3
                env.approvals.resolve(p[0].request_id, True, "test")
                return
            time.sleep(0.05)
    threading.Thread(target=approve, daemon=True).start()
    r = BuildAndRunTask(env.broker, cfg(env), ws).run()
    assert r["status"] == "COMPLETED", r["error"]
    assert set(r["checks"]) == {"verify_script_on_disk", "verify_editor_tab", "verify_program_output"}
    assert len(a.typed) == 3 and a.pressed == ["escape", "return"] * 3
