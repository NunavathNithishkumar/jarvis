"""Human-controlled credential handoff.

While a handoff is active for an app, the broker refuses every observation and
input action on that app, so no snapshot, screenshot or keystroke log can
capture what the human types. Automation resumes only after the human confirms.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class Handoff:
    app: str
    window_title: str | None
    reason: str
    handoff_id: str = field(default_factory=lambda: "cred-" + uuid.uuid4().hex[:10])
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    status: str = "waiting_for_user"   # waiting_for_user | completed | aborted

    def public(self) -> dict:
        return {"handoff_id": self.handoff_id, "app": self.app, "window_title": self.window_title,
                "reason": self.reason, "status": self.status, "started_at": self.started_at,
                "message": f"{self.app} needs you to sign in. Use the original window directly; "
                           "JARVIS is not observing or typing into it. Confirm here when done."}


class CredentialGuard:
    def __init__(self):
        self._cond = threading.Condition()
        self._active: dict[str, Handoff] = {}

    def begin(self, app: str, window_title: str | None, reason: str) -> Handoff:
        h = Handoff(app.lower(), window_title, reason)
        with self._cond:
            self._active[h.app] = h
        return h

    def is_blocked(self, app: str | None) -> bool:
        return bool(app) and app.lower() in self._active

    def confirm_complete(self, handoff_id: str) -> bool:
        with self._cond:
            for app, h in list(self._active.items()):
                if h.handoff_id == handoff_id:
                    h.status, h.completed_at = "completed", time.time()
                    del self._active[app]
                    self._cond.notify_all()
                    return True
        return False

    def wait(self, handoff: Handoff, timeout_s: float, should_abort=lambda: False) -> str:
        deadline = time.time() + timeout_s
        with self._cond:
            while handoff.status == "waiting_for_user":
                if should_abort() or time.time() > deadline:
                    handoff.status = "aborted"
                    self._active.pop(handoff.app, None)
                    break
                self._cond.wait(0.25)
        return handoff.status

    def active(self) -> list[dict]:
        with self._cond:
            return [h.public() for h in self._active.values()]
