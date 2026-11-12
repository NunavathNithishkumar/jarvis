"""Human approval requests and scoped batch grants. Timeout means deny."""
from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class ApprovalRequest:
    kind: str                    # "action" | "batch"
    summary: str
    details: dict                # shown verbatim to the human (exact command, target window, ...)
    fingerprint: str             # binds the approval to one exact action (or a batch template)
    timeout_s: float
    request_id: str = field(default_factory=lambda: "apr-" + uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)
    status: str = "pending"      # pending | approved | denied | timeout | cancelled
    resolved_at: float | None = None
    actor: str | None = None
    consumed: bool = False

    @property
    def expires_at(self) -> float:
        return self.created_at + self.timeout_s

    def public(self) -> dict:
        return {"request_id": self.request_id, "kind": self.kind, "summary": self.summary,
                "details": self.details, "status": self.status, "created_at": self.created_at,
                "expires_at": self.expires_at, "actor": self.actor}


@dataclass
class BatchGrant:
    request: ApprovalRequest
    command_regex: re.Pattern
    window_title_regex: re.Pattern
    max_runs: int
    ttl_s: float
    used: int = 0
    approved_at: float | None = None
    revoked: bool = False
    unique_commands: bool = False            # plan grants: each listed command may run once
    used_commands: set = field(default_factory=set)

    @property
    def grant_id(self) -> str:
        return self.request.request_id

    def active(self) -> bool:
        return (self.request.status == "approved" and not self.revoked and self.used < self.max_runs
                and self.approved_at is not None and time.time() < self.approved_at + self.ttl_s)


class ApprovalManager:
    def __init__(self, on_change=None):
        self._cond = threading.Condition()
        self._requests: dict[str, ApprovalRequest] = {}
        self._grants: dict[str, BatchGrant] = {}
        self._on_change = on_change or (lambda req: None)

    # -- requests -----------------------------------------------------------
    def request(self, kind: str, summary: str, details: dict, fingerprint: str, timeout_s: float) -> ApprovalRequest:
        req = ApprovalRequest(kind, summary, details, fingerprint, timeout_s)
        with self._cond:
            self._requests[req.request_id] = req
        self._on_change(req)
        return req

    def resolve(self, request_id: str, approve: bool, actor: str) -> ApprovalRequest:
        with self._cond:
            req = self._requests.get(request_id)
            if req is None:
                raise KeyError(request_id)
            if req.status != "pending":
                return req
            if time.time() > req.expires_at:
                req.status = "timeout"
            else:
                req.status = "approved" if approve else "denied"
                grant = self._grants.get(request_id)
                if grant and approve:
                    grant.approved_at = time.time()
            req.resolved_at, req.actor = time.time(), actor
            self._cond.notify_all()
        self._on_change(req)
        return req

    def wait(self, req: ApprovalRequest, should_abort=lambda: False) -> str:
        """Block until resolved, timed out (-> denied), or aborted by cancel/e-stop."""
        with self._cond:
            while req.status == "pending":
                if should_abort():
                    req.status, req.resolved_at, req.actor = "cancelled", time.time(), "system"
                    break
                remaining = req.expires_at - time.time()
                if remaining <= 0:
                    req.status, req.resolved_at, req.actor = "timeout", time.time(), "system"
                    break
                self._cond.wait(min(remaining, 0.25))
        self._on_change(req)
        return req.status

    def consume(self, request_id: str, fingerprint: str) -> bool:
        """Single-use: an approval authorises exactly the action it was shown for."""
        with self._cond:
            req = self._requests.get(request_id)
            if not req or req.kind != "action" or req.status != "approved" or req.consumed:
                return False
            if req.fingerprint != fingerprint:
                return False
            req.consumed = True
            return True

    def cancel_all(self, reason: str = "emergency stop") -> None:
        with self._cond:
            for req in self._requests.values():
                if req.status == "pending":
                    req.status, req.resolved_at, req.actor = "cancelled", time.time(), reason
            for g in self._grants.values():
                g.revoked = True
            self._cond.notify_all()

    def pending(self) -> list[ApprovalRequest]:
        with self._cond:
            now = time.time()
            return [r for r in self._requests.values() if r.status == "pending" and r.expires_at > now]

    def get(self, request_id: str) -> ApprovalRequest | None:
        return self._requests.get(request_id)

    # -- batch grants ---------------------------------------------------------
    def request_batch(self, summary: str, details: dict, command_regex: str, window_title_regex: str,
                      max_runs: int, ttl_s: float, timeout_s: float, unique_commands: bool = False,
                      kind: str = "batch") -> BatchGrant:
        req = ApprovalRequest(kind, summary, details, fingerprint=f"batch:{command_regex}", timeout_s=timeout_s)
        grant = BatchGrant(req, re.compile(command_regex), re.compile(window_title_regex), max_runs, ttl_s,
                           unique_commands=unique_commands)
        with self._cond:
            self._requests[req.request_id] = req
            self._grants[req.request_id] = grant
        self._on_change(req)
        return grant

    def consume_batch(self, grant_id: str, command: str, window_title: str) -> bool:
        with self._cond:
            g = self._grants.get(grant_id)
            if not g or not g.active():
                return False
            if not g.command_regex.fullmatch(command) or not g.window_title_regex.search(window_title or ""):
                return False
            if g.unique_commands and command in g.used_commands:
                return False
            g.used_commands.add(command)
            g.used += 1
            return True

    def grant(self, grant_id: str) -> BatchGrant | None:
        return self._grants.get(grant_id)

    def request_plan(self, summary: str, details: dict, commands: list[str], window_title_regex: str,
                     ttl_s: float, timeout_s: float) -> BatchGrant:
        """Approve an exact list of commands once; each may run exactly one time."""
        rx = "|".join(f"(?:{re.escape(c)})" for c in commands)
        return self.request_batch(summary, {**details, "commands": commands}, rx, window_title_regex,
                                  len(commands), ttl_s, timeout_s, unique_commands=True, kind="plan")
