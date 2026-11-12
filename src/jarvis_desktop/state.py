"""Task execution state machine and the pause/cancel gate."""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class TaskState(str, Enum):
    CREATED = "CREATED"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    PAUSED = "PAUSED"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL = {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}
_S = TaskState
TRANSITIONS: dict[TaskState, set[TaskState]] = {
    _S.CREATED: {_S.READY, _S.CANCELLED, _S.FAILED},
    _S.READY: {_S.RUNNING, _S.CANCELLED, _S.FAILED},
    _S.RUNNING: {_S.WAITING_FOR_APPROVAL, _S.PAUSED, _S.VERIFYING, _S.FAILED, _S.CANCELLED},
    _S.WAITING_FOR_APPROVAL: {_S.RUNNING, _S.PAUSED, _S.FAILED, _S.CANCELLED},
    _S.PAUSED: {_S.RUNNING, _S.WAITING_FOR_APPROVAL, _S.VERIFYING, _S.CANCELLED, _S.FAILED},
    _S.VERIFYING: {_S.RUNNING, _S.PAUSED, _S.COMPLETED, _S.FAILED, _S.CANCELLED},
    _S.COMPLETED: set(),
    _S.FAILED: set(),
    _S.CANCELLED: set(),
}


class InvalidTransition(Exception):
    pass


class TaskCancelled(Exception):
    """Raised at a gate when the task was cancelled or the emergency stop tripped."""


@dataclass
class Task:
    name: str
    task_id: str = field(default_factory=lambda: "task-" + uuid.uuid4().hex[:12])
    state: TaskState = TaskState.CREATED
    created_at: float = field(default_factory=time.time)
    current_step: str | None = None
    current_action: str | None = None
    completed_steps: list[str] = field(default_factory=list)
    dispatched_steps: set[str] = field(default_factory=set)  # non-idempotent steps that may have landed
    history: list[dict] = field(default_factory=list)
    error: dict | None = None
    result: dict | None = None
    stop_reason: str | None = None

    def __post_init__(self):
        self._cond = threading.Condition()
        self._resume_to: TaskState | None = None
        self._cancel_requested = False

    # -- state machine ----------------------------------------------------
    def transition(self, new: TaskState, reason: str = "") -> None:
        with self._cond:
            if new not in TRANSITIONS[self.state]:
                raise InvalidTransition(f"{self.state.value} -> {new.value}")
            self.history.append({"from": self.state.value, "to": new.value, "at": time.time(), "reason": reason})
            self.state = new
            if new in TERMINAL and reason:
                self.stop_reason = reason
            self._cond.notify_all()

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    # -- operator controls --------------------------------------------------
    def pause(self) -> bool:
        with self._cond:
            if self.state not in (TaskState.RUNNING, TaskState.WAITING_FOR_APPROVAL, TaskState.VERIFYING):
                return False
            self._resume_to = self.state
            self.transition(TaskState.PAUSED, "operator pause")
            return True

    def resume(self) -> bool:
        with self._cond:
            if self.state != TaskState.PAUSED or self._cancel_requested:
                return False
            self.transition(self._resume_to or TaskState.RUNNING, "operator resume")
            return True

    def request_cancel(self, reason: str) -> None:
        with self._cond:
            self._cancel_requested = True
            self.stop_reason = reason
            self._cond.notify_all()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested

    def gate(self) -> None:
        """Block while paused; raise if cancelled. Called before every desktop action."""
        with self._cond:
            while self.state == TaskState.PAUSED and not self._cancel_requested:
                self._cond.wait(0.25)
            if self._cancel_requested:
                raise TaskCancelled(self.stop_reason or "cancelled")

    def snapshot(self) -> dict:
        return {
            "task_id": self.task_id, "name": self.name, "state": self.state.value,
            "current_step": self.current_step, "current_action": self.current_action,
            "completed_steps": list(self.completed_steps), "error": self.error,
            "result": self.result, "stop_reason": self.stop_reason, "history": list(self.history),
        }
