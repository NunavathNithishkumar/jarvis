# JARVIS Phase 2: Demo guide (Windows PowerShell)

Phase 2 adds natural-language chat on top of the Phase 1 broker. JARVIS understands the request, plans it with OpenJarvis, and then works through the broker. **You approve every file write, editor-open and run.**

## 1. One-time setup

```powershell
cd C:\Desktop\Jarvis\jarvis-desktop-broker
# Phase 1 environment already exists (.venv, node_modules/agent-desktop 1.2.0).
# Phase 2 dependencies (pinned OpenJarvis commit + local Rust wheel + openai 2.54.0):
$OJ = "$env:LOCALAPPDATA\OpenJarvis\src"          # checkout at 570d75425d50592d931bac7a17ce6b2621561c06
uv pip install --python .venv\Scripts\python.exe "$OJ" "$OJ\rust\target\wheels\openjarvis_rust-0.1.0-cp311-cp311-win_amd64.whl" "openai==2.54.0"

# Model provider: OpenRouter (default). User-level key, never in the repo, never pasted into chats.
[Environment]::SetEnvironmentVariable('OPENROUTER_API_KEY', '<your OpenRouter key>', 'User')
# Optional: any OpenRouter model that supports tool calling (default anthropic/claude-sonnet-5.5)
[Environment]::SetEnvironmentVariable('JARVIS_OPENROUTER_MODEL', 'anthropic/claude-sonnet-5.5', 'User')
# Alternative provider: JARVIS_LLM_PROVIDER=openai with OPENAI_API_KEY + JARVIS_OPENAI_MODEL
```

JARVIS reads these values from `HKCU\Environment` even in shells opened earlier; restart the UI server after changing them. Optional limits are listed in `.env.example`. If the key or model is missing, the chat replies "I can't reach the language model: …" and nothing runs.

## 2. Start

1. Open VS Code on the probe workspace so the window title ends in `probe-workspace - Visual Studio Code`:
   `code -n .\artifacts\probe-workspace`
2. Start the broker UI:
   ```powershell
   .venv\Scripts\python -m jarvis_desktop.ui.server          # opens http://127.0.0.1:8765/
   ```
3. Check that the header shows "agent-desktop 1.2.0 connected" and "E-stop armed".

## 3. Try it (type into **Talk to JARVIS**)

| Type | Expected |
|---|---|
| `How are you?` / `kaise ho?` | A friendly reply. No plan, no desktop action. |
| `VS Code mein kuch bana do` | Clarifying questions. Nothing runs. |
| `Jarvis, VS Code mein ek naya Python project banao. Ek script likho jo 1 se 100 tak sum, 50 se chhote primes, aur fibonacci(20) calculate kare. File open karo, run karo aur output verify karo.` | Rewritten goal and success criteria (5050, 15 primes, 6765), followed by the OpenJarvis plan |
| `Create a word-frequency Python script for this synthetic text: 'the quick brown fox jumps over the lazy dog the fox'. Print the count of every word as JSON under the key word_counts, open the file in VS Code, run it and verify the output.` | A different plan and a different file |

For a task, the approvals arrive in this order:
1. **Review plan.** This is informational only. It lets JARVIS start, but authorizes no action.
2. **Create `<name>_<taskid>.py`.** The full file content is shown. Read it.
3. **Open in editor.** Shows the exact `code -r '…'` command. VS Code visibly opens the file.
4. **Run.** Shows the exact `python '…' | Tee-Object …` command. It is typed visibly into the VS Code terminal.

Then the **Phase 2 plan & live progress** panel shows independent verification (file integrity, fresh output, each expected value, anti-hardcoding) and an evidence-based summary.

**Do not use the mouse or keyboard while JARVIS types into the terminal.** If focus moves away, Enter is withheld, the terminal is killed, and the run stops without retrying.

## 4. Stop controls

- **Pause / Resume / Stop** buttons act on the current Phase 2 task.
- **EMERGENCY STOP**, or `Ctrl+Alt+Shift+F12`, or `New-Item artifacts\EMERGENCY_STOP`. This halts model calls, tools and broker actions, and cancels pending approvals. Then **Re-arm** (delete the sentinel file first if you used it). Completed effects are not undone.
- Denying any approval ends the task as BLOCKED. Nothing further runs.

## 5. Tests

```powershell
.venv\Scripts\python -m pytest tests -q                    # Phase 1 + Phase 2 unit/integration (scripted model, fake desktop)
.venv\Scripts\python -m pytest -m llm tests/phase2/integration -v        # real model (OpenRouter) + real OpenJarvis, fake desktop
$env:JARVIS_LIVE_TESTS_AUTHORIZED = "1"                    # you pre-authorize the narrow test approver
.venv\Scripts\python -m pytest -m "desktop and llm" tests/phase2/live -v -s   # REAL desktop, ~2–4 min per task
.venv\Scripts\python -m pytest -m desktop tests/integration -v           # Phase 1 real-desktop regression
```

Results are written to `artifacts/test_results/` (`phase2-llm-*.json`, `phase2-live-*.json`, junit files).

## 6. Debugging

- Audit log: `artifacts\audit_logs\audit-<date>.jsonl`. Every Phase 2 record carries `correlation_id`, `plan_id`, `step_id` and `role`. OpenJarvis runtime events appear as `agent_or_component = "openjarvis"`. Model calls appear as `action_type = "llm_call"`, with tokens and estimated cost.
  ```powershell
  Get-Content artifacts\audit_logs\audit-*.jsonl | ConvertFrom-Json | ? correlation_id -eq 'corr-…' | ft timestamp,agent_or_component,action_type,status,error_code
  ```
- Task files: `artifacts\probe-workspace\task-<8 hex>\`
- Live state: `GET http://127.0.0.1:8765/api/phase2/state`
- Common failures:
  - `APP_NOT_FOUND`: the probe VS Code window is not open.
  - `AMBIGUOUS_WINDOW`: two probe windows are open; close one.
  - `FOREGROUND_LOST`: the desktop was used during typing.
  - `CONFIG_ERROR`: the key is missing, or the provider/model is not allowed.
