"""Live, user-visible Phase 2 progress state (read by the UI; written by orchestrator/gateway)."""
from __future__ import annotations

import threading
import time
from collections import deque

from ..audit import redact

STATUS_TEXT = {
    "tool_start": {
        "inspect_vscode": "Looking at VS Code",
        "open_terminal": "Opening a terminal in VS Code",
        "propose_file_creation": "Preparing a file for your approval",
        "open_file_in_editor": "Opening the file in VS Code",
        "run_python_file": "Running the program in the VS Code terminal",
        "read_task_artifact": "Reading the result",
        "get_task_status": "Checking task status",
    },
}


class Timeline:
    def __init__(self, maxlen: int = 600):
        self._lock = threading.Lock()
        self.events: deque[dict] = deque(maxlen=maxlen)
        self.state: dict = {}
        self._seq = 0
        self.reset()

    def reset(self, **initial) -> None:
        with self._lock:
            self.events.clear()
            self.state = {"phase": "idle", "request": None, "intent": None, "plan": None, "active_role": None,
                          "active_step": None, "active_tool": None, "mode": None, "approval": None,
                          "outcome": None, "summary": None, "verification": None, "usage": None,
                          "started_at": time.time(), "finished_at": None, "status_text": None, **initial}

    def set(self, **kw) -> None:
        with self._lock:
            self.state.update(kw)

    def add(self, kind: str, **data) -> dict:
        data = redact(data)
        text = data.pop("text", None)
        if not text and kind == "tool_start":
            text = STATUS_TEXT["tool_start"].get(data.get("tool"), f"Using {data.get('tool')}")
        if not text and kind == "approval_waiting":
            text = "Waiting for your approval"
        if not text and kind == "approval_resolved":
            text = f"Approval {data.get('status')}"
        with self._lock:
            self._seq += 1
            ev = {"seq": self._seq, "ts": time.time(), "kind": kind, "text": text, **data}
            self.events.append(ev)
            if text:
                self.state["status_text"] = text
            if kind == "tool_start":
                self.state.update(active_tool=data.get("tool"), mode=data.get("mode"))
            elif kind == "approval_waiting":
                self.state["approval"] = {"status": "pending", "summary": data.get("summary"),
                                          "request_id": data.get("request_id")}
            elif kind == "approval_resolved":
                self.state["approval"] = {"status": data.get("status"), "request_id": data.get("request_id")}
        return ev

    def snapshot(self, since: int = 0) -> dict:
        with self._lock:
            st = dict(self.state)
            end = st.get("finished_at") or time.time()
            st["elapsed_s"] = round(end - (st.get("started_at") or end), 1)
            return {"state": st, "events": [e for e in self.events if e["seq"] > since]}
