"""Emergency stop latch with LLM-independent triggers: UI, global hotkey, sentinel file."""
from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from pathlib import Path

HOTKEY_DESCRIPTION = "Ctrl+Alt+Shift+F12"
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
VK_F12 = 0x7B
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012


class EmergencyStopActive(Exception):
    pass


class EmergencyStop:
    def __init__(self, sentinel_path: Path):
        self.sentinel_path = Path(sentinel_path)
        self._tripped = threading.Event()
        self._lock = threading.Lock()
        self.source: str | None = None
        self.tripped_at: float | None = None
        self.epoch = 0                      # bumps on every trip; invalidates outstanding refs
        self._handlers = []

    def on_trip(self, fn) -> None:
        self._handlers.append(fn)

    @property
    def tripped(self) -> bool:
        return self._tripped.is_set()

    def trip(self, source: str) -> bool:
        with self._lock:
            if self._tripped.is_set():
                return False
            self._tripped.set()
            self.source, self.tripped_at = source, time.time()
            self.epoch += 1
        for fn in self._handlers:
            try:
                fn(source)
            except Exception:
                pass
        return True

    def check(self) -> None:
        if self._tripped.is_set():
            raise EmergencyStopActive(f"emergency stop active (source: {self.source}); re-arm required")

    def rearm(self) -> tuple[bool, str]:
        """Deliberate re-arm. Refused while the sentinel file still exists."""
        if self.sentinel_path.exists():
            return False, f"remove sentinel file {self.sentinel_path} first"
        with self._lock:
            self._tripped.clear()
            self.source = None
        return True, "re-armed"

    def status(self) -> dict:
        return {"tripped": self.tripped, "source": self.source, "tripped_at": self.tripped_at,
                "epoch": self.epoch, "sentinel": str(self.sentinel_path), "hotkey": HOTKEY_DESCRIPTION}


class SentinelWatcher(threading.Thread):
    """Trips the latch when the sentinel file appears (e.g. `New-Item artifacts\\EMERGENCY_STOP`)."""

    def __init__(self, estop: EmergencyStop, interval_s: float = 0.1):
        super().__init__(daemon=True, name="estop-sentinel")
        self.estop, self.interval_s = estop, interval_s
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            if self.estop.sentinel_path.exists():
                self.estop.trip("sentinel_file")
            self._stop.wait(self.interval_s)

    def stop(self):
        self._stop.set()


class HotkeyListener(threading.Thread):
    """Global hotkey via RegisterHotKey on a dedicated message-loop thread."""

    def __init__(self, estop: EmergencyStop):
        super().__init__(daemon=True, name="estop-hotkey")
        self.estop = estop
        self.registered = threading.Event()
        self.error: str | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()

    def run(self):
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, VK_F12):
            self.error = f"RegisterHotKey failed (WinError {ctypes.GetLastError()}); hotkey may be taken"
            self._ready.set()
            return
        self.registered.set()
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    self.estop.trip("global_hotkey")
        finally:
            user32.UnregisterHotKey(None, 1)

    def wait_ready(self, timeout: float = 2.0) -> bool:
        self._ready.wait(timeout)
        return self.registered.is_set()

    def stop(self):
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)


# -- input release -----------------------------------------------------------
_KEYEVENTF_KEYUP = 0x0002
_MODIFIER_VKS = {"lshift": 0xA0, "rshift": 0xA1, "lctrl": 0xA2, "rctrl": 0xA3,
                 "lalt": 0xA4, "ralt": 0xA5, "lwin": 0x5B, "rwin": 0x5C}


def release_held_inputs() -> list[str]:
    """Send key-up for modifiers that are currently down. Keys not held are left alone
    (a stray Win key-up can open Start)."""
    if os.name != "nt":
        return []
    user32 = ctypes.windll.user32
    released = []
    for name, vk in _MODIFIER_VKS.items():
        if user32.GetAsyncKeyState(vk) & 0x8000:
            user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
            released.append(name)
    for name, vk, up_flag in (("lbutton", 0x01, 0x0004), ("rbutton", 0x02, 0x0010)):
        if user32.GetAsyncKeyState(vk) & 0x8000:
            user32.mouse_event(up_flag, 0, 0, 0, 0)
            released.append(name)
    return released


def send_hotkey_for_test() -> None:
    """Synthesize Ctrl+Alt+Shift+F12 (used by tests to exercise the hotkey path)."""
    user32 = ctypes.windll.user32
    keys = [0x11, 0x12, 0x10, VK_F12]  # ctrl, alt, shift, f12
    for vk in keys:
        user32.keybd_event(vk, 0, 0, 0)
    for vk in reversed(keys):
        user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
