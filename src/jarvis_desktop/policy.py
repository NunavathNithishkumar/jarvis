"""Deny-by-default action policy, loaded from config/policy.yaml."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import yaml

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "policy.yaml"


class Category(str, Enum):
    READ_ONLY = "read_only"
    LOW_RISK = "low_risk"
    APPROVAL_REQUIRED = "approval_required"
    DENIED = "denied"


@dataclass
class Action:
    """A request to the broker. `window` is resolved by the broker, never trusted from the caller."""
    action_type: str
    app: str | None = None
    window_id: str | None = None
    params: dict = field(default_factory=dict)
    # Filled in by the broker before classification:
    window_title: str | None = None
    process_name: str | None = None
    element: dict | None = None  # {"role","name","states"} of the resolved target element


@dataclass
class Decision:
    category: Category
    reason: str
    scope: str | None = None
    template: str | None = None
    template_match: dict | None = None

    @property
    def allowed_without_approval(self) -> bool:
        return self.category in (Category.READ_ONLY, Category.LOW_RISK)


class Policy:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.scopes = {k: (v["app"].lower(), re.compile(v["window_title_regex"])) for k, v in cfg["scopes"].items()}
        self.excluded = [re.compile(p) for p in cfg.get("excluded_window_title_regex", [])]
        self.read_only = set(cfg.get("read_only", []))
        self.low_risk = {k: set(v) for k, v in (cfg.get("low_risk") or {}).items()}
        self.approval = {k: set(v) for k, v in (cfg.get("approval_required") or {}).items()}
        d = cfg.get("denied") or {}
        self.denied_types = set(d.get("action_types", []))
        self.sensitive = [re.compile(p, re.I) for p in d.get("sensitive_element_patterns", [])]
        self.sensitive_roles = {r.lower() for r in d.get("sensitive_roles", [])}
        self.sensitive_states = {s.lower() for s in d.get("sensitive_states", [])}
        self.denied_processes = {p.lower() for p in d.get("processes", [])}
        self.templates = {k: re.compile(v.strip()) for k, v in (cfg.get("command_templates") or {}).items()}
        self.approval_cfg = cfg.get("approval", {})

    @classmethod
    def load(cls, path: Path | str = DEFAULT_POLICY_PATH) -> "Policy":
        with open(path, encoding="utf-8") as f:
            return cls(yaml.safe_load(f))

    # -- helpers ----------------------------------------------------------
    def is_sensitive_element(self, element: dict | None) -> bool:
        if not element:
            return False
        role = (element.get("role") or "").lower()
        if role in self.sensitive_roles:
            return True
        if {s.lower() for s in element.get("states") or []} & self.sensitive_states:
            return True
        name = element.get("name") or ""
        return any(p.search(name) for p in self.sensitive)

    def is_excluded_window(self, title: str | None) -> bool:
        return bool(title) and any(p.search(title) for p in self.excluded)

    def matching_scope(self, action: Action, allowed: set[str]) -> str | None:
        if self.is_excluded_window(action.window_title):
            return None
        for name in allowed:
            app, title_re = self.scopes[name]
            if (action.app or "").lower() == app and action.window_title and title_re.search(action.window_title):
                return name
        return None

    def match_command(self, command: str) -> tuple[str, dict] | None:
        for name, pat in self.templates.items():
            m = pat.fullmatch(command)
            if m:
                return name, m.groupdict()
        return None

    # -- classification -----------------------------------------------------
    def classify(self, action: Action) -> Decision:
        t = action.action_type
        if t in self.denied_types:
            return Decision(Category.DENIED, f"action type '{t}' is denied")
        if (action.process_name or "").lower() in self.denied_processes:
            return Decision(Category.DENIED, f"process '{action.process_name}' is protected")
        if self.is_sensitive_element(action.element):
            return Decision(Category.DENIED, "target element is a credential/sensitive field")
        if t == "get" and (action.params.get("property") or "text") == "value" and action.element is None:
            return Decision(Category.DENIED, "reading an unidentified element's value is denied")
        if t in self.read_only:
            if self.is_excluded_window(action.window_title) and t not in ("list_windows", "list_apps", "status"):
                return Decision(Category.DENIED, "window is excluded from automation")
            return Decision(Category.READ_ONLY, "read-only observation")
        if t in self.low_risk:
            scope = self.matching_scope(action, self.low_risk[t])
            if scope:
                return Decision(Category.LOW_RISK, f"low-risk action inside scope '{scope}'", scope=scope)
            return Decision(Category.DENIED, f"'{t}' outside any permitted window scope")
        if t in self.approval:
            scope = self.matching_scope(action, self.approval[t])
            if not scope:
                return Decision(Category.DENIED, f"'{t}' outside any permitted window scope")
            if t == "terminal_command":
                m = self.match_command(action.params.get("command", ""))
                if not m:
                    return Decision(Category.DENIED, "command does not match an allowed template")
                return Decision(Category.APPROVAL_REQUIRED, "terminal command requires approval",
                                scope=scope, template=m[0], template_match=m[1])
            return Decision(Category.APPROVAL_REQUIRED, f"'{t}' requires approval", scope=scope)
        return Decision(Category.DENIED, f"action type '{t}' is not classified (deny by default)")
