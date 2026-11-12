"""REAL Windows desktop + REAL model (OpenRouter/OpenAI) + REAL OpenJarvis + Phase 1 broker (tests 19, 20, 21).

Opt-in and operator-authorized only:
    1. Open VS Code on artifacts\\probe-workspace (window title "... - probe-workspace - Visual Studio Code").
    2. $env:JARVIS_LIVE_TESTS_AUTHORIZED = "1"   (you pre-authorize this run's approvals, see below)
    3. .venv\\Scripts\\python -m pytest -m "desktop and llm" tests/phase2/live -v -s
    Do not touch mouse/keyboard while it runs; Ctrl+Alt+Shift+F12 stops everything.

Approvals: the broker still requires an approval for every write/open/run. In this unattended
mode the *test operator* (you, via the env var above) pre-authorizes a narrow approver that
prints each request and approves only: plan reviews, write_workspace_file inside the probe
workspace, and the exact Phase 1 open/run templates for this task's files. Anything else is
DENIED. For a human-in-the-loop demo use the UI instead (PHASE2_DEMO_GUIDE.md).

Ground truth is computed here, independently of the model and of the runtime's own verifier.
"""
import collections
import json
import os
import re
import threading
import time
from pathlib import Path

import pytest

from jarvis_desktop.runtime import PROBE_WORKSPACE, build_runtime
from jarvis_desktop.task_verifier import parse_output_json

pytestmark = [pytest.mark.desktop, pytest.mark.llm]
RESULTS = Path(__file__).resolve().parents[3] / "artifacts" / "test_results"
PRIMES = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47]
HINGLISH = ("Jarvis, VS Code mein ek naya Python project banao. Ek script likho jo 1 se 100 tak sum, 50 se chhote "
            "primes, aur fibonacci(20) calculate kare. File open karo, run karo aur output verify karo.")
WORDS = "the quick brown fox jumps over the lazy dog the fox"
WORDFREQ = (f"Create a word-frequency Python script for this synthetic text: '{WORDS}'. Print the count of every "
            "word as JSON under the key word_counts, open the file in VS Code, run it and verify the output.")
LOG: list = []


@pytest.fixture(scope="module")
def rt():
    if os.environ.get("JARVIS_LIVE_TESTS_AUTHORIZED") != "1":
        pytest.skip("set JARVIS_LIVE_TESTS_AUTHORIZED=1 to authorize live desktop runs")
    r = build_runtime()
    from jarvis_desktop.phase2_services import get_services
    try:
        get_services(r)
    except Exception as e:
        r.close()
        pytest.skip(f"model not configured: {e}")
    yield r
    r.close()
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"phase2-live-{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(
        json.dumps(LOG, indent=2, default=str), encoding="utf-8")


def operator_approver(rt, stop: threading.Event, seen: list):
    ws = str(PROBE_WORKSPACE.resolve())
    rx_open = re.compile(r"^code -r '" + re.escape(ws) + r"\\task-[0-9a-f]{8}\\[a-z0-9_]+\.py'$")
    rx_run = re.compile(r"^python '" + re.escape(ws) + r"\\task-[0-9a-f]{8}\\[a-z0-9_]+\.py' \| Tee-Object "
                        r"-FilePath '" + re.escape(ws) + r"\\task-[0-9a-f]{8}\\[a-z_]+\.txt'$")
    while not stop.is_set():
        for r in rt.approvals.pending():
            if r.request_id in {x["id"] for x in seen}:
                continue
            d = r.details
            ok = (r.kind == "plan_review"
                  or (d.get("action_type") == "write_workspace_file" and str(d.get("file_path", "")).startswith(ws))
                  or (d.get("action_type") == "terminal_command" and (rx_open.match(d.get("command") or "")
                                                                      or rx_run.match(d.get("command") or ""))))
            print(f"\n[operator-approver] {'APPROVE' if ok else 'DENY'} {r.kind}: {r.summary}\n"
                  f"   {d.get('command') or d.get('file_path') or ''}")
            if d.get("file_content"):
                print("   ---- file content ----\n" + d["file_content"] + "   ----------------------")
            seen.append({"id": r.request_id, "kind": r.kind, "summary": r.summary, "approved": ok})
            rt.approvals.resolve(r.request_id, ok, "test-operator(pre-authorized)")
        time.sleep(0.1)


def run_task(rt, text, timeout=900):
    seen, stop = [], threading.Event()
    th = threading.Thread(target=operator_approver, args=(rt, stop, seen), daemon=True)
    th.start()
    try:
        job = rt.jobs.start_chat(text)
        deadline = time.time() + timeout
        while job.status == "running" and time.time() < deadline:
            time.sleep(0.5)
    finally:
        stop.set()
    assert job.status != "running", "timed out"
    res = job.runs[-1] if job.runs else {"outcome": "ERROR", "error": job.message}
    entry = {"request": text, "outcome": res.get("outcome"), "error": res.get("error"),
             "plans": res.get("plans"), "replans": res.get("replans"), "usage": res.get("usage"),
             "verification": res.get("verification"), "duration_ms": res.get("duration_ms"),
             "approvals": seen, "task_dir": res.get("task_dir"), "files": res.get("files"),
             "openjarvis_event_types": res.get("openjarvis_event_types"), "tool_calls": res.get("tool_calls")}
    LOG.append(entry)
    return res, entry


def output_json(res):
    outs = res.get("outputs") or {}
    assert outs, "no program output"
    last = list(outs.values())[-1]
    p = Path(last["path"])
    raw = p.read_bytes()
    txt = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
    return parse_output_json(txt), txt


def test_19_hinglish_sum_primes_fib_on_real_desktop(rt):
    res, entry = run_task(rt, HINGLISH)
    assert res["outcome"] == "VERIFIED_SUCCESS", (res.get("error"), res.get("verification"))
    data, txt = output_json(res)
    vals = list((data or {}).values())
    # independent ground truth (F0=0, F1=1)
    assert 5050 in vals and PRIMES in vals and 6765 in vals, txt
    for meta in res["files"].values():
        assert Path(meta["path"]).exists()
    entry["harness_ground_truth"] = "passed"


def test_20_word_frequency_different_plan_on_real_desktop(rt):
    res, entry = run_task(rt, WORDFREQ)
    assert res["outcome"] == "VERIFIED_SUCCESS", (res.get("error"), res.get("verification"))
    data, txt = output_json(res)
    truth = dict(collections.Counter(WORDS.split()))
    found = [v for v in (data or {}).values() if isinstance(v, dict)]
    assert truth in found, txt
    first = next(e for e in LOG if e["request"] == HINGLISH) if any(e["request"] == HINGLISH for e in LOG) else None
    if first:
        assert [s["title"] for s in first["plans"][-1]["steps"]] != [s["title"] for s in res["plans"][-1]["steps"]]
    entry["harness_ground_truth"] = "passed"


def test_21_consecutive_live_runs(rt):
    n = int(os.environ.get("JARVIS_LIVE_RUNS", "3"))
    outcomes = []
    for k in range(n):
        if rt.estop.tripped:
            break
        res, entry = run_task(rt, HINGLISH if k % 2 == 0 else WORDFREQ)
        entry["iteration"] = k + 1
        outcomes.append(res["outcome"])
        time.sleep(1.0)
    LOG.append({"consecutive_summary": {"requested": n, "ran": len(outcomes),
                                        "verified_success": outcomes.count("VERIFIED_SUCCESS"),
                                        "outcomes": outcomes}})
    # Safety regression: whatever the outcome, no run may be reported as success without verification.
    for e in LOG:
        if e.get("outcome") == "VERIFIED_SUCCESS":
            assert e["verification"] and e["verification"]["ok"]
    assert outcomes.count("VERIFIED_SUCCESS") == len(outcomes), outcomes
