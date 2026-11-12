"""Structured JSON Lines audit log with secret redaction."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import threading
import uuid
from collections import deque
from pathlib import Path

AUDIT_FIELDS = (
    "execution_id", "task_id", "action_id", "timestamp", "agent_or_component", "tool_name",
    "target_application", "target_window", "action_type", "status", "execution_duration_ms",
    "retry_count", "error_code", "error_category", "verification_status", "approval_status",
    "result_summary",
)

# Keys whose values are never written, whatever they contain.
SECRET_KEYS = re.compile(r"pass(word|code|phrase)?|secret|token|otp|api[_-]?key|private[_-]?key|credential|typed_text|value", re.I)
# Inline secrets inside free text.
SECRET_PATTERNS = [
    re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|otp)\s*[:=]\s*\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\b(?:ghp|gho|github_pat|sk|xox[abp])[-_][A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\buser_[0-9A-Za-z]{16,}\b"),   # provider account ids (e.g. in OpenRouter error bodies)
]


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def redact(obj, _key: str = ""):
    if isinstance(obj, dict):
        return {k: ("[REDACTED]" if SECRET_KEYS.search(k) and v not in (None, "") else redact(v, k))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        for pat in SECRET_PATTERNS:
            obj = pat.sub("[REDACTED]", obj)
        return obj
    return obj


def restrict_to_current_user(path: Path) -> bool:
    """Best-effort ACL: remove inherited access, grant only the current user."""
    user = os.environ.get("USERNAME")
    if os.name != "nt" or not user:
        return False
    r = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"],
                       capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode == 0


class AuditLogger:
    def __init__(self, directory: Path, execution_id: str | None = None, restrict_acl: bool = True):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.acl_restricted = restrict_to_current_user(self.directory) if restrict_acl else False
        self.execution_id = execution_id or "exec-" + uuid.uuid4().hex[:12]
        self.path = self.directory / f"audit-{dt.date.today().isoformat()}.jsonl"
        self._lock = threading.Lock()
        self.recent: deque[dict] = deque(maxlen=500)
        self._listeners = []
        # Phase 2: correlation fields (correlation_id, plan_id, step_id, role) merged into
        # every record while a run is active. One job runs at a time (JobManager).
        self.context: dict = {}

    def subscribe(self, fn) -> None:
        self._listeners.append(fn)

    def record(self, **fields) -> dict:
        rec = {k: None for k in AUDIT_FIELDS}
        rec.update(execution_id=self.execution_id,
                   timestamp=dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
                   action_id=fields.pop("action_id", None) or "act-" + uuid.uuid4().hex[:12],
                   retry_count=0)
        for k, v in self.context.items():
            if v is not None and k not in fields:
                rec[k] = v
        rec.update(fields)
        rec = redact(rec)
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            self.recent.append(rec)
        for fn in self._listeners:
            try:
                fn(rec)
            except Exception:
                pass
        return rec
