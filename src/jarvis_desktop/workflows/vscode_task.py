"""Multi-step VS Code task (deterministic, non-LLM):

open terminal -> approve a 3-command plan -> create a Python script -> open it in the
editor -> run it with output in the terminal -> independently verify each step.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from ..broker import BrokerError
from ..policy import Action
from ..state import TaskState
from .vscode_probe import ProbeConfig, ProbeWorkflow, _ApprovalNotGiven, terminal_locator

# Single line, no single quotes (PowerShell literal), no $ or backtick (template-enforced).
SCRIPT = ('import json, platform; '
          'primes = [n for n in range(2, 50) if all(n % d for d in range(2, int(n ** 0.5) + 1))]; '
          'print(json.dumps({"sum_1_to_100": sum(range(1, 101)), "primes_below_50": primes, '
          '"fib_20": (lambda f: f(f, 20))(lambda s, n: n if n < 2 else s(s, n - 1) + s(s, n - 2)), '
          '"python": platform.python_version()}))')


def expected_output() -> dict:
    primes = [n for n in range(2, 50) if all(n % d for d in range(2, int(n ** 0.5) + 1))]
    fib = [0, 1]
    while len(fib) <= 20:
        fib.append(fib[-1] + fib[-2])
    return {"sum_1_to_100": 5050, "primes_below_50": primes, "fib_20": fib[20]}


class BuildAndRunTask(ProbeWorkflow):
    def __init__(self, broker, cfg: ProbeConfig, workspace: Path, task=None):
        super().__init__(broker, cfg, task)
        self.task.name = "vscode-build-and-run-python"
        tid = uuid.uuid4().hex[:8]
        self.task_dir = Path(workspace).resolve() / f"task-{tid}"
        # Unique name so the editor-tab check cannot match a tab restored from an earlier run.
        self.script = self.task_dir / f"app_{tid}.py"
        self.out = self.task_dir / "output.txt"
        self.commands = [
            f"New-Item -ItemType File -Force -Path '{self.script}' -Value '{SCRIPT}' | Out-Null",
            f"code -r '{self.script}'",
            f"python '{self.script}' | Tee-Object -FilePath '{self.out}'",
        ]
        self.grant_id: str | None = None
        self.checks: dict = {}

    # ------------------------------------------------------------------ steps
    def approve_plan(self) -> None:
        with self._step("approve_plan") as rec:
            for c in self.commands:  # every command must pass policy before the human even sees it
                d = self.b.policy.classify(Action("terminal_command", app="Code.exe", window_title=self.b.windows[self.window_id]["title"],
                                                  params={"command": c}))
                if d.category.value != "approval_required":
                    raise BrokerError("POLICY_DENIED", f"plan command rejected by policy: {d.reason}", "policy")
            a = self.b.policy.approval_cfg
            grant = self.b.approvals.request_plan(
                "Plan: build and run a Python script in VS Code (3 commands, each runs once)",
                {"action_type": "terminal_command", "application": "Code.exe",
                 "window_title": self.b.windows[self.window_id]["title"],
                 "effects": f"Creates folder {self.task_dir} with app.py and output.txt, opens app.py in the "
                            "editor, runs it with the local Python. No network, no deletion.",
                 "input_method": "Headed keyboard input (SendInput) into the VS Code integrated terminal"},
                self.commands, self.cfg.window_title_regex, ttl_s=600,
                timeout_s=self.cfg.approval_timeout_s or a.get("timeout_s", 120))
            self.b.audit.record(agent_or_component="approvals", action_type="plan_approval_requested", status="pending",
                                task_id=self.task.task_id, approval_status="pending",
                                result_summary={"grant_id": grant.grant_id, "commands": self.commands})
            t0 = time.perf_counter()
            self.task.transition(TaskState.WAITING_FOR_APPROVAL, "plan approval")
            status = self.b.approvals.wait(grant.request, lambda: self.b.estop.tripped or self.task.cancel_requested)
            self.metrics["approval_wait_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            self.b.audit.record(agent_or_component="approvals", action_type="plan_approval_resolved", status=status,
                                task_id=self.task.task_id, approval_status=status,
                                result_summary={"grant_id": grant.grant_id, "actor": grant.request.actor})
            self.approval_status = status
            rec.detail["approval"] = status
            if status != "approved":
                raise _ApprovalNotGiven(status)
            self.task.gate()
            self.task.transition(TaskState.RUNNING, "plan approved")
            self.grant_id = grant.grant_id

    def _run_command(self, i: int, name: str) -> None:
        with self._step(name) as rec:
            term = self.b.locate(self.task, self.window_id, terminal_locator(self.terminal_n), "workflow.vscode_task")
            res = self._submit(Action("terminal_command", window_id=self.window_id,
                                      params={"ref": term["ref_id"], "command": self.commands[i]}),
                               grant_id=self.grant_id)
            self.input_method = res.input_method
            self.metrics[f"{name}_ms"] = round(res.duration_ms, 1)
            rec.detail.update(command=self.commands[i], approval=res.approval_status)

    def _record_check(self, name: str, ok: bool, detail: dict) -> None:
        self.checks[name] = {"ok": ok, **detail}
        self.b.audit.record(agent_or_component="verification", tool_name="filesystem/uia", action_type=name,
                            task_id=self.task.task_id, status="succeeded" if ok else "failed",
                            verification_status="verified" if ok else "failed", result_summary=detail)
        if not ok:
            raise BrokerError("VERIFICATION_FAILED", f"{name}: {detail}", "verification")

    def create_script(self) -> None:
        self._run_command(0, "create_script")
        with self._step("verify_script_on_disk"):
            t0 = time.time()
            while not self.script.exists() and time.time() - t0 < 10:
                time.sleep(0.2)
            content = self.script.read_text(encoding="utf-8-sig").strip() if self.script.exists() else None
            self._record_check("verify_script_on_disk", content == SCRIPT,
                               {"path": str(self.script), "exists": content is not None, "content_matches": content == SCRIPT})

    def open_in_editor(self) -> None:
        self._run_command(1, "open_in_editor")
        with self._step("verify_editor_tab"):
            # Visible evidence through the accessibility tree: an editor tab named app.py in the probe window.
            # Read-only presence check: an exact-name tab for this run's script, selected (active editor).
            name = self.script.name

            def tab_present():
                res = self._submit(Action("find", window_id=self.window_id,
                                          params={"role": "tab", "name": name, "exact": True}))
                return [m for m in res.data.get("matches", []) if "selected" in (m.get("states") or [])]
            tabs = self._poll(tab_present, 15, 1.0) or []
            self.b.foreground_window_id()
            self._record_check("verify_editor_tab", bool(tabs),
                               {"tab": name, "selected_matches": len(tabs),
                                "window_title": self.b.windows[self.window_id]["title"]})

    def run_script(self) -> None:
        self._run_command(2, "run_script")
        with self._step("verify_program_output"):
            t0 = time.time()
            data = None
            while time.time() - t0 < 20 and data is None:
                if self.out.exists():
                    raw = self.out.read_bytes()
                    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
                    try:
                        data = json.loads(text.strip())
                    except json.JSONDecodeError:
                        pass
                if data is None:
                    time.sleep(0.3)
            exp = expected_output()
            ok = bool(data) and all(data.get(k) == v for k, v in exp.items())
            self._record_check("verify_program_output", ok, {"output": data, "expected": exp,
                                                             "fresh": self.out.exists() and self.out.stat().st_mtime >= self.task.created_at - 2})

    # ------------------------------------------------------------------- run
    def run(self) -> dict:
        t = self.task
        self.b.active_task = t
        t0 = time.perf_counter()
        error = None
        try:
            t.transition(TaskState.READY, "configured")
            t.transition(TaskState.RUNNING, "start")
            self.detect()
            self.inspect()
            self.clean_terminals()
            self.open_terminal()
            if self.task_dir.exists():
                raise BrokerError("PRECONDITION_FAILED", f"{self.task_dir} already exists", "verification")
            self.approve_plan()
            self.create_script()
            self.open_in_editor()
            self.run_script()
            t.transition(TaskState.VERIFYING, "all steps verified")
            t.transition(TaskState.COMPLETED, "verified")
        except _ApprovalNotGiven as e:
            error = {"code": f"APPROVAL_{e.status.upper()}", "category": "approval", "message": "nothing executed"}
            if not t.is_terminal:
                t.transition(TaskState.CANCELLED, f"approval {e.status}")
        except Exception as e:
            from ..broker import error_category
            code, cat = error_category(e)
            error = {"code": code, "category": cat, "message": str(e)[:400]}
            if not t.is_terminal:
                t.transition(TaskState.CANCELLED if cat == "interruption" else TaskState.FAILED, code)
        finally:
            t.current_step = t.current_action = None
        self.metrics["end_to_end_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        self.metrics["end_to_end_excl_approval_ms"] = round(
            self.metrics["end_to_end_ms"] - self.metrics.get("approval_wait_ms", 0.0), 1)
        t.error = error
        t.result = self.report()
        t.result.update(task_dir=str(self.task_dir), commands=self.commands, checks=self.checks,
                        verification={"ok": t.state == TaskState.COMPLETED,
                                      "checks": {k: v["ok"] for k, v in self.checks.items()}})
        self.b.audit.record(agent_or_component="workflow.vscode_task", tool_name="workflow", action_type="task_finished",
                            task_id=t.task_id, status=t.state.value, target_application="Code.exe",
                            execution_duration_ms=self.metrics["end_to_end_ms"], retry_count=self.retries,
                            error_code=(error or {}).get("code"), error_category=(error or {}).get("category"),
                            verification_status="verified" if t.state == TaskState.COMPLETED else "failed",
                            approval_status=self.approval_status,
                            result_summary={"task_dir": str(self.task_dir), "checks": self.checks, "metrics": self.metrics})
        return t.result
