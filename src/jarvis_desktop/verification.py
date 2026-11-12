"""Independent verification of desktop effects (does not trust the UI)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class VerificationResult:
    ok: bool
    checks: dict = field(default_factory=dict)
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {"ok": self.ok, "checks": self.checks, "duration_ms": round(self.duration_ms, 1)}


def probe_preconditions(path: Path, probe_dir: Path) -> dict:
    """Must hold before execution, so a leftover file can never count as success."""
    path, probe_dir = Path(path).resolve(), Path(probe_dir).resolve()
    return {"absent_before_run": not path.exists(), "inside_probe_dir": probe_dir in path.parents}


def verify_probe_file(path: Path, expected: str, probe_dir: Path, run_started_at: float,
                      absent_before_run: bool, wait_s: float = 10.0) -> VerificationResult:
    """Poll for the file, then check content, location and freshness."""
    start = time.perf_counter()
    path, probe_dir = Path(path).resolve(), Path(probe_dir).resolve()
    deadline = time.time() + wait_s
    while not path.exists() and time.time() < deadline:
        time.sleep(0.1)
    checks: dict = {"absent_before_run": absent_before_run, "exists": path.exists(),
                    "inside_probe_dir": probe_dir in path.parents}
    if checks["exists"]:
        content = path.read_text(encoding="utf-8-sig", errors="replace")
        mtime = path.stat().st_mtime
        checks["content_matches_run_id"] = content.strip() == expected
        # 2s slack for filesystem timestamp granularity.
        checks["written_after_run_start"] = mtime >= run_started_at - 2
    else:
        checks["content_matches_run_id"] = False
        checks["written_after_run_start"] = False
    return VerificationResult(all(checks.values()), checks, (time.perf_counter() - start) * 1000)
