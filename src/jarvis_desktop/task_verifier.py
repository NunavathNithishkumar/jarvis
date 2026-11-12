"""Independent, deterministic verification of a Phase 2 task (no LLM involved).

Evidence comes from the filesystem only: the files the broker wrote (hash-checked
against the approved content) and the output files the program produced in this
task (fresh: absent before the run). A model never decides that a task succeeded.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .audit import sha256
from .tools.broker_gateway import read_text_any


@dataclass
class VerificationReport:
    ok: bool
    checks: list = field(default_factory=list)
    duration_ms: float = 0.0

    def public(self) -> dict:
        return {"ok": self.ok, "checks": self.checks, "duration_ms": round(self.duration_ms, 1)}


def parse_output_json(text: str) -> dict | None:
    """The last line that parses as a JSON object (programs are told to print exactly one)."""
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                v = json.loads(line)
                if isinstance(v, dict):
                    return v
            except json.JSONDecodeError:
                continue
    try:
        v = json.loads((text or "").strip())
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        return None


def _equal(actual, expected) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return actual is expected if isinstance(expected, bool) else False
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(actual - expected) < 1e-9
    if isinstance(expected, list) and isinstance(actual, list):
        return len(expected) == len(actual) and all(_equal(a, e) for a, e in zip(actual, expected))
    if isinstance(expected, dict) and isinstance(actual, dict):
        return set(expected) == set(actual) and all(_equal(actual[k], expected[k]) for k in expected)
    return actual == expected


def _distinctive(v) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return abs(v) >= 100
    if isinstance(v, (list, dict)):
        return len(json.dumps(v)) >= 12
    return False


def verify_task(session, criteria: list) -> VerificationReport:
    t0 = time.perf_counter()
    checks: list[dict] = []
    for name, meta in session.files.items():
        p = Path(meta["path"])
        exists = p.exists()
        same = exists and sha256(p.read_text(encoding="utf-8")) == meta["sha256"]
        inside = session.task_dir.resolve() in p.resolve().parents
        checks.append({"check": f"file_integrity:{name}", "ok": bool(exists and same and inside),
                       "evidence": {"exists": exists, "matches_approved_sha256": same, "inside_task_dir": inside}})
    outputs = list(session.outputs.values())
    if not outputs and any(getattr(c, "kind", None) in ("output_json_equals", "output_contains") for c in criteria):
        checks.append({"check": "program_output_present", "ok": False, "evidence": "no program output captured"})
    latest = outputs[-1] if outputs else None
    if latest:
        p = Path(latest["path"])
        text = read_text_any(p) if p.exists() else ""
        checks.append({"check": "program_output_fresh", "ok": bool(p.exists() and latest.get("fresh")),
                       "evidence": {"path": str(p), "script": latest["script"], "fresh": latest.get("fresh")}})
        parsed = parse_output_json(text)
        for c in criteria:
            kind = getattr(c, "kind", None)
            if kind == "output_json_equals":
                actual = (parsed or {}).get(c.key, "<missing>") if parsed is not None else "<no JSON output>"
                checks.append({"check": f"output_json_equals:{c.key}", "ok": parsed is not None and c.key in parsed
                               and _equal(parsed[c.key], c.value),
                               "evidence": {"expected": c.value, "actual": actual}})
            elif kind == "output_contains":
                checks.append({"check": "output_contains", "ok": c.text in text,
                               "evidence": {"text": c.text}})
        # Anti-hardcoding: a distinctive expected value must be computed, not typed into the source.
        src = ""
        if latest["script"] in session.files:
            sp = Path(session.files[latest["script"]]["path"])
            src = sp.read_text(encoding="utf-8") if sp.exists() else ""
        for c in criteria:
            if getattr(c, "kind", None) == "output_json_equals" and _distinctive(c.value):
                lit = [json.dumps(c.value), str(c.value)]
                found = any(x in src for x in lit) or (isinstance(c.value, list) and
                                                        json.dumps(c.value, separators=(",", ":")) in src.replace(" ", ""))
                checks.append({"check": f"not_hardcoded:{c.key}", "ok": not found,
                               "evidence": {"expected_literal_in_source": found}})
    for c in criteria:
        if getattr(c, "kind", None) == "file_exists":
            found = session.resolve_known(c.filename)
            checks.append({"check": f"file_exists:{c.filename}", "ok": bool(found),
                           "evidence": {"resolved": found}})
    ok = bool(checks) and all(c["ok"] for c in checks)
    return VerificationReport(ok, checks, (time.perf_counter() - t0) * 1000)
