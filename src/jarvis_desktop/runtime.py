"""Wires the broker components together and runs workflows as background jobs."""
from __future__ import annotations

import json
import math
import os
import statistics
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .adapter import PROJECT_ROOT, DesktopAdapter
from .approvals import ApprovalManager
from .audit import AuditLogger
from .broker import Broker
from .credentials import CredentialGuard
from .emergency_stop import EmergencyStop, HotkeyListener, SentinelWatcher
from .policy import Policy
from .state import Task
from .workflows.vscode_probe import ProbeConfig, ProbeWorkflow
from .workflows.vscode_task import BuildAndRunTask

ARTIFACTS = PROJECT_ROOT / "artifacts"
PROBE_DIR = ARTIFACTS / "probe-output"
PROBE_WORKSPACE = ARTIFACTS / "probe-workspace"
CODE_EXE = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Microsoft VS Code" / "Code.exe"
CODE_CLI = CODE_EXE.parent / "bin" / "code.cmd"


@dataclass
class Runtime:
    broker: Broker
    policy: Policy
    estop: EmergencyStop
    approvals: ApprovalManager
    audit: AuditLogger
    guard: CredentialGuard
    hotkey: HotkeyListener | None = None
    sentinel: SentinelWatcher | None = None
    jobs: "JobManager" = None  # type: ignore

    def close(self):
        if self.hotkey:
            self.hotkey.stop()
        if self.sentinel:
            self.sentinel.stop()


def build_runtime(*, artifacts: Path = ARTIFACTS, hotkey: bool = True, sentinel: bool = True,
                  adapter: DesktopAdapter | None = None, restrict_acl: bool = True) -> Runtime:
    policy = Policy.load()
    audit = AuditLogger(artifacts / "audit_logs", restrict_acl=restrict_acl)
    estop = EmergencyStop(artifacts / "EMERGENCY_STOP")
    approvals = ApprovalManager()
    guard = CredentialGuard()
    broker = Broker(adapter or DesktopAdapter(), policy, audit, estop, approvals, guard,
                    probe_dir=artifacts / "probe-output", launch_target=str(CODE_EXE),
                    launch_args=["-n", str(PROBE_WORKSPACE)], launch_cli=str(CODE_CLI),
                    workspace_dir=PROBE_WORKSPACE)
    rt = Runtime(broker, policy, estop, approvals, audit, guard)
    if sentinel:
        rt.sentinel = SentinelWatcher(estop)
        rt.sentinel.start()
    if hotkey and os.name == "nt":
        rt.hotkey = HotkeyListener(estop)
        rt.hotkey.start()
        rt.hotkey.wait_ready()
    rt.jobs = JobManager(rt)
    audit.record(agent_or_component="runtime", action_type="startup", status="ok",
                 result_summary={"hotkey_registered": bool(rt.hotkey and rt.hotkey.registered.is_set()),
                                 "hotkey_error": rt.hotkey.error if rt.hotkey else None,
                                 "audit_acl_restricted": audit.acl_restricted})
    return rt


def probe_config(**kw) -> ProbeConfig:
    pol = Policy.load()
    return ProbeConfig(probe_dir=PROBE_DIR,
                       window_title_regex=pol.cfg["scopes"]["vscode_probe"]["window_title_regex"], **kw)


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, math.ceil(p / 100 * len(s)) - 1))  # nearest-rank
    return round(s[k], 1)


def summarize(runs: list[dict]) -> dict:
    ok = [r for r in runs if r["status"] == "COMPLETED"]
    keys = sorted({k for r in runs for k in (r.get("metrics") or {})})
    lat = {}
    for k in keys:
        vals = [r["metrics"][k] for r in ok if k in (r.get("metrics") or {})]
        if vals:
            lat[k] = {"n": len(vals), "median": round(statistics.median(vals), 1), "p95": pct(vals, 95),
                      "min": round(min(vals), 1), "max": round(max(vals), 1)}
    fails: dict[str, int] = {}
    for r in runs:
        if r["status"] != "COMPLETED":
            key = (r.get("error") or {}).get("category", "unknown") + ":" + (r.get("error") or {}).get("code", "?")
            fails[key] = fails.get(key, 0) + 1
    return {"runs": len(runs), "succeeded": len(ok), "success_rate": round(len(ok) / len(runs), 4) if runs else None,
            "total_retries": sum(r.get("retries", 0) for r in runs),
            "runs_with_retries": sum(1 for r in runs if r.get("retries")), "latency_ms": lat,
            "failures_by_category": fails}


@dataclass
class Job:
    kind: str
    job_id: str
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    status: str = "running"
    runs: list = field(default_factory=list)
    summary: dict | None = None
    grant_id: str | None = None
    message: str | None = None
    current: ProbeWorkflow | None = None

    def public(self) -> dict:
        return {"kind": self.kind, "job_id": self.job_id, "status": self.status, "started_at": self.started_at,
                "finished_at": self.finished_at, "runs": self.runs, "summary": self.summary,
                "grant_id": self.grant_id, "message": self.message}


class JobManager:
    def __init__(self, rt: Runtime):
        self.rt = rt
        self.job: Job | None = None
        self._lock = threading.Lock()
        self.history: list[dict] = []

    @property
    def busy(self) -> bool:
        return bool(self.job and self.job.status in ("running", "waiting_for_batch_approval"))

    @property
    def current_task(self) -> Task | None:
        return self.job.current.task if self.job and self.job.current else None

    def start_demo(self, **cfg) -> Job:
        return self._start("demo", lambda job: self._demo(job, cfg))

    def start_task(self, **cfg) -> Job:
        def target(job):
            wf = BuildAndRunTask(self.rt.broker, probe_config(kill_terminal_after=False, **cfg), PROBE_WORKSPACE)
            job.current = wf
            job.runs.append(wf.run())
            job.summary = summarize(job.runs)
        return self._start("complex_task", target)

    def start_reliability(self, runs: int = 20, **cfg) -> Job:
        return self._start("reliability", lambda job: self._reliability(job, runs, cfg))

    def _start(self, kind, target) -> Job:
        with self._lock:
            if self.busy:
                raise RuntimeError("a job is already running")
            self.rt.estop.check()
            job = Job(kind, f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}")
            self.job = job
        threading.Thread(target=self._wrap, args=(job, target), daemon=True, name=job.job_id).start()
        return job

    def _wrap(self, job: Job, target):
        try:
            target(job)
            if job.status == "running":
                job.status = "finished"
        except Exception as e:  # surfaced in UI, never swallowed silently
            job.status, job.message = "error", f"{type(e).__name__}: {e}"
        finally:
            job.finished_at = time.time()
            job.current = None
            self.history.append(job.public())

    def _run_one(self, job: Job, cfg: dict) -> dict:
        wf = ProbeWorkflow(self.rt.broker, probe_config(**cfg))
        job.current = wf
        report = wf.run()
        job.runs.append(report)
        return report

    def _demo(self, job: Job, cfg: dict):
        self._run_one(job, cfg)
        job.summary = summarize(job.runs)

    def _reliability(self, job: Job, n: int, cfg: dict):
        pol = self.rt.policy
        a = pol.approval_cfg
        title_rx = pol.cfg["scopes"]["vscode_probe"]["window_title_regex"]
        cmd_rx = pol.cfg["command_templates"]["probe_write_file"].strip()
        job.status = "waiting_for_batch_approval"
        grant = self.rt.approvals.request_batch(
            f"Batch approval: {n} reliability runs of the VS Code probe command",
            {"action_type": "terminal_command", "application": "Code.exe",
             "window_title_regex": title_rx, "command_template": cmd_rx,
             "example_command": f"Set-Content -LiteralPath '{PROBE_DIR}\\run-<32 hex>.txt' -Value '<same 32 hex>' -NoNewline",
             "max_runs": n, "valid_for_s": a.get("batch_ttl_s", 1800),
             "effects": f"Each run creates one new file in {PROBE_DIR}. No network, no deletion.",
             "input_method": "Headed keyboard input (SendInput) into the VS Code integrated terminal"},
            cmd_rx, title_rx, min(n, a.get("batch_max_runs", 20)), a.get("batch_ttl_s", 1800),
            a.get("batch_timeout_s", 300))
        job.grant_id = grant.grant_id
        self.rt.audit.record(agent_or_component="approvals", action_type="batch_approval_requested", status="pending",
                             approval_status="pending", result_summary={"grant_id": grant.grant_id, "max_runs": n})
        status = self.rt.approvals.wait(grant.request, lambda: self.rt.estop.tripped)
        self.rt.audit.record(agent_or_component="approvals", action_type="batch_approval_resolved", status=status,
                             approval_status=status, result_summary={"grant_id": grant.grant_id,
                                                                     "actor": grant.request.actor})
        if status != "approved":
            job.status, job.message = "not_approved", f"batch approval {status}"
            return
        job.status = "running"
        for i in range(n):
            if self.rt.estop.tripped:
                job.message = "stopped by emergency stop"
                break
            r = self._run_one(job, {**cfg, "grant_id": grant.grant_id})
            r["iteration"] = i + 1
            time.sleep(0.5)
        job.summary = summarize(job.runs)
        out = ARTIFACTS / "test_results" / f"reliability-{time.strftime('%Y%m%d-%H%M%S')}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"summary": job.summary, "runs": job.runs}, indent=2), encoding="utf-8")
        job.message = f"results: {out}"
