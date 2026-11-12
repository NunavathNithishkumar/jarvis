"""Broker file-write policy (Phase 2 scope extension), tool-registry narrowness and the
no-direct-OS-access rule for the planning layer (tests 7, 8, 9, 10)."""
import ast
import sys
from pathlib import Path

import pytest

from jarvis_desktop.broker import BrokerError
from jarvis_desktop.content_guard import check_python
from jarvis_desktop.policy import Action, Category, Policy
from jarvis_desktop.tools.broker_gateway import TOOL_SPECS, BrokerGateway, Phase2Session

SRC = Path(__file__).resolve().parents[2] / "src" / "jarvis_desktop"
P = Policy.load()


def fw(rel, content):
    return P.classify(Action("write_workspace_file", params={"rel_path": rel, "content": content}))


def test_valid_file_write_requires_approval():
    d = fw("task-0123abcd\\calc.py", "import json\nprint(json.dumps({'a': sum(range(5))}))\n")
    assert d.category == Category.APPROVAL_REQUIRED


@pytest.mark.parametrize("rel", ["task-0123abcd\\..\\x.py", "..\\task-0123abcd\\x.py", "task-0123abcd/x.py",
                                 "C:\\Windows\\x.py", "task-0123abcd\\X.py", "task-0123abcd\\x.exe",
                                 "task-0123abcd\\sub\\x.py", "task-zz\\x.py", "task-0123abcd\\x.ps1", ""])
def test_bad_paths_denied(rel):
    assert fw(rel, "print(1)\n").category == Category.DENIED


@pytest.mark.parametrize("code", [
    "import os\nos.remove('x')", "import subprocess", "from os import path", "import socket",
    "open('x','w').write('y')", "exec('1')", "eval('1')", "__import__('os')", "getattr(1, 'x')",
    "().__class__.__bases__", "import json as j\nj.__dict__", "from . import x", "import ctypes",
    "import sys\nsys.exit()", "import shutil", "import urllib.request", "import pathlib", "input()",
    "def f(:\n", "import importlib",
])
def test_dangerous_python_denied(code):
    d = fw("task-0123abcd\\x.py", code + "\n")
    assert d.category == Category.DENIED, code


def test_size_and_empty_limits():
    assert fw("task-0123abcd\\x.txt", "a" * 8193).category == Category.DENIED
    assert fw("task-0123abcd\\x.txt", "").category == Category.DENIED
    assert fw("task-0123abcd\\x.txt", "the cat and the hat").category == Category.APPROVAL_REQUIRED


def test_allowed_stdlib_ok():
    code = "import json, math, re, collections, itertools, functools, statistics, string, textwrap\nprint(1)\n"
    assert check_python(code) == []


def test_broker_denies_without_approval_and_never_overwrites(env):
    ws = (env.tmp / "ws").resolve()
    ws.mkdir()
    env.broker.path_roots["probe_workspace"] = ws
    a = Action("write_workspace_file", params={"rel_path": "task-0123abcd\\a.py", "content": "print(1)\n"})
    with pytest.raises(BrokerError) as e:
        env.broker.submit(a)
    assert e.value.code == "APPROVAL_MISSING" and not (ws / "task-0123abcd").exists()
    req = env.broker.request_approval(a, task=None, summary="s", effects="e", timeout_s=5)
    assert req.details["file_content"] == "print(1)\n"            # human sees the exact content
    env.approvals.resolve(req.request_id, False, "t")              # denied
    with pytest.raises(BrokerError):
        env.broker.submit(Action("write_workspace_file", params=dict(a.params)), approval_id=req.request_id)
    assert not (ws / "task-0123abcd" / "a.py").exists()
    (ws / "task-0123abcd").mkdir()
    (ws / "task-0123abcd" / "a.py").write_text("orig")
    with pytest.raises(BrokerError) as e:                            # overwrite refused before approval
        env.broker.request_approval(a, task=None, summary="s", effects="e", timeout_s=5)
    assert "already exists" in e.value.message
    assert (ws / "task-0123abcd" / "a.py").read_text() == "orig"


def test_no_workspace_root_denies(env):
    env.broker.path_roots.pop("probe_workspace", None)
    a = Action("write_workspace_file", params={"rel_path": "task-0123abcd\\a.py", "content": "print(1)\n"})
    with pytest.raises(BrokerError) as e:
        env.broker.request_approval(a, task=None, summary="s", effects="e", timeout_s=5)
    assert e.value.code == "POLICY_DENIED"


# --------------------------------------------------------------- tool registry (test 7)
def test_tool_registry_is_exactly_the_narrow_set(env):
    s = Phase2Session(env.broker, None, env.tmp, "c") if False else None
    assert set(TOOL_SPECS) == {"inspect_vscode", "open_terminal", "propose_file_creation", "open_file_in_editor",
                               "run_python_file", "read_task_artifact", "get_task_status",
                               "open_website", "play_youtube"}
    for name, spec in TOOL_SPECS.items():
        assert spec["parameters"].get("additionalProperties") is False, name
        props = set(spec["parameters"].get("properties", {}))
        assert not props & {"command", "path", "ref", "x", "y", "keys", "text", "app", "window_id"}, name


# ------------------------------------------------- no direct OS access (test 8)
def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mod = ("." * n.level) + (n.module or "")
            out |= {f"{mod}.{a.name}" for a in n.names} | {mod}
    return out


FORBIDDEN = {"subprocess", "os", "ctypes", "shutil", "socket", "winreg_write", "pyautogui", "pywinauto"}


@pytest.mark.parametrize("pkg", ["intelligence", "orchestration", "tools"])
def test_planning_layer_has_no_os_or_adapter_access(pkg):
    for f in (SRC / pkg).glob("*.py"):
        imps = _imports(f)
        roots = {i.lstrip(".").split(".")[0] for i in imps}
        bad = roots & FORBIDDEN
        if f.name == "config.py":
            bad -= {"os"}      # reads environment variables only
        assert not bad, f"{f.name} imports {bad}"
        assert not any("DesktopAdapter" in i for i in imps), f.name
        src = f.read_text(encoding="utf-8")
        for needle in ("os.system", "Popen", "ShellExecute", "keybd_event", "mouse_event"):
            assert needle not in src, f"{f.name} uses {needle}"


def test_openjarvis_builtin_tools_not_registered(env):
    import openjarvis.tools  # noqa: F401
    from jarvis_desktop.orchestration import openjarvis_runtime
    src = Path(openjarvis_runtime.__file__).read_text(encoding="utf-8")
    for t in ("shell_exec", "file_write", "code_interpreter", "openjarvis.tools.browser", "browser_axtree", "ToolRegistry", "JarvisSystem"):
        assert t not in src


def test_no_posthog_analytics_client_created(env, monkeypatch):
    import posthog
    created = []
    orig = posthog.Posthog.__init__
    monkeypatch.setattr(posthog.Posthog, "__init__", lambda self, *a, **k: (created.append(1), orig(self, *a, **k))[1])
    from p2harness import P2, AutoApprover, default_responder
    p2 = P2(env, default_responder())
    ap = AutoApprover(env)
    r = p2.orch.run(p2.intent())
    ap.stop()
    assert r["outcome"] == "VERIFIED_SUCCESS" and created == []
