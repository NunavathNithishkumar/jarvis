"""Narrow wrapper around the pinned vercel-labs/agent-desktop CLI.

This is the only module that spawns agent-desktop. Every call is a
subprocess with a timeout whose handle the emergency stop can kill.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXE = PROJECT_ROOT / "node_modules" / "agent-desktop" / "bin" / "agent-desktop-win32-x64.exe"
PINNED_VERSION = "1.2.0"


class AdapterError(Exception):
    """A failed agent-desktop command, carrying its error envelope."""

    def __init__(self, code: str, message: str, *, delivery: str = "not_delivered",
                 retry: str = "safe", details: dict | None = None, command: str = ""):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.delivery = delivery
        self.retry = retry
        self.details = details or {}
        self.command = command

    @property
    def retry_safe(self) -> bool:
        return self.retry == "safe"


@dataclass
class AdapterResult:
    command: str
    data: Any
    duration_ms: float
    headed: bool
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def disposition(self) -> dict:
        return (self.data or {}).get("disposition", {}) if isinstance(self.data, dict) else {}


class DesktopAdapter:
    def __init__(self, exe: Path | str = DEFAULT_EXE, default_timeout_s: float = 60.0):
        self.exe = str(exe)
        self.default_timeout_s = default_timeout_s
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._killed = False
        self.call_count = 0

    # -- process control -------------------------------------------------
    def run(self, args: list[str], *, headed: bool = False, stdin: str | None = None,
            timeout_s: float | None = None) -> AdapterResult:
        argv = [self.exe] + (["--headed"] if headed else []) + args
        command = args[0] if args else ""
        start = time.perf_counter()
        with self._lock:
            self._killed = False
            self.call_count += 1
            self._proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            proc = self._proc
        try:
            out, err = proc.communicate(
                input=stdin.encode("utf-8") if stdin is not None else None,
                timeout=timeout_s or self.default_timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            # A timed-out action may have landed, so it is never "safe" to repeat blindly.
            raise AdapterError("TIMEOUT", f"agent-desktop {command} exceeded {timeout_s or self.default_timeout_s}s",
                               delivery="unknown", retry="unsafe", command=command)
        finally:
            with self._lock:
                self._proc = None
        duration_ms = (time.perf_counter() - start) * 1000
        if self._killed:
            raise AdapterError("KILLED", "agent-desktop process killed by emergency stop",
                               delivery="unknown", retry="unsafe", command=command)
        envelope = self._parse(out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), command)
        if not envelope.get("ok"):
            e = envelope.get("error") or {}
            disp = e.get("disposition") or {}
            raise AdapterError(e.get("code", "UNKNOWN"), e.get("message", ""),
                               delivery=disp.get("delivery", "unknown"), retry=disp.get("retry", "unsafe"),
                               details=e.get("details"), command=command)
        return AdapterResult(command, envelope.get("data"), duration_ms, headed, envelope)

    @staticmethod
    def _parse(stdout: str, stderr: str, command: str) -> dict:
        for line in reversed(stdout.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    break
        raise AdapterError("BAD_OUTPUT", f"no JSON envelope from {command}: {stderr.strip()[:300]}",
                           delivery="unknown", retry="unsafe", command=command)

    def kill_active(self) -> bool:
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return False
            self._killed = True
            proc.kill()
            return True

    # -- commands (thin, typed) -------------------------------------------
    def version(self) -> AdapterResult:
        return self.run(["version"], timeout_s=10)

    def status(self) -> AdapterResult:
        return self.run(["status"], timeout_s=10)

    def list_windows(self, app: str | None = None) -> AdapterResult:
        return self.run(["list-windows"] + (["--app", app] if app else []), timeout_s=15)

    def list_apps(self, app: str | None = None) -> AdapterResult:
        return self.run(["list-apps"] + (["--app", app] if app else []), timeout_s=15)

    def snapshot(self, app: str, window_id: str, *, skeleton: bool = False, interactive: bool = True,
                 compact: bool = True, max_depth: int | None = None, timeout_ms: int = 20000,
                 root: str | None = None) -> AdapterResult:
        args = ["snapshot", "--app", app, "--window-id", window_id, "--timeout-ms", str(timeout_ms)]
        if skeleton:
            args.append("--skeleton")
        if interactive:
            args.append("-i")
        if compact:
            args.append("--compact")
        if max_depth:
            args += ["--max-depth", str(max_depth)]
        if root:
            args += ["--root", root]
        return self.run(args, timeout_s=timeout_ms / 1000 + 15)

    def find(self, app: str, window_id: str, *, role: str | None = None, name: str | None = None,
             text: str | None = None, exact: bool = False) -> AdapterResult:
        args = ["find", "--app", app, "--window-id", window_id]
        if role:
            args += ["--role", role]
        if name:
            args += ["--name", name]
        if text:
            args += ["--text", text]
        if exact:
            args.append("--exact")
        return self.run(args, timeout_s=30)

    def get(self, ref: str, prop: str = "text") -> AdapterResult:
        return self.run(["get", ref, "--property", prop], timeout_s=20)

    def click(self, ref: str) -> AdapterResult:
        return self.run(["click", ref], timeout_s=20)

    def clear(self, ref: str) -> AdapterResult:
        return self.run(["clear", ref], timeout_s=20)

    def focus_window(self, window_id: str) -> AdapterResult:
        return self.run(["focus-window", "--window-id", window_id], timeout_s=15)

    def restore_window(self, app: str, window_id: str) -> AdapterResult:
        return self.run(["restore", "--app", app, "--window-id", window_id], timeout_s=15)

    def focus(self, ref: str, *, headed: bool) -> AdapterResult:
        return self.run(["focus", ref], headed=headed, timeout_s=20)

    def type_text(self, ref: str, text: str, *, headed: bool) -> AdapterResult:
        # --stdin keeps the text off the process command line.
        return self.run(["type", ref, "--stdin"], headed=headed, stdin=text, timeout_s=30)

    def press(self, combo: str, *, window_id: str | None = None, headed: bool) -> AdapterResult:
        args = ["press", combo] + (["--window-id", window_id] if window_id else [])
        return self.run(args, headed=headed, timeout_s=15)

    def launch(self, target: str, args: list[str] | None = None, timeout_ms: int = 30000) -> AdapterResult:
        return self.run(["launch", target, "--timeout", str(timeout_ms)] + [f"--arg={a}" for a in (args or [])],
                        timeout_s=timeout_ms / 1000 + 15)

    def screenshot(self, app: str, window_id: str, path: str) -> AdapterResult:
        return self.run(["screenshot", "--app", app, "--window-id", window_id, path], timeout_s=20)


def iter_nodes(tree: dict):
    """Depth-first walk of a snapshot tree."""
    stack = [tree]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.get("children", []) or []))
