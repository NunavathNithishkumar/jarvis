"""Phase 2 test harness.

The model is scripted (ScriptedEngine implements OpenJarvis's InferenceEngine ABC);
OpenJarvis's OrchestratorAgent / ToolExecutor / WorkflowGraph / EventBus are the REAL
framework classes. The desktop is the Phase 1 FakeAdapter: none of this is evidence of
real desktop behaviour (see tests/phase2/live for that).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

UNIT = Path(__file__).resolve().parents[1] / "unit"
sys.path.insert(0, str(UNIT))

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("phase1_unit_conftest", UNIT / "conftest.py")
_unit_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_unit_conftest)
Env = _unit_conftest.Env

from openjarvis.engine._stubs import InferenceEngine  # noqa: E402
from openjarvis.core.types import Role  # noqa: E402
from test_broker_workflow import TITLE_RX, vscode_ui  # noqa: E402

from jarvis_desktop.intelligence.config import Budgets, ModelConfig  # noqa: E402
from jarvis_desktop.intelligence.openai_provider import GuardedEngine  # noqa: E402
from jarvis_desktop.intelligence.schemas import Intent  # noqa: E402
from jarvis_desktop.orchestration.openjarvis_runtime import Phase2Orchestrator  # noqa: E402
from jarvis_desktop.workflows.vscode_probe import ProbeConfig  # noqa: E402

MODEL = "gpt-test-model"

GOOD_SCRIPT = """import json
total = sum(range(1, 101))
primes = [n for n in range(2, 50) if all(n % d for d in range(2, int(n ** 0.5) + 1))]
a, b = 0, 1
for _ in range(20):
    a, b = b, a + b
print(json.dumps({"sum_1_to_100": total, "primes_below_50": primes, "fibonacci_20": a}))
"""
WRONG_SCRIPT = GOOD_SCRIPT.replace("range(1, 101)", "range(1, 100)")
CRITERIA = [{"kind": "output_json_equals", "key": "sum_1_to_100", "value": 5050},
            {"kind": "output_json_equals", "key": "primes_below_50",
             "value": [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47]},
            {"kind": "output_json_equals", "key": "fibonacci_20", "value": 6765}]
DEFAULT_PLAN = {"summary": "Create, open and run a Python script, then verify its output", "steps": [
    {"id": "s1", "title": "Inspect VS Code", "tool": "inspect_vscode", "instructions": "find the window",
     "depends_on": [], "success_criteria": "window found"},
    {"id": "s2", "title": "Open a terminal", "tool": "open_terminal", "instructions": "open terminal",
     "depends_on": ["s1"], "success_criteria": "terminal open"},
    {"id": "s3", "title": "Create the script", "tool": "propose_file_creation",
     "instructions": "write calc.py computing the values", "depends_on": ["s2"], "success_criteria": "file exists"},
    {"id": "s4", "title": "Open it in the editor", "tool": "open_file_in_editor", "instructions": "open calc.py",
     "depends_on": ["s3"], "success_criteria": "tab active"},
    {"id": "s5", "title": "Run it", "tool": "run_python_file", "instructions": "run calc.py",
     "depends_on": ["s4"], "success_criteria": "output captured"}]}


def tool_call(name, args, cid="call_1"):
    return {"content": "", "tool_calls": [{"id": cid, "name": name, "arguments": json.dumps(args)}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}, "finish_reason": "tool_calls"}


def text(s):
    return {"content": s, "usage": {"prompt_tokens": 50, "completion_tokens": 10}, "finish_reason": "stop"}


def role_of(messages) -> str:
    sys_msg = next((m.content for m in messages if m.role == Role.SYSTEM), "") or ""
    for key, role in (("intake stage", "intake"), ("planning role", "planner"),
                      ("computer-execution role", "executor"), ("verification role", "verifier")):
        if key in sys_msg:
            return role
    return "unknown"


class ScriptedEngine(InferenceEngine):
    """A deterministic stand-in for the model. `responder(role, messages, kwargs, engine)` -> dict."""
    engine_id = "scripted"

    def __init__(self, responder):
        self.responder = responder
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def generate(self, messages, *, model, temperature=0.7, max_tokens=1024, **kwargs):
        role = role_of(messages)
        with self.lock:
            self.calls.append({"role": role, "messages": list(messages), "kwargs": kwargs})
        return self.responder(role, list(messages), kwargs, self)

    async def stream(self, *a, **k):  # pragma: no cover
        yield ""

    def list_models(self):
        return [MODEL]

    def health(self):
        return True


def step_data(messages) -> dict:
    user = next(m.content for m in messages if m.role == Role.USER)
    return json.loads(user.split("\n", 1)[1])


def last_is_tool(messages) -> bool:
    return messages[-1].role == Role.TOOL


def default_responder(script=GOOD_SCRIPT, plan=None, intent=None, executor_override=None):
    plan = plan or DEFAULT_PLAN

    def respond(role, messages, kwargs, eng):
        if role == "intake":
            return text(json.dumps(intent or {}))
        if role == "planner":
            return text("Plan submitted.") if last_is_tool(messages) else tool_call("submit_plan", plan)
        if role == "executor":
            if last_is_tool(messages):
                return text("Step finished.")
            d = step_data(messages)
            if executor_override:
                r = executor_override(d, messages)
                if r is not None:
                    return r
            tool = d["step"]["tool"]
            files = d["files_created_in_this_task"]
            args = {"propose_file_creation": {"filename": "calc.py", "content": script,
                                              "purpose": "compute sum, primes and fibonacci"},
                    "open_file_in_editor": {"filename": files[-1] if files else "calc.py"},
                    "run_python_file": {"filename": files[-1] if files else "calc.py",
                                        "output_name": f"output{'_' * len(d['outputs_in_this_task'])}.txt"},
                    "read_task_artifact": {"filename": files[-1] if files else "calc.py"}}.get(tool, {})
            return tool_call(tool, args)
        if role == "verifier":
            return text("The program output did not match the expected values.")
        return text("?")
    return respond


def fake_shell(env):
    """Extend the Phase 1 fake VS Code: give `code -r` and `python ... | Tee-Object` real effects."""
    a = env.adapter
    orig = a.on_call

    def on_call(cmd, args):
        orig(cmd, args)
        if cmd != "press" or args[0] != "return" or not a.typed:
            return
        c = a.typed[-1]
        m = re.fullmatch(r"code -r '([^']+)'", c)
        if m:
            for e in a.elements["w-probe"]:
                if e["role"] == "tab" and e["name"] != "Terminal (Ctrl+`)":
                    e["states"] = [s for s in e.get("states", []) if s != "selected"]
            n = len([e for e in a.elements["w-probe"] if e["role"] == "tab"])
            a.elements["w-probe"].append({"role": "tab", "name": Path(m[1]).name, "ref_id": f"@s1:tab{n}",
                                          "states": ["selected"]})
            return
        m = re.fullmatch(r"python '([^']+)' \| Tee-Object -FilePath '([^']+)'", c)
        if m:
            out = subprocess.run([sys.executable, "-I", m[1]], capture_output=True, text=True, timeout=30)
            Path(m[2]).write_text(out.stdout + out.stderr, encoding="utf-8")
    a.on_call = on_call


class AutoApprover:
    """Resolves pending approvals like a human would. decide(req) -> True/False/None (leave pending)."""

    def __init__(self, env, decide=lambda req: True):
        self.env, self.decide, self.seen = env, decide, []
        self._stop = threading.Event()
        self.th = threading.Thread(target=self._loop, daemon=True)
        self.th.start()

    def _loop(self):
        while not self._stop.is_set():
            for r in self.env.approvals.pending():
                if r.request_id in [x.request_id for x in self.seen]:
                    continue
                d = self.decide(r)
                if d is None:
                    continue
                self.seen.append(r)
                self.env.approvals.resolve(r.request_id, bool(d), "test-operator")
            time.sleep(0.02)

    def stop(self):
        self._stop.set()


class P2:
    def __init__(self, env: Env, responder, budgets=None, review=True):
        self.env = env
        ws = (env.tmp / "ws").resolve()
        ws.mkdir(exist_ok=True)
        env.broker.workspace_dir = ws
        env.broker.path_roots["probe_workspace"] = ws
        self.ws = ws
        vscode_ui(env)
        fake_shell(env)
        self.scripted = ScriptedEngine(responder)
        self.engine = GuardedEngine(ModelConfig("openai", MODEL, max_retries=2), budgets or Budgets(),
                                    inner=self.scripted, sleep=lambda s: None)
        self.cfg = ProbeConfig(probe_dir=env.probe_dir, window_title_regex=TITLE_RX, approval_timeout_s=5,
                               cleanup_existing_terminals=False)
        self.orch = Phase2Orchestrator(env.broker, self.engine, MODEL, budgets=budgets or Budgets(),
                                       probe_cfg=self.cfg, workspace=ws, approval_timeout_s=5,
                                       require_plan_review=review)

    def intent(self, criteria=None, caps=None, objective="Create a Python script that computes sum 1..100, "
               "primes below 50 and fibonacci(20); open it in VS Code, run it and verify the output") -> Intent:
        return Intent(intent_type="task", normalized_objective=objective, user_visible_summary=objective[:100],
                      acceptance_criteria=CRITERIA if criteria is None else criteria,
                      requested_applications=["vscode"],
                      required_capabilities=caps or ["vscode_workspace", "create_file", "open_in_editor",
                                                     "run_python", "verify_output"],
                      original_request="test", correlation_id="corr-test123")

