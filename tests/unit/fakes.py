"""Scriptable stand-in for agent-desktop. Unit tests only: never evidence of desktop behaviour."""
from __future__ import annotations

from jarvis_desktop.adapter import AdapterError, AdapterResult, DesktopAdapter

PROBE = {"id": "w-probe", "title": "Welcome - probe-workspace - Visual Studio Code", "app_name": "Code.exe",
         "is_focused": True, "minimized": False, "pid": 1}
HOST = {"id": "w-host", "title": "JARVIS Phase 1 - Jarvis - Visual Studio Code", "app_name": "Code.exe",
        "is_focused": False, "minimized": False, "pid": 1}


class FakeAdapter(DesktopAdapter):
    def __init__(self):
        super().__init__(exe="fake")
        self.windows = [dict(PROBE), dict(HOST)]
        self.calls: list[tuple] = []
        self.elements: dict[str, list[dict]] = {}    # window_id -> nodes (role, name, ref_id, states, value)
        self.stale_once: set[str] = set()             # refs that raise STALE_REF on first use
        self.fail: dict[str, AdapterError] = {}       # command -> error to raise
        self.foreground = "w-probe"
        self.on_call = None                           # hook(cmd, args) for test interleavings
        self._n = 0
        self.typed: list[str] = []
        self.pressed: list[str] = []

    def _r(self, cmd, data, headed=False, *args):
        self.calls.append((cmd, *args))
        if self.on_call:
            self.on_call(cmd, args)
        if cmd in self.fail:
            raise self.fail[cmd]
        return AdapterResult(cmd, data, 1.0, headed)

    def _ok(self):
        return {"disposition": {"delivery": "delivered_unverified", "retry": "unsafe"}}

    def version(self):
        return self._r("version", {"version": "1.2.0"})

    def status(self):
        return self._r("status", {"platform": "windows"})

    def list_windows(self, app=None):
        ws = [dict(w, is_focused=(w["id"] == self.foreground)) for w in self.windows]
        return self._r("list-windows", [w for w in ws if not app or w["app_name"] == app])

    def list_apps(self, app=None):
        return self._r("list-apps", {"apps": []})

    def snapshot(self, app, window_id, **kw):
        nodes = [dict(n) for n in self.elements.get(window_id, [])]
        return self._r("snapshot", {"ref_count": len(nodes), "complete": True,
                                    "tree": {"role": "window", "children": nodes}}, False, window_id)

    def find(self, app, window_id, role=None, name=None, text=None, exact=False):
        out = []
        for n in self.elements.get(window_id, []):
            nm = n.get("name") or ""
            if role and n.get("role") != role:
                continue
            if name and ((nm.lower() != name.lower()) if exact else (name.lower() not in nm.lower())):
                continue
            if text and text.lower() not in nm.lower():
                continue
            self._n += 1
            out.append(dict(n))
        return self._r("find", {"matches": out, "total_matches": len(out)}, False, window_id)

    def get(self, ref, prop="text"):
        return self._r("get", {"value": "secret-value" if prop == "value" else "text"}, False, ref)

    def _ref_action(self, cmd, ref, headed=False):
        if ref in self.stale_once:
            self.stale_once.discard(ref)
            self.calls.append((cmd, ref))
            raise AdapterError("STALE_REF", "gone", delivery="not_delivered", retry="safe", command=cmd)
        return self._r(cmd, self._ok(), headed, ref)

    def click(self, ref):
        return self._ref_action("click", ref)

    def clear(self, ref):
        return self._ref_action("clear", ref)

    def focus(self, ref, *, headed):
        return self._ref_action("focus", ref, headed)

    def focus_window(self, window_id):
        self.foreground = window_id
        return self._r("focus-window", {}, False, window_id)

    def restore_window(self, app, window_id):
        return self._r("restore", {}, False, window_id)

    def type_text(self, ref, text, *, headed):
        self.typed.append(text)
        return self._r("type", self._ok(), headed, ref)

    def press(self, combo, *, window_id=None, headed):
        self.pressed.append(combo)
        return self._r("press", self._ok(), headed, combo)

    def launch(self, target, args=None, timeout_ms=30000):
        return self._r("launch", {}, False, target)

    def screenshot(self, app, window_id, path):
        return self._r("screenshot", {"path": path}, False, window_id)

    def kill_active(self):
        return False
