# JARVIS Phase 1 — Test Report

Date: 2026-11-13 · Machine: Windows 11 Home 10.0.26300 (x64), interactive session
Stack: Python 3.11.3, agent-desktop **1.2.0** (npm, sha256 of win32-x64 binary `d2746670…fd94c3`), VS Code (user install)
Sources: `artifacts/test_results/` (junit, integration observations, reliability JSON) and `artifacts/audit_logs/audit-2026-11-13.jsonl`

Evidence levels used below:
- **Real desktop**: ran against the actual VS Code probe window.
- **Unit**: broker/policy logic with a scripted fake adapter. This is not evidence of desktop behaviour.

## 1. Summary

| Area | Result |
|---|---|
| Unit tests | **58 / 58 passed** (fresh run, `unit-junit.xml`) |
| Real-desktop integration tests (pytest `-m desktop`) | **6 / 7 passed**: B, C-sentinel, C-hotkey, D, E, G pass; F failed (see §3) |
| VS Code probe demo (Test A) | **Passed on real desktop**: 3 verified completions |
| Multi-step "build and run Python" task | **Passed on real desktop** on the 3rd attempt. The first two attempts exposed 2 bugs, both fixed |
| Reliability ×20 (Test J) | **Not met.** Only one run attempted: 1 succeeded, 1 failed (focus lost), then the operator stopped it. 20 consecutive runs have not been done |

**Phase 1 is not complete:** Test J (20/20) and a real-desktop rerun of Test F are still outstanding.

## 2. Spec test matrix (A–J)

| Test | Requirement | Evidence | Result |
|---|---|---|---|
| A Normal execution | Detect → inspect → open terminal → approve → execute → verify | Real: 3 completed runs (20:22, 20:29, 20:30 UTC). All 5 file checks passed (absent before, exists, in probe dir, content = run id, fresh) | **Pass** |
| B Approval denial | Nothing executes, no file | Real: denied → `CANCELLED`, 0 new files, no type/press actions in audit. Unit: deny + 120 s timeout → deny | **Pass** |
| C Emergency stop | No action accepted after stop | Real, sentinel file: workflow cancelled while waiting for approval; next action rejected without reaching agent-desktop; re-arm refused until sentinel removed. Real, hotkey: `RegisterHotKey` Ctrl+Alt+Shift+F12 tripped the latch. Real, UI button: used by operator at 20:31, run 3 cancelled. Unit: stop between typing and Enter → Enter never sent | **Pass** (hotkey tested with a *synthesized* key press; a physical press is untested) |
| D Stale reference | Refresh and re-identify, never guess | Real: old terminal ref → agent-desktop `STALE_REF` (`saved_ref_absent`); broker re-looked it up, found no matching element, refused. Real, live runs: `kill_terminal` hit `STALE_REF` twice and recovered after one fresh lookup each time. Unit: identical twins → `AMBIGUOUS_ELEMENT`, no click; unsafe retry never repeated | **Pass** |
| E Missing application | Clear failure or approved launch | Real: `APP_NOT_FOUND`, no actions; launch denied → nothing launched. Real: approved launch opened the probe window twice (via `code.cmd` fallback) | **Pass** |
| F Ambiguous window | Explicit target resolution | Unit: 2 matching windows → `AMBIGUOUS_WINDOW` with candidates, explicit choice works. **Real: failed**: the second test window did not appear within 40 s (launched via `Code.exe -n`; the operator was also closing windows at the time). Launch method since changed to `code.cmd`, not rerun | **Unit pass / Real not verified** |
| G Input method | Determine semantic vs headed | Real findings: headless `set-value` reports `delivered_verified` but **text never reaches the shell** (screenshot evidence); headless `type` → `POLICY_DENIED`; headed SendInput type + Enter **works** | **Done**: headed input required |
| H Credential boundary | Sensitive fields blocked, absent from logs | Unit only (simulated by design, no real credentials): password/OTP fields hidden in snapshots, `get`/`click` denied, value absent from audit log; credential hand-off suspends all observation of that app until the user confirms | **Pass (simulated)** |
| I Audit completeness | Timestamps, types, status, duration, verification | Unit: every record has all 17 spec fields. Real log: 16 workflow runs with `task_finished` records, per-action durations, approval and verification status | **Pass** |
| J Reliability 20/20 | 20 consecutive isolated runs | 1 / 2 ran-to-completion runs succeeded, then the operator stopped the job (run 3 cancelled by emergency stop) | **Not met** |

## 3. All real-desktop workflow runs (from audit log)

| UTC time | Workflow | Outcome | Cause |
|---|---|---|---|
| 20:17:29 | probe | CANCELLED | Test B, approval denied (expected) |
| 20:17:50 | probe | CANCELLED | Test C, emergency stop during approval (expected) |
| 20:18:40–41 | probe ×2 | FAILED | Test E, no window / launch denied (expected) |
| 20:22:48 | probe | **COMPLETED** | Test A with approved launch; 1 stale-ref retry recovered |
| 20:23–20:24 | probe ×4 | FAILED | `APP_NOT_FOUND`: probe window had been closed, launch unticked (correct behaviour) |
| 20:29:40 | probe | **COMPLETED** | Demo |
| 20:30:37 | probe | **COMPLETED** | Reliability run 1 |
| 20:31:17 | probe | FAILED | Reliability run 2: `FOREGROUND_LOST`, focus moved away after typing; Enter withheld |
| 20:31:22 | probe | CANCELLED | Reliability run 3: operator emergency stop |
| 20:37:09 | task | FAILED | `FOREGROUND_LOST` at step 3 (operator switched to Chrome); Enter withheld |
| 20:40:43 | task | FAILED | `AMBIGUOUS_ELEMENT`: two `app.py` editor tabs (one restored from previous run) |
| 20:45:43 | task | **COMPLETED** | Script created, opened in editor, run; output matched (sum 5050, 15 primes < 50, fib(20) 6765) |

Unintended failures: **3** (two focus losses caused by the operator using the desktop, one verification bug). No run executed an unapproved command, and no failure left a wrong file.

## 4. Defects found by testing and fixed

1. **Path traversal in the command template.** `C:\a\..\b\run-….txt` matched the probe template. The broker's resolved-path check still blocked it, but the policy layer now rejects `..` itself.
2. **agent-desktop `launch` cannot open VS Code while it is running.** It returns `AMBIGUOUS_TARGET` across Code's 14 processes. The broker now falls back to the official `code.cmd -n`, still approval-gated.
3. **An unexecuted command could stay at the shell prompt after `FOREGROUND_LOST`.** Clearing xterm's hidden textarea does not clear the shell line. Fix: press Escape before typing (PSReadLine reverts the line), and on focus loss kill the terminal through UIA so the line is discarded.
4. **Editor-tab check matched a restored twin tab.** Scripts now get unique names (`app_<id>.py`), and the check uses an exact name.
5. Test-harness fixes: sentinel-trip source was recorded after re-arm; Test F used a launch method that doesn't open windows reliably.

Fixes 2–4 are covered by unit tests. Fixes 3–4 were confirmed in the final real task run. **The real-desktop integration suite has not been rerun since these changes.**

## 5. Performance (real desktop, completed probe runs, n = 3)

| Metric | Values (ms) | Median |
|---|---|---|
| End-to-end, excluding human approval wait | 71 698 (incl. 20 s launch) · 39 975 · 39 098 | **39 975** |
| Window detection | ~386 | — |
| Skeleton snapshot | ~1 400–1 680 | — |
| Terminal init (menu click → terminal present) | ~7 700–10 600 | — |
| Command execution (focus + type + Enter) | ~9 000–10 300 | — |
| Independent file verification | ~107–118 | — |

P95 is not reported because n = 3 is too few. The main cost is agent-desktop itself: each call takes about 1.2–2.5 s, mostly its interaction-lease and auto-wait. A typical run makes about 20 calls.

## 6. Technical findings

- **VS Code accessibility tree is rich and usable.** Menus (`menuitem "Terminal"`), panel tabs, the terminal textarea and editor tabs are all exposed. The semantic menu path Terminal → New Terminal works headless, with no coordinates.
- **Terminal output is not readable via UIA.** The xterm textarea only exposes a status string, so all verification is independent (filesystem and editor tabs).
- **Both VS Code windows share one process (pid 53520).** Targeting must use the window id, and `press` goes to whichever window is in the foreground. This is why the broker re-checks the foreground before typing and before Enter.
- **Headed input is a real limitation.** If the user clicks elsewhere during the 1–2 s typing window, the run fails safely. This caused 2 of the 3 unintended failures.

## 7. Outstanding before Phase 1 can be signed off

1. **Test J:** run Reliability ×20 from the UI, with the desktop left untouched for about 15 minutes.
2. **Test F:** rerun on the real desktop with the `code.cmd` launch.
3. Rerun the full `pytest -m desktop` suite against the current code.
4. Test the hotkey with a physical key press (so far only a synthesized press has been tested).

Reproduce: `.venv\Scripts\python -m pytest tests/unit` · `.venv\Scripts\python -m pytest -m desktop tests/integration -v` (with the probe window open) · `.venv\Scripts\python -m jarvis_desktop.ui.server`.
