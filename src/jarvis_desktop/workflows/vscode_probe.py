"""Deterministic (non-LLM) VS Code workflow:
detect -> inspect -> open terminal -> approve -> execute -> verify -> cleanup.
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..adapter import AdapterError
from ..broker import ActionResult, Broker, BrokerError, Locator, error_category
from ..emergency_stop import EmergencyStopActive
from ..policy import Action
from ..state import Task, TaskCancelled, TaskState
from ..verification import probe_preconditions, verify_probe_file

APP = "Code.exe"
TERMINAL_NAME_RE = re.compile(r"^Terminal (\d+),")
MENU_TERMINAL = Locator("menuitem", name="Terminal", exact=True)
MENU_NEW_TERMINAL = Locator("menuitem", name="New Terminal Ctrl+Shift+`", exact=True)
KILL_TERMINAL = Locator("button", name="Kill Terminal", exact=True)
TERMINAL_TAB = Locator("tab", name="Terminal (Ctrl+`)", exact=True)


def terminal_locator(n: int) -> Locator:
    return Locator("textfield", text=f"Terminal {n}", name_regex=rf"^Terminal {n},")


@dataclass
class ProbeConfig:
    probe_dir: Path
    window_title_regex: str
    expected_window_title: str = "probe-workspace - Visual Studio Code"
    window_id: str | None = None          # explicit operator choice when several windows match
    launch_if_missing: bool = False
    grant_id: str | None = None           # scoped batch approval (reliability runs)
    approval_timeout_s: float | None = None
    cleanup_existing_terminals: bool = True
    kill_terminal_after: bool = True
    stop_after: str | None = None         # test hook: stop cleanly after this step


@dataclass
class StepRecord:
    name: str
    status: str = "pending"
    duration_ms: float = 0.0
    detail: dict = field(default_factory=dict)


class ProbeWorkflow:
    def __init__(self, broker: Broker, cfg: ProbeConfig, task: Task | None = None):
        self.b, self.cfg = broker, cfg
        self.task = task or Task("vscode-terminal-probe")
        self.run_id = uuid.uuid4().hex
        self.steps: list[StepRecord] = []
        self.metrics: dict[str, float] = {}
        self.findings: dict = {}
        self.retries = 0
        self.window_id: str | None = None
        self.terminal_n: int | None = None
        self.verification = None
        self.approval_status: str | None = None
        self.input_method: str | None = None

    # ------------------------------------------------------------------ util
    def _step(self, name: str):
        wf = self

        class _Ctx:
            def __enter__(self_):
                wf.task.gate()
                wf.task.current_step = name
                self_.rec = StepRecord(name, "running")
                wf.steps.append(self_.rec)
                self_.t0 = time.perf_counter()
                return self_.rec

            def __exit__(self_, et, ev, tb):
                self_.rec.duration_ms = round((time.perf_counter() - self_.t0) * 1000, 1)
                self_.rec.status = "failed" if et else "done"
                if ev is not None:
                    self_.rec.detail["error"] = str(ev)[:300]
                else:
                    wf.task.completed_steps.append(name)
                return False
        return _Ctx()

    def _submit(self, action: Action, **kw) -> ActionResult:
        res = self.b.submit(action, task=self.task, component="workflow.vscode_probe", **kw)
        self.retries += res.retries
        return res

    def _act(self, action_type: str, loc: Locator, **kw) -> ActionResult:
        res = self.b.act(self.task, self.window_id, action_type, loc, component="workflow.vscode_probe", **kw)
        self.retries += res.retries
        return res

    def _poll(self, fn, timeout_s: float, interval_s: float = 0.4):
        deadline = time.time() + timeout_s
        last = None
        while True:
            self.task.gate()
            try:
                v = fn()
                if v:
                    return v
            except (BrokerError, AdapterError) as e:
                if isinstance(e, BrokerError) and e.code == "AMBIGUOUS_ELEMENT":
                    raise
                last = e
            if time.time() > deadline:
                if last:
                    raise last
                return None
            time.sleep(interval_s)

    def _terminal_numbers(self) -> set[int]:
        res = self._submit(Action("find", window_id=self.window_id, params={"role": "textfield", "text": "Terminal"}))
        out = set()
        for m in res.data.get("matches", []):
            mm = TERMINAL_NAME_RE.match(m.get("name") or "")
            if mm:
                out.add(int(mm.group(1)))
        return out

    # ------------------------------------------------------------------ steps
    def detect(self) -> None:
        with self._step("detect_vscode") as rec:
            t0 = time.perf_counter()
            wins = self._submit(Action("list_windows", params={"app": APP})).data
            rx = re.compile(self.cfg.window_title_regex)
            cands = [w for w in wins if rx.search(w.get("title") or "")
                     and not self.b.policy.is_excluded_window(w.get("title"))]
            rec.detail["all_code_windows"] = [w.get("title") for w in wins]
            rec.detail["candidates"] = [{"id": w["id"], "title": w["title"]} for w in cands]
            if self.cfg.window_id:
                cands = [w for w in cands if w["id"] == self.cfg.window_id]
                if not cands:
                    raise BrokerError("WINDOW_NOT_FOUND", f"chosen window {self.cfg.window_id} is not a probe window",
                                      "missing_target")
            if not cands:
                cands = self._launch()
            if len(cands) > 1:
                raise BrokerError("AMBIGUOUS_WINDOW",
                                  f"{len(cands)} VS Code windows match the probe scope; choose one explicitly",
                                  "ambiguous_target", {"candidates": rec.detail["candidates"]})
            w = cands[0]
            if w.get("minimized"):
                self._submit(Action("restore_window", window_id=w["id"]))
            self.window_id = w["id"]
            rec.detail["window"] = {"id": w["id"], "title": w["title"], "pid": w.get("pid")}
            self.metrics["detection_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    def _launch(self) -> list[dict]:
        if not self.cfg.launch_if_missing:
            raise BrokerError("APP_NOT_FOUND", "no VS Code probe window is open and launch was not requested",
                              "missing_target")
        action = Action("launch_app", app=APP, window_title=self.cfg.expected_window_title)
        req = self.b.request_approval(action, task=self.task, summary="Launch VS Code probe window",
                                      effects=f"Starts VS Code with: {' '.join(self.b.launch_args)}",
                                      timeout_s=self.cfg.approval_timeout_s)
        status = self.b.wait_for_approval(req, self.task)
        if status != "approved":
            raise BrokerError("LAUNCH_NOT_APPROVED", f"launch approval {status}", "approval")
        self._submit(Action("launch_app", app=APP, window_title=self.cfg.expected_window_title),
                     approval_id=req.request_id)
        rx = re.compile(self.cfg.window_title_regex)

        def found():
            wins = self._submit(Action("list_windows", params={"app": APP})).data
            return [w for w in wins if rx.search(w.get("title") or "")]
        cands = self._poll(found, 40, 1.0)
        if not cands:
            raise BrokerError("APP_START_TIMEOUT", "VS Code probe window did not appear", "timeout")
        return cands

    def inspect(self) -> None:
        with self._step("inspect_accessibility_tree") as rec:
            t0 = time.perf_counter()
            snap = self._submit(Action("snapshot", window_id=self.window_id,
                                       params={"skeleton": True, "timeout_ms": 20000}))
            self.metrics["snapshot_ms"] = round(snap.duration_ms, 1)
            rec.detail["skeleton_ref_count"] = snap.data.get("ref_count")
            rec.detail["complete"] = snap.data.get("complete")
            # Electron renderer may still be settling: retry the lookup with a bounded wait.
            t1 = time.perf_counter()
            m = self._poll(lambda: self.b.locate(self.task, self.window_id, MENU_TERMINAL, "workflow.vscode_probe"), 25, 1.0)
            if not m:
                raise BrokerError("ELEMENT_NOT_FOUND", "Terminal menu not exposed by the accessibility tree",
                                  "missing_target")
            self.metrics["target_resolution_ms"] = round((time.perf_counter() - t1) * 1000, 1)
            rec.detail["terminal_menu_ref"] = m["ref_id"]
            self.metrics["inspect_total_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    def clean_terminals(self) -> None:
        if not self.cfg.cleanup_existing_terminals:
            return
        with self._step("cleanup_preexisting_terminals") as rec:
            killed = 0
            for _ in range(6):
                if not self._terminal_numbers():
                    break
                try:
                    self._act("kill_terminal", KILL_TERMINAL)
                    killed += 1
                except BrokerError as e:
                    rec.detail["warning"] = e.code
                    break
                time.sleep(0.6)
            rec.detail["killed"] = killed

    def open_terminal(self) -> None:
        with self._step("open_terminal") as rec:
            before = self._terminal_numbers()
            t0 = time.perf_counter()
            self._act("click", MENU_TERMINAL)
            self._poll(lambda: self.b.locate(self.task, self.window_id, MENU_NEW_TERMINAL, "workflow.vscode_probe"), 5)
            self._act("click", MENU_NEW_TERMINAL)
            self.metrics["menu_action_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            rec.detail["method"] = "semantic menu: Terminal > New Terminal"

            def new_terminal():
                return sorted(self._terminal_numbers() - before)
            new = self._poll(new_terminal, 20, 0.5)
            self.metrics["terminal_init_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            if not new:
                raise BrokerError("TERMINAL_NOT_OPENED", "no new terminal appeared after New Terminal", "verification")
            self.terminal_n = new[-1]
            tab = self.b.locate(self.task, self.window_id, TERMINAL_TAB, "workflow.vscode_probe")
            rec.detail.update(terminal=f"Terminal {self.terminal_n}", before=sorted(before),
                              terminal_tab_states=tab.get("states"))
            if "selected" not in (tab.get("states") or []):
                raise BrokerError("TERMINAL_NOT_VISIBLE", "terminal tab is not selected", "verification")

    def build_command(self) -> tuple[str, Path]:
        path = (Path(self.cfg.probe_dir) / f"run-{self.run_id}.txt").resolve()
        return f"Set-Content -LiteralPath '{path}' -Value '{self.run_id}' -NoNewline", path

    def approve_and_execute(self) -> None:
        cmd, path = self.build_command()
        Path(self.cfg.probe_dir).mkdir(parents=True, exist_ok=True)
        pre = probe_preconditions(path, self.cfg.probe_dir)
        self.pre = pre
        if not all(pre.values()):
            raise BrokerError("PRECONDITION_FAILED", f"probe preconditions failed: {pre}", "verification")
        self.probe_path, self.command = path, cmd
        approval_id = None
        with self._step("request_approval") as rec:
            rec.detail["command"] = cmd
            if self.cfg.grant_id:
                self.approval_status = "batch_grant"
                rec.detail["grant_id"] = self.cfg.grant_id
                self.metrics["approval_wait_ms"] = 0.0
            else:
                action = Action("terminal_command", window_id=self.window_id, params={
                    "command": cmd,
                    "input_method_note": "Headed keyboard input (SendInput) into the VS Code integrated terminal, then Enter"})
                req = self.b.request_approval(
                    action, task=self.task, summary=f"Run probe command in VS Code terminal {self.terminal_n}",
                    effects=f"Creates one new file {path} containing the run id. No network, no deletion.",
                    timeout_s=self.cfg.approval_timeout_s)
                t0 = time.perf_counter()
                status = self.b.wait_for_approval(req, self.task)
                self.metrics["approval_wait_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                self.approval_status = status
                rec.detail["approval"] = status
                if status != "approved":
                    raise _ApprovalNotGiven(status)
                approval_id = req.request_id
        if self.cfg.stop_after == "approval":
            raise TaskCancelled("stop_after=approval (test hook)")
        with self._step("execute_in_terminal") as rec:
            # Fresh lookup right before acting: never act on a ref older than this step.
            term = self.b.locate(self.task, self.window_id, terminal_locator(self.terminal_n), "workflow.vscode_probe")
            res = self._submit(Action("terminal_command", window_id=self.window_id,
                                      params={"ref": term["ref_id"], "command": cmd,
                                              "input_method_note": "Headed keyboard input (SendInput) into the VS Code integrated terminal, then Enter"}),
                               approval_id=approval_id, grant_id=self.cfg.grant_id)
            self.input_method = res.input_method
            self.metrics["execution_ms"] = round(res.duration_ms, 1)
            rec.detail.update(input_method=res.input_method, approval=res.approval_status, sub_actions=res.sub_actions)

    def verify(self) -> None:
        if self.task.state != TaskState.PAUSED:
            self.task.transition(TaskState.VERIFYING, "independent verification")
        with self._step("verify_output") as rec:
            v = verify_probe_file(self.probe_path, self.run_id, self.cfg.probe_dir, self.task.created_at,
                                  self.pre["absent_before_run"])
            self.verification = v
            self.metrics["verification_ms"] = round(v.duration_ms, 1)
            rec.detail.update(v.to_dict())
            self.b.audit.record(agent_or_component="verification", tool_name="filesystem",
                                action_type="verify_probe_file", task_id=self.task.task_id,
                                target_application="Code.exe", status="succeeded" if v.ok else "failed",
                                verification_status="verified" if v.ok else "failed",
                                execution_duration_ms=round(v.duration_ms, 1),
                                result_summary={"checks": v.checks, "run_id": self.run_id, "path": str(self.probe_path)})
            # Separate technical finding: is the terminal output readable through UIA?
            try:
                term = self.b.locate(self.task, self.window_id, terminal_locator(self.terminal_n), "workflow.vscode_probe")
                txt = self._submit(Action("get", window_id=self.window_id,
                                          params={"ref": term["ref_id"], "property": "text"})).data.get("value") or ""
                self.findings["terminal_text_via_uia_contains_run_id"] = self.run_id in txt
            except (BrokerError, AdapterError) as e:
                self.findings["terminal_text_via_uia_contains_run_id"] = f"unavailable: {e}"[:120]

    def cleanup(self) -> None:
        if not (self.cfg.kill_terminal_after and self.window_id and self.terminal_n):
            return
        try:
            with self._step("cleanup_kill_terminal"):
                self._act("kill_terminal", KILL_TERMINAL)
        except Exception:
            pass

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
            if self.cfg.stop_after == "detect":
                raise _StopHere()
            self.inspect()
            self.clean_terminals()
            self.open_terminal()
            self.approve_and_execute()
            self.verify()
            t.gate()
            if self.verification.ok:
                t.transition(TaskState.COMPLETED, "verified")
            else:
                t.transition(TaskState.FAILED, "verification failed")
                error = {"code": "VERIFICATION_FAILED", "category": "verification", "message": str(self.verification.checks)}
            self.cleanup()
        except _StopHere:
            t.transition(TaskState.COMPLETED, f"stop_after={self.cfg.stop_after}")
        except _ApprovalNotGiven as e:
            error = {"code": f"APPROVAL_{e.status.upper()}", "category": "approval", "message": "command not executed"}
            self._finish_cancel(f"approval {e.status}")
            self.cleanup()
        except (TaskCancelled, EmergencyStopActive) as e:
            code, cat = error_category(e)
            error = {"code": code, "category": cat, "message": str(e)}
            self._finish_cancel(str(e))
        except Exception as e:
            code, cat = error_category(e)
            error = {"code": code, "category": cat, "message": str(e)[:400],
                     "details": getattr(e, "details", None)}
            if not t.is_terminal:
                if t.state == TaskState.PAUSED:
                    t.transition(TaskState.FAILED, code)
                else:
                    t.transition(TaskState.FAILED, code)
            if "terminal_command" in t.dispatched_steps and self.verification is None:
                # The command may have landed: verify, but never re-execute.
                try:
                    self.verification = verify_probe_file(self.probe_path, self.run_id, self.cfg.probe_dir,
                                                          t.created_at, self.pre["absent_before_run"], wait_s=5)
                except Exception:
                    pass
            if not isinstance(e, EmergencyStopActive):
                self.cleanup()
        finally:
            t.current_step = t.current_action = None
        self.metrics["end_to_end_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        self.metrics["end_to_end_excl_approval_ms"] = round(
            self.metrics["end_to_end_ms"] - self.metrics.get("approval_wait_ms", 0.0), 1)
        t.error = error
        t.result = self.report()
        self.b.audit.record(agent_or_component="workflow.vscode_probe", tool_name="workflow", action_type="task_finished",
                            task_id=t.task_id, status=t.state.value,
                            target_application="Code.exe",
                            execution_duration_ms=self.metrics["end_to_end_ms"], retry_count=self.retries,
                            error_code=(error or {}).get("code"), error_category=(error or {}).get("category"),
                            verification_status=("verified" if self.verification and self.verification.ok
                                                 else "failed" if self.verification else "not_run"),
                            approval_status=self.approval_status,
                            result_summary={"run_id": self.run_id, "input_method": self.input_method,
                                            "metrics": self.metrics, "stop_reason": t.stop_reason})
        return t.result

    def _finish_cancel(self, reason: str) -> None:
        t = self.task
        if t.is_terminal:
            return
        t.transition(TaskState.CANCELLED, reason)

    def report(self) -> dict:
        return {
            "task_id": self.task.task_id, "run_id": self.run_id, "status": self.task.state.value,
            "stop_reason": self.task.stop_reason, "window_id": self.window_id,
            "terminal": f"Terminal {self.terminal_n}" if self.terminal_n else None,
            "approval_status": self.approval_status, "input_method": self.input_method,
            "probe_file": str(getattr(self, "probe_path", "")) or None,
            "verification": self.verification.to_dict() if self.verification else None,
            "findings": self.findings, "metrics": self.metrics, "retries": self.retries,
            "steps": [s.__dict__ for s in self.steps], "error": self.task.error,
            "audit_log": str(self.b.audit.path),
        }


class _StopHere(Exception):
    pass


class _ApprovalNotGiven(Exception):
    def __init__(self, status: str):
        super().__init__(status)
        self.status = status
