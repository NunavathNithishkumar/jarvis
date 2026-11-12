"""The desktop broker: the only path from a planner to the Windows desktop.

submit(action) -> e-stop check -> pause/cancel gate -> resolve real window/element
context -> policy -> approval -> execute through the adapter -> audit.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .adapter import AdapterError, DesktopAdapter, iter_nodes
from .approvals import ApprovalManager, ApprovalRequest
from .audit import AuditLogger, sha256
from .credentials import CredentialGuard
from .emergency_stop import EmergencyStop, EmergencyStopActive, release_held_inputs
from .policy import Action, Category, Decision, Policy
from .state import Task, TaskCancelled, TaskState

# agent-desktop errors that mean "re-identify the element and try again" when retry is safe.
RELOCATABLE = {"STALE_REF", "SNAPSHOT_NOT_FOUND", "TIMEOUT"}
MAX_RELOCATE_RETRIES = 2


class BrokerError(Exception):
    def __init__(self, code: str, message: str, category: str = "broker", details: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.category, self.details = code, message, category, details or {}


def error_category(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, EmergencyStopActive):
        return "EMERGENCY_STOP", "interruption"
    if isinstance(exc, TaskCancelled):
        return "CANCELLED", "interruption"
    if isinstance(exc, BrokerError):
        return exc.code, exc.category
    if isinstance(exc, AdapterError):
        cat = {"STALE_REF": "stale_reference", "AMBIGUOUS_TARGET": "ambiguous_target",
               "TIMEOUT": "timeout", "KILLED": "interruption", "WINDOW_NOT_FOUND": "missing_target",
               "ELEMENT_NOT_FOUND": "missing_target", "PERM_DENIED": "permission",
               "POLICY_DENIED": "permission"}.get(exc.code, "adapter")
        return exc.code, cat
    return type(exc).__name__, "internal"


@dataclass
class Locator:
    """Semantic description of a target element, used to re-identify it after a stale ref."""
    role: str
    name: str | None = None          # passed to agent-desktop --name
    exact: bool = False
    text: str | None = None          # passed to agent-desktop --text
    name_regex: str | None = None    # extra client-side filter
    require_states: tuple[str, ...] = ()

    def describe(self) -> str:
        return f"{self.role}:{self.name or self.text or self.name_regex}"


@dataclass
class ActionResult:
    action_id: str
    action_type: str
    category: str
    data: object
    duration_ms: float
    retries: int = 0
    input_method: str | None = None
    approval_status: str = "not_required"
    sub_actions: list = field(default_factory=list)


class Broker:
    def __init__(self, adapter: DesktopAdapter, policy: Policy, audit: AuditLogger, estop: EmergencyStop,
                 approvals: ApprovalManager | None = None, guard: CredentialGuard | None = None,
                 probe_dir: Path | None = None, launch_target: str | None = None, launch_args: list[str] | None = None,
                 launch_cli: str | None = None, workspace_dir: Path | None = None, system=None):
        self.adapter, self.policy, self.audit, self.estop = adapter, policy, audit, estop
        if system is None:
            from .system_actions import SystemActions
            system = SystemActions()
        self.system = system          # Phase 3: browser/media executor (injectable for tests)
        self.approvals = approvals or ApprovalManager()
        self.guard = guard or CredentialGuard()
        self.probe_dir = Path(probe_dir).resolve() if probe_dir else None
        self.workspace_dir = Path(workspace_dir).resolve() if workspace_dir else None
        self.path_roots = {k: v for k, v in (("probe_output", self.probe_dir),
                                             ("probe_workspace", self.workspace_dir)) if v}
        self.launch_target, self.launch_args, self.launch_cli = launch_target, launch_args or [], launch_cli
        self.windows: dict[str, dict] = {}
        self.refs: dict[str, dict] = {}
        self.active_task: Task | None = None
        self.current_action: dict | None = None
        self.estop.on_trip(self._on_estop)

    # ------------------------------------------------------------------ e-stop
    def _on_estop(self, source: str) -> None:
        killed = self.adapter.kill_active()
        released = release_held_inputs()
        n_refs = len(self.refs)
        self.refs.clear()
        self.approvals.cancel_all("emergency stop")
        if self.active_task and not self.active_task.is_terminal:
            self.active_task.request_cancel(f"emergency stop ({source})")
        self.audit.record(agent_or_component="emergency_stop", action_type="emergency_stop", status="tripped",
                          task_id=self.active_task.task_id if self.active_task else None,
                          result_summary={"source": source, "killed_active_process": killed,
                                          "released_inputs": released, "invalidated_refs": n_refs,
                                          "epoch": self.estop.epoch,
                                          "note": "does not reverse actions already completed"})

    def rearm(self, actor: str) -> tuple[bool, str]:
        ok, msg = self.estop.rearm()
        self.audit.record(agent_or_component="emergency_stop", action_type="rearm", status="ok" if ok else "refused",
                          result_summary={"actor": actor, "message": msg})
        return ok, msg

    # ------------------------------------------------------------- context
    def _window(self, window_id: str) -> dict:
        if window_id not in self.windows:
            self._refresh_windows()
        w = self.windows.get(window_id)
        if not w:
            raise BrokerError("WINDOW_NOT_FOUND", f"window {window_id} is not open", "missing_target")
        return w

    def _refresh_windows(self, app: str | None = None) -> list[dict]:
        wins = self.adapter.list_windows(app).data or []
        if app is None:
            self.windows = {w["id"]: w for w in wins}
        else:
            for wid in [k for k, v in self.windows.items() if v.get("app_name", "").lower() == app.lower()]:
                del self.windows[wid]
            self.windows.update({w["id"]: w for w in wins})
        return wins

    def _resolve_context(self, action: Action) -> None:
        ref = action.params.get("ref")
        if ref:
            meta = self.refs.get(ref)
            if not meta or meta["epoch"] != self.estop.epoch:
                raise BrokerError("STALE_REF_LOCAL", f"ref {ref} was not issued in the current broker epoch",
                                  "stale_reference")
            if action.window_id and meta["window_id"] != action.window_id:
                raise BrokerError("REF_WINDOW_MISMATCH", "ref belongs to a different window", "validation")
            action.window_id = meta["window_id"]
            action.element = meta["element"]
        if action.window_id:
            w = self._window(action.window_id)
            if action.app and action.app.lower() != w.get("app_name", "").lower():
                raise BrokerError("APP_MISMATCH", f"window {action.window_id} belongs to {w.get('app_name')}",
                                  "validation")
            action.app = w.get("app_name")
            action.window_title = w.get("title")
        action.process_name = action.app

    def _register_refs(self, window_id: str, nodes) -> None:
        for n in nodes:
            ref = n.get("ref_id")
            if ref:
                self.refs[ref] = {"window_id": window_id, "epoch": self.estop.epoch,
                                  "element": {"role": n.get("role"), "name": n.get("name"),
                                              "states": n.get("states") or []}}

    def _scrub(self, nodes) -> int:
        """Strip values of sensitive elements from observation output so they never reach a caller or log."""
        hidden = 0
        for n in nodes:
            if self.policy.is_sensitive_element({"role": n.get("role"), "name": n.get("name"),
                                                 "states": n.get("states")}):
                if n.get("value"):
                    n["value"] = "[SENSITIVE-HIDDEN]"
                    hidden += 1
                n["sensitive"] = True
        return hidden

    @staticmethod
    def fingerprint(action: Action) -> str:
        params = {k: v for k, v in action.params.items() if k != "ref"}
        return sha256(json.dumps([action.action_type, action.window_id, params], sort_keys=True))

    # -------------------------------------------------------------- submit
    def submit(self, action: Action, *, task: Task | None = None, approval_id: str | None = None,
               grant_id: str | None = None, component: str = "broker", retry_count: int = 0) -> ActionResult:
        action_id = "act-" + uuid.uuid4().hex[:12]
        start = time.perf_counter()
        decision: Decision | None = None
        approval_status = "not_required"
        base = dict(action_id=action_id, task_id=task.task_id if task else None, agent_or_component=component,
                    tool_name="agent-desktop", action_type=action.action_type, retry_count=retry_count)
        try:
            self.estop.check()
            if task:
                task.gate()
                task.current_action = action.action_type
            self.current_action = {"action_id": action_id, "action_type": action.action_type,
                                   "window_id": action.window_id, "status": "running", "started_at": time.time()}
            self._resolve_context(action)
            if self.guard.is_blocked(action.app) and action.action_type not in ("list_windows", "list_apps", "status"):
                raise BrokerError("CREDENTIAL_HANDOFF_ACTIVE",
                                  f"{action.app} is under human credential control; automation suspended", "policy")
            decision = self.policy.classify(action)
            if decision.category == Category.DENIED:
                raise BrokerError("POLICY_DENIED", decision.reason, "policy")
            if decision.category == Category.APPROVAL_REQUIRED:
                approval_status = self._check_approval(action, decision, approval_id, grant_id)
            self.estop.check()
            if task:
                task.gate()
            handler: Callable = getattr(self, f"_do_{action.action_type}")
            data, input_method, subs = handler(action, task, action_id)
            dur = (time.perf_counter() - start) * 1000
            self.audit.record(**base, status="succeeded", target_application=action.app,
                              target_window=action.window_title, execution_duration_ms=round(dur, 1),
                              approval_status=approval_status,
                              result_summary=self._summary(action, data, decision, input_method))
            return ActionResult(action_id, action.action_type, decision.category.value, data, dur,
                                retry_count, input_method, approval_status, subs)
        except Exception as exc:
            code, cat = error_category(exc)
            status = "rejected" if code in ("POLICY_DENIED", "APPROVAL_MISSING", "EMERGENCY_STOP",
                                            "CREDENTIAL_HANDOFF_ACTIVE", "STALE_REF_LOCAL") else "failed"
            self.audit.record(**base, status=status, target_application=action.app, target_window=action.window_title,
                              execution_duration_ms=round((time.perf_counter() - start) * 1000, 1),
                              error_code=code, error_category=cat, approval_status=approval_status,
                              result_summary={"message": str(exc)[:300],
                                              "policy": decision.reason if decision else None,
                                              "delivery": getattr(exc, "delivery", None),
                                              "retry": getattr(exc, "retry", None)})
            raise
        finally:
            if self.current_action and self.current_action.get("action_id") == action_id:
                self.current_action = None

    def workspace_file_path(self, rel_path: str) -> Path:
        """Resolve a policy-validated task-file path and confine it to the workspace root."""
        root_name = self.policy.file_write.get("root")
        root = self.path_roots.get(root_name)
        if root is None:
            raise BrokerError("POLICY_DENIED", f"workspace root '{root_name}' is not configured", "policy")
        path = (root / rel_path).resolve()
        if root not in path.parents or path.parent.parent != root:
            raise BrokerError("POLICY_DENIED", "file must be directly inside a task directory of the workspace",
                              "policy")
        for p in (path.parent, path):
            if p.exists() and p.is_symlink():
                raise BrokerError("POLICY_DENIED", "symlinks are not allowed", "policy")
        return path

    def _check_approval(self, action: Action, decision: Decision, approval_id, grant_id) -> str:
        if action.action_type == "write_workspace_file":
            self.workspace_file_path(action.params["rel_path"])
        if action.action_type == "terminal_command":
            root_name = self.policy.cfg.get("command_path_roots", {}).get(decision.template)
            root = self.path_roots.get(root_name)
            for key in ("path", "out"):
                value = (decision.template_match or {}).get(key)
                if value is None:
                    continue
                if root is None or root not in Path(value).resolve().parents:
                    raise BrokerError("POLICY_DENIED", f"{key} must be inside the approved {root_name} directory",
                                      "policy")
        fp = self.fingerprint(action)
        if approval_id and self.approvals.consume(approval_id, fp):
            return "approved"
        if grant_id and action.action_type == "terminal_command" and \
                self.approvals.consume_batch(grant_id, action.params["command"], action.window_title or ""):
            return "approved_batch"
        raise BrokerError("APPROVAL_MISSING", "no valid, unused approval matches this exact action", "policy")

    def _summary(self, action: Action, data, decision: Decision | None, input_method) -> dict:
        s = {"category": decision.category.value if decision else None, "input_method": input_method}
        if action.action_type == "terminal_command":
            cmd = action.params["command"]
            s.update(command_template=decision.template, command_sha256=sha256(cmd),
                     command_display=cmd if decision.template else None)
        elif action.action_type in ("open_url", "play_media") and isinstance(data, dict):
            s.update({k: data.get(k) for k in ("host", "video_id", "title") if data.get(k)})
        elif action.action_type == "write_workspace_file" and isinstance(data, dict):
            s.update(path=data.get("path"), file_sha256=data.get("sha256"), file_bytes=data.get("bytes"))
        elif action.action_type in ("snapshot", "find") and isinstance(data, dict):
            s.update(ref_count=data.get("ref_count") or data.get("total_matches"),
                     sensitive_hidden=data.get("_sensitive_hidden", 0))
        elif action.action_type == "list_windows" and isinstance(data, list):
            s.update(windows=[{"id": w["id"], "title": w.get("title"), "focused": w.get("is_focused")} for w in data])
        elif isinstance(data, dict) and "disposition" in data:
            s.update(disposition=data.get("disposition"))
        return s

    # ---------------------------------------------------------- approvals
    def request_approval(self, action: Action, *, task: Task | None, summary: str, effects: str,
                         timeout_s: float | None = None) -> ApprovalRequest:
        self.estop.check()
        self._resolve_context(action)
        decision = self.policy.classify(action)
        if decision.category != Category.APPROVAL_REQUIRED:
            raise BrokerError("POLICY_DENIED", f"cannot request approval: {decision.reason}", "policy")
        details = {"action_type": action.action_type, "application": action.app,
                   "window_title": action.window_title, "window_id": action.window_id,
                   "command": action.params.get("command"), "template": decision.template,
                   "effects": effects, "input_method": action.params.get("input_method_note")}
        if action.action_type == "write_workspace_file":
            path = self.workspace_file_path(action.params["rel_path"])
            if path.exists():
                raise BrokerError("POLICY_DENIED", f"{path.name} already exists; overwriting is not allowed", "policy")
            content = action.params["content"]
            details.update(application="filesystem (probe workspace)", file_path=str(path),
                           file_content=content, file_sha256=sha256(content),
                           file_bytes=len(content.encode("utf-8")))
        req = self.approvals.request("action", summary, details, self.fingerprint(action),
                                     timeout_s or self.policy.approval_cfg.get("timeout_s", 120))
        self.audit.record(agent_or_component="approvals", action_type="approval_requested", status="pending",
                          task_id=task.task_id if task else None, target_application=action.app,
                          target_window=action.window_title, approval_status="pending",
                          result_summary={"request_id": req.request_id, "summary": summary,
                                          "command_sha256": sha256(action.params.get("command", "")),
                                          "file_sha256": details.get("file_sha256")})
        return req

    def wait_for_approval(self, req: ApprovalRequest, task: Task | None) -> str:
        start = time.perf_counter()
        if task and task.state == TaskState.RUNNING:
            task.transition(TaskState.WAITING_FOR_APPROVAL, req.summary)
        status = self.approvals.wait(req, lambda: self.estop.tripped or bool(task and task.cancel_requested))
        if status == "timeout":
            status_out = "denied_timeout"
        else:
            status_out = status
        if task and task.state == TaskState.PAUSED:
            task.gate()  # approval may resolve while paused; do not continue until resumed
        if task and task.state == TaskState.WAITING_FOR_APPROVAL and status == "approved":
            task.transition(TaskState.RUNNING, "approved")
        self.audit.record(agent_or_component="approvals", action_type="approval_resolved", status=status_out,
                          task_id=task.task_id if task else None, approval_status=status_out,
                          execution_duration_ms=round((time.perf_counter() - start) * 1000, 1),
                          result_summary={"request_id": req.request_id, "actor": req.actor,
                                          "approval_wait_ms": round((time.perf_counter() - start) * 1000, 1)})
        return status

    # ------------------------------------------------- semantic element access
    def locate(self, task: Task | None, window_id: str, loc: Locator, component: str = "broker") -> dict:
        """Fresh lookup; returns the single match or raises (never guesses between candidates)."""
        params = {"role": loc.role, "name": loc.name, "text": loc.text, "exact": loc.exact}
        res = self.submit(Action("find", window_id=window_id, params=params), task=task, component=component)
        matches = res.data.get("matches", [])
        if loc.name_regex:
            rx = re.compile(loc.name_regex)
            matches = [m for m in matches if rx.search(m.get("name") or "")]
        if loc.require_states:
            matches = [m for m in matches if set(loc.require_states) <= set(m.get("states") or [])]
        matches = [m for m in matches if m.get("ref_id")]
        if not matches:
            raise BrokerError("ELEMENT_NOT_FOUND", f"no element matches {loc.describe()}", "missing_target")
        if len(matches) > 1:
            raise BrokerError("AMBIGUOUS_ELEMENT", f"{len(matches)} elements match {loc.describe()}",
                              "ambiguous_target", {"candidates": [m.get("name") for m in matches][:10]})
        return matches[0]

    def act(self, task: Task | None, window_id: str, action_type: str, loc: Locator, *,
            ref_hint: str | None = None, extra: dict | None = None, component: str = "broker",
            **submit_kw) -> ActionResult:
        """Act on an element; on a stale ref take a fresh lookup, re-validate and retry (bounded)."""
        ref = ref_hint
        last: Exception | None = None
        for attempt in range(MAX_RELOCATE_RETRIES + 1):
            if ref is None:
                ref = self.locate(task, window_id, loc, component)["ref_id"]
            try:
                return self.submit(Action(action_type, window_id=window_id, params={"ref": ref, **(extra or {})}),
                                   task=task, component=component, retry_count=attempt, **submit_kw)
            except AdapterError as e:
                if e.code in RELOCATABLE and e.retry_safe and attempt < MAX_RELOCATE_RETRIES:
                    last = e
                    self.audit.record(agent_or_component=component, action_type="relocate", status="retrying",
                                      task_id=task.task_id if task else None, retry_count=attempt + 1,
                                      error_code=e.code, error_category="stale_reference",
                                      result_summary={"stale_ref": ref, "locator": loc.describe()})
                    self.refs.pop(ref, None)
                    ref = None
                    continue
                raise
            except BrokerError as e:
                if e.code == "STALE_REF_LOCAL" and attempt < MAX_RELOCATE_RETRIES:
                    last, ref = e, None
                    continue
                raise
        raise last  # pragma: no cover

    def foreground_window_id(self) -> str | None:
        wins = self._refresh_windows()
        return next((w["id"] for w in wins if w.get("is_focused")), None)

    # ------------------------------------------------------------ handlers
    # Each returns (data, input_method, sub_actions).
    def _do_status(self, a, t, aid):
        return self.adapter.status().data, None, []

    def _do_list_apps(self, a, t, aid):
        return self.adapter.list_apps(a.params.get("app")).data, None, []

    def _do_list_windows(self, a, t, aid):
        return self._refresh_windows(a.params.get("app")), None, []

    def _do_snapshot(self, a, t, aid):
        p = a.params
        data = self.adapter.snapshot(a.app, a.window_id, skeleton=p.get("skeleton", False),
                                     interactive=p.get("interactive", True), max_depth=p.get("max_depth"),
                                     timeout_ms=p.get("timeout_ms", 20000), root=p.get("root")).data
        nodes = list(iter_nodes(data.get("tree") or {}))
        data["_sensitive_hidden"] = self._scrub(nodes)
        self._register_refs(a.window_id, nodes)
        return data, None, []

    def _do_find(self, a, t, aid):
        p = a.params
        data = self.adapter.find(a.app, a.window_id, role=p.get("role"), name=p.get("name"),
                                 text=p.get("text"), exact=p.get("exact", False)).data
        matches = data.get("matches", [])
        data["_sensitive_hidden"] = self._scrub(matches)
        self._register_refs(a.window_id, matches)
        return data, None, []

    def _do_get(self, a, t, aid):
        return self.adapter.get(a.params["ref"], a.params.get("property", "text")).data, None, []

    def _do_screenshot(self, a, t, aid):
        return self.adapter.screenshot(a.app, a.window_id, a.params["path"]).data, None, []

    def _do_click(self, a, t, aid):
        return self.adapter.click(a.params["ref"]).data, "semantic_uia", []

    def _do_clear(self, a, t, aid):
        return self.adapter.clear(a.params["ref"]).data, "semantic_uia", []

    def _do_focus_window(self, a, t, aid):
        return self.adapter.focus_window(a.window_id).data, "window_activation", []

    def _do_restore_window(self, a, t, aid):
        return self.adapter.restore_window(a.app, a.window_id).data, "window_activation", []

    def _do_kill_terminal(self, a, t, aid):
        return self.adapter.click(a.params["ref"]).data, "semantic_uia", []

    def _do_launch_app(self, a, t, aid):
        if not self.launch_target:
            raise BrokerError("LAUNCH_NOT_CONFIGURED", "no launch target configured", "validation")
        try:
            return self.adapter.launch(self.launch_target, self.launch_args).data, "agent_desktop_launch", []
        except AdapterError as e:
            # agent-desktop 1.2.0 refuses to launch a multi-process app that is already running
            # (AMBIGUOUS_TARGET over Code.exe's helper processes). Fall back to the app's own CLI.
            if e.code != "AMBIGUOUS_TARGET" or not self.launch_cli:
                raise
            subprocess.Popen([self.launch_cli, *self.launch_args], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return {"fallback": "app_cli", "reason": e.code}, "app_cli_launch", []

    # ---------------------------------------------------------- Phase 3: system actions
    def _system_call(self, fn, *args):
        from .system_actions import SystemActionError
        try:
            return fn(*args)
        except SystemActionError as e:
            raise BrokerError(e.code, e.message, "system")

    def _do_open_url(self, a, t, aid):
        from urllib.parse import urlsplit
        url = a.params["url"]
        self._system_call(self.system.open_url, url)
        return {"url": url, "host": urlsplit(url).hostname}, "default_browser", []

    def _do_play_media(self, a, t, aid):
        hit = self._system_call(self.system.youtube_search, a.params["query"])
        problem = self.policy.check_url(hit.url)      # the resolved URL is re-checked by policy
        if problem:
            raise BrokerError("POLICY_DENIED", problem, "policy")
        self.estop.check()
        if t:
            t.gate()
        self._system_call(self.system.open_url, hit.url)
        return {"video_id": hit.video_id, "title": hit.title, "url": hit.url, "host": "www.youtube.com"}, \
            "default_browser", []

    def _do_write_workspace_file(self, a, t, aid):
        """Write an approved file (Phase 2). Exclusive create: never overwrites."""
        path = self.workspace_file_path(a.params["rel_path"])
        content = a.params["content"]
        if t:
            t.dispatched_steps.add("write_workspace_file")
        path.parent.mkdir(exist_ok=True)
        try:
            with open(path, "x", encoding="utf-8", newline="\n") as f:
                f.write(content)
        except FileExistsError:
            raise BrokerError("POLICY_DENIED", f"{path.name} already exists; overwriting is not allowed", "policy")
        data = {"path": str(path), "sha256": sha256(content), "bytes": len(content.encode("utf-8"))}
        return data, "broker_filesystem", []

    def _sub(self, t: Task | None, parent: str, name: str, fn, subs: list):
        """A gated, audited sub-step of a composite action."""
        self.estop.check()
        if t:
            t.gate()
            t.current_action = f"terminal_command/{name}"
        start = time.perf_counter()
        try:
            res = fn()
        except Exception as exc:
            code, cat = error_category(exc)
            self.audit.record(agent_or_component="broker", tool_name="agent-desktop",
                              action_type=f"terminal_command/{name}", status="failed", error_code=code,
                              error_category=cat, task_id=t.task_id if t else None,
                              execution_duration_ms=round((time.perf_counter() - start) * 1000, 1),
                              result_summary={"parent_action_id": parent, "message": str(exc)[:200]})
            raise
        dur = round((time.perf_counter() - start) * 1000, 1)
        data = getattr(res, "data", res)
        disp = data.get("disposition") if isinstance(data, dict) else None
        subs.append({"step": name, "duration_ms": dur, "disposition": disp})
        self.audit.record(agent_or_component="broker", tool_name="agent-desktop", action_type=f"terminal_command/{name}",
                          status="succeeded", task_id=t.task_id if t else None, execution_duration_ms=dur,
                          result_summary={"parent_action_id": parent, "disposition": disp,
                                          "headed": getattr(res, "headed", None)})
        return res

    def _discard_terminal(self, wid: str) -> bool:
        """Kill the active terminal semantically (no foreground needed) so a typed but
        unexecuted command cannot be run later by a stray Enter. Clearing xterm's hidden
        textarea does not touch the shell's input line, so it is not enough."""
        try:
            hits = self.adapter.find(self.windows[wid]["app_name"], wid, role="button", name="Kill Terminal",
                                     exact=True).data.get("matches", [])
            if len(hits) != 1:
                return False
            self.adapter.click(hits[0]["ref_id"])
            self.audit.record(agent_or_component="broker", action_type="terminal_command/discard_pending_line",
                              status="succeeded", result_summary={"method": "kill_terminal (semantic)"})
            return True
        except Exception:
            return False

    def _do_terminal_command(self, a: Action, t: Task | None, aid: str):
        """Type an approved command into the VS Code integrated terminal and press Enter.

        Headed input is required: agent-desktop's semantic set-value lands in xterm's hidden
        textarea without reaching the shell (observed on VS Code, Phase 1 discovery). Headed
        keystrokes go to the foreground window, so foreground is re-verified before each burst.
        """
        ref, wid, cmd = a.params["ref"], a.window_id, a.params["command"]
        subs: list = []
        self._sub(t, aid, "clear_input", lambda: self.adapter.clear(ref), subs)
        self._sub(t, aid, "focus_window", lambda: self.adapter.focus_window(wid), subs)
        self._sub(t, aid, "focus_terminal", lambda: self.adapter.focus(ref, headed=True), subs)
        fg = self._sub(t, aid, "verify_foreground_before_type", self.foreground_window_id, subs)
        if fg != wid:
            raise BrokerError("FOREGROUND_MISMATCH", f"foreground is {fg}, not {wid}; nothing typed", "focus")
        # Escape reverts any pending shell input (PSReadLine), so leftovers can't prefix the approved command.
        self._sub(t, aid, "clear_shell_line", lambda: self.adapter.press("escape", window_id=wid, headed=True), subs)
        if t:
            t.dispatched_steps.add("terminal_command")  # from here the action may have landed: never replay
        self._sub(t, aid, "type_command", lambda: self.adapter.type_text(ref, cmd, headed=True), subs)
        fg = self._sub(t, aid, "verify_foreground_before_enter", self.foreground_window_id, subs)
        if fg != wid:
            discarded = self._discard_terminal(wid)
            raise BrokerError("FOREGROUND_LOST", f"foreground changed to {fg} after typing; Enter not sent; "
                              f"unexecuted line discarded by killing the terminal: {discarded}", "focus",
                              {"command_executed": False, "terminal_killed": discarded})
        self._sub(t, aid, "press_enter", lambda: self.adapter.press("return", window_id=wid, headed=True), subs)
        return {"sub_actions": subs}, "headed_sendinput", subs
