"""Minimal local control UI. All state comes from the broker; every control calls the broker.

Bound to 127.0.0.1. Mutating requests need the per-launch token embedded in the page,
which blocks cross-site requests from browser tabs.
"""
from __future__ import annotations

import argparse
import secrets
import time
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..adapter import DesktopAdapter
from ..runtime import Runtime, build_runtime

STATIC = Path(__file__).parent / "static"


class Decision(BaseModel):
    approve: bool


class DemoRequest(BaseModel):
    window_id: str | None = None
    launch_if_missing: bool = False


class ReliabilityRequest(BaseModel):
    runs: int = 20


def create_app(rt: Runtime, token: str | None = None) -> FastAPI:
    token = token or secrets.token_urlsafe(24)
    app = FastAPI(title="JARVIS Desktop Broker", docs_url=None, redoc_url=None)
    app.state.rt, app.state.token = rt, token
    health = {"connected": False, "checked_at": 0.0, "version": None, "error": None}
    # Separate adapter so health probes never share the job adapter's killable process slot.
    probe_adapter = DesktopAdapter(rt.broker.adapter.exe)

    def auth(x_jarvis_token: str | None):
        if not x_jarvis_token or not secrets.compare_digest(x_jarvis_token, token):
            raise HTTPException(403, "missing or invalid X-Jarvis-Token")

    def check_health():
        if time.time() - health["checked_at"] < 10:
            return
        try:
            v = probe_adapter.version().data
            health.update(connected=True, version=v.get("version"), error=None)
        except Exception as e:
            health.update(connected=False, error=str(e)[:200])
        health["checked_at"] = time.time()

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text(encoding="utf-8").replace("__JARVIS_TOKEN__", token)

    @app.get("/api/state")
    def state(since: int = 0):
        check_health()
        b, job = rt.broker, rt.jobs.job
        task = rt.jobs.current_task
        last = job.runs[-1] if job and job.runs else None
        events = list(rt.audit.recent)
        return {
            "now": time.time(),
            "adapter": health,
            "estop": rt.estop.status(),
            "hotkey_registered": bool(rt.hotkey and rt.hotkey.registered.is_set()),
            "job": job.public() if job else None,
            "task": task.snapshot() if task else (last and {"state": last["status"], "task_id": last["task_id"]}),
            "current_action": b.current_action,
            "detected_window": (job.current.window_id and b.windows.get(job.current.window_id))
                               if job and job.current else (last and last.get("window_id") and b.windows.get(last["window_id"])),
            "approvals": [r.public() for r in rt.approvals.pending()],
            "credential_handoffs": rt.guard.active(),
            "events": events[-150:],
            "audit_log": str(rt.audit.path),
            "last_report": last,
        }

    @app.post("/api/demo/start")
    def start_demo(req: DemoRequest, x_jarvis_token: str | None = Header(None)):
        auth(x_jarvis_token)
        try:
            job = rt.jobs.start_demo(window_id=req.window_id, launch_if_missing=req.launch_if_missing)
        except Exception as e:
            raise HTTPException(409, str(e))
        return job.public()

    @app.post("/api/task/start")
    def start_task(req: DemoRequest, x_jarvis_token: str | None = Header(None)):
        auth(x_jarvis_token)
        try:
            job = rt.jobs.start_task(window_id=req.window_id, launch_if_missing=req.launch_if_missing)
        except Exception as e:
            raise HTTPException(409, str(e))
        return job.public()

    @app.post("/api/reliability/start")
    def start_rel(req: ReliabilityRequest, x_jarvis_token: str | None = Header(None)):
        auth(x_jarvis_token)
        try:
            job = rt.jobs.start_reliability(runs=max(1, min(req.runs, 20)))
        except Exception as e:
            raise HTTPException(409, str(e))
        return job.public()

    @app.post("/api/approvals/{request_id}")
    def decide(request_id: str, d: Decision, x_jarvis_token: str | None = Header(None)):
        auth(x_jarvis_token)
        if rt.estop.tripped and d.approve:
            raise HTTPException(409, "emergency stop active; approvals refused")
        try:
            r = rt.approvals.resolve(request_id, d.approve, actor="ui-operator")
        except KeyError:
            raise HTTPException(404, "unknown request")
        return r.public()

    def _task_or_409():
        t = rt.jobs.current_task
        if not t:
            raise HTTPException(409, "no active task")
        return t

    @app.post("/api/pause")
    def pause(x_jarvis_token: str | None = Header(None)):
        auth(x_jarvis_token)
        t = _task_or_409()
        ok = t.pause()
        rt.audit.record(agent_or_component="ui", action_type="pause", status="ok" if ok else "ignored", task_id=t.task_id)
        return {"ok": ok, "state": t.state.value}

    @app.post("/api/resume")
    def resume(x_jarvis_token: str | None = Header(None)):
        auth(x_jarvis_token)
        t = _task_or_409()
        if rt.estop.tripped:
            raise HTTPException(409, "emergency stop active")
        ok = t.resume()
        rt.audit.record(agent_or_component="ui", action_type="resume", status="ok" if ok else "ignored", task_id=t.task_id)
        return {"ok": ok, "state": t.state.value}

    @app.post("/api/stop")
    def stop(x_jarvis_token: str | None = Header(None)):
        """Cancel the current task (stops the reliability loop too)."""
        auth(x_jarvis_token)
        t = _task_or_409()
        t.request_cancel("operator stop")
        rt.approvals.cancel_all("operator stop")
        rt.audit.record(agent_or_component="ui", action_type="stop", status="ok", task_id=t.task_id)
        return {"ok": True}

    @app.post("/api/estop")
    def estop(x_jarvis_token: str | None = Header(None)):
        auth(x_jarvis_token)
        rt.estop.trip("ui_button")
        return rt.estop.status()

    @app.post("/api/rearm")
    def rearm(x_jarvis_token: str | None = Header(None)):
        auth(x_jarvis_token)
        ok, msg = rt.broker.rearm("ui-operator")
        if not ok:
            raise HTTPException(409, msg)
        return rt.estop.status()

    @app.post("/api/credentials/{handoff_id}/done")
    def cred_done(handoff_id: str, x_jarvis_token: str | None = Header(None)):
        auth(x_jarvis_token)
        if not rt.guard.confirm_complete(handoff_id):
            raise HTTPException(404, "unknown handoff")
        rt.audit.record(agent_or_component="credentials", action_type="handoff_completed", status="ok",
                        result_summary={"handoff_id": handoff_id})
        return {"ok": True}

    return app


def main():
    ap = argparse.ArgumentParser(description="JARVIS desktop broker control UI")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    rt = build_runtime()
    app = create_app(rt)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"JARVIS broker UI: {url}\nAudit log: {rt.audit.path}\n"
          f"Emergency stop: UI button, {rt.estop.status()['hotkey']} "
          f"({'registered' if rt.hotkey and rt.hotkey.registered.is_set() else 'NOT registered'}), "
          f"or create {rt.estop.sentinel_path}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        rt.close()


if __name__ == "__main__":
    main()
