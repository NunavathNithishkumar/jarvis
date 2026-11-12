# Phase 2: Security review

Scope: the Phase 2 additions, which are typed chat, LLM intake, OpenJarvis orchestration, the broker tool gateway, the broker action `write_workspace_file`, and UI endpoints. The Phase 1 controls remain unchanged unless stated below.

## 1. Trust boundaries

```
 user (trusted for intent and approvals)
   │ typed text (sanitized + redacted before the model sees it)
   ▼
 LLM: intake / planner / executor / verifier   ◄── UNTRUSTED w.r.t. OS privilege
   │ proposals only: typed tool calls
   ▼
 BrokerGateway   (can refuse: tool allowlist per step, lock, budgets, halts; cannot approve)
   ▼
 Phase 1 Broker  (AUTHORITATIVE: e-stop → gate → context → policy → human approval → audit)
   ▼
 agent-desktop → VS Code              filesystem writes (write_workspace_file only)
```

Untrusted data includes model output, plans, tool results, terminal and program output, file contents, accessibility snapshots, READMEs and web text. Tool output is wrapped as `[UNTRUSTED DATA …]` before it reaches a model. No component reads policy or approval state from model-produced text.

## 2. Threats and controls

| # | Threat | Control | Test |
|---|---|---|---|
| T1 | The model claims or creates approval | Only `ApprovalManager.resolve` (called by the UI endpoint with the per-launch token, or the operator-authorized live approver) can approve. The gateway hook returns False or None and has no approve path. Plan-review approvals are kind `plan_review` and cannot be consumed by any action | `test_plan_review_cannot_authorize_an_action` |
| T2 | A changed command or file is executed under an old approval | The fingerprint covers the action type, the window and all params (command, or path + full content). Approvals are single-use | `test_changed_file_content_needs_fresh_approval`, Phase 1 `test_approval_cannot_be_reused_for_other_command` |
| T3 | Arbitrary code execution through "create + run" | Policy runs an AST content guard *before* the human is asked: import allowlist; `open/exec/eval/compile/__import__/getattr/…` refused; no `_`-prefixed attributes. The human then sees the full content. Run commands are still limited to the Phase 1 `task_run_python` template with path roots | `test_dangerous_python_denied` (20 cases) |
| T4 | Path traversal or overwrite | Rel-path regex in policy. The broker resolves the path, requires `root/task-<id>/file`, refuses symlinks, writes with exclusive create (`open(..., "x")`), and checks existence before asking | `test_bad_paths_denied`, `test_broker_denies_without_approval_and_never_overwrites` |
| T5 | Unrestricted tools exposed | Seven narrow tools, each with `additionalProperties:false` and no command/path/coordinate/key parameters. No OpenJarvis built-in tools are registered (shell_exec, file_write, browser, code_interpreter) | `test_tool_registry_is_exactly_the_narrow_set`, `test_openjarvis_builtin_tools_not_registered` |
| T6 | Planning layer reaches the OS directly | An AST import test ensures `intelligence/`, `orchestration/` and `tools/` never import subprocess, os (except env reads in config), ctypes, shutil, socket or `DesktopAdapter`, and do not use Popen or keybd_event | `test_planning_layer_has_no_os_or_adapter_access` |
| T7 | Prompt injection in output, files or the request | Untrusted wrapping. Per-step tool allowlist: an injected "call run_shell" becomes a governance denial. Policy is static. Intake overwrites `constraints` and `approval_expectations` and drops model notes | `test_prompt_injection_in_program_output_changes_nothing`, `test_injection_in_request_cannot_change_security_fields`, `-m openai` injection test |
| T8 | Focus race or replay | Single desktop lock in the gateway (`DESKTOP_BUSY`), required because `ToolExecutor` cannot kill a timed-out tool. The Phase 1 foreground re-check before Enter is retained, and the terminal is killed if focus is lost. Exact commands are recorded as dispatched and never resent. `FOREGROUND_LOST`, `TIMEOUT` and `KILLED` are non-recoverable, so there is no automatic replan | `test_focus_loss_during_run_no_enter_no_replay`, `test_same_run_command_never_replayed`, `test_gateway_desktop_lock_rejects_overlap` |
| T9 | Ambiguous editor tabs or a stale reference | File names get a task-unique suffix. The tab check requires exactly one selected exact-name tab. Stale references go through the Phase 1 re-locate path | `test_duplicate_editor_tabs_refused`, `test_stale_reference_reobserved` |
| T10 | Runaway loops or cost | `GuardedEngine` enforces budgets for calls, tokens and cost. Wall-clock deadline. `max_replans`, executor `max_turns`, tool-call budget, and the OpenJarvis LoopGuard | `test_budget_exceeded_stops`, `test_verification_failure_is_never_success_and_replans_bounded`, `test_invalid_plans_rejected_and_bounded` |
| T11 | Emergency stop ignored by the LLM layer | `GuardedEngine.generate` raises `LLMHalted` before any call when the e-stop has tripped or the task is cancelled. The gateway checks e-stop and gate before each tool. The broker checks again before each action and between sub-steps. The e-stop handler cancels pending approvals and revokes grants. Re-arming is still required, and completed effects are not undone | `test_emergency_stop_halts_llm_and_broker`, `test_cancel_during_planning_stops_before_execution`, `test_estop_blocks_chat` |
| T12 | Secrets reach the model or logs | `audit.redact` is applied to every outgoing message (key/value patterns, `sk-…`, `ghp_…`, PEM keys). Messages are capped at 16 kB and images are stripped. Snapshots are never sent to a model. The audit log stores file sha256 and size, never content. Timeline args show the length and hash of file content only | `test_secrets_redacted_before_reaching_model`, `test_correlation_chain_and_no_file_content_in_audit` |
| T13 | Model provider switched silently | Only providers the user explicitly chose are allowed: `openrouter` (default, user's choice on 2026-11-13) and `openai`. Anything else is refused. The CloudEngine gets only the chosen provider's client; other keys in the environment are ignored. Note: with OpenRouter the request is forwarded to the upstream model vendor (default Anthropic) under OpenRouter's routing; see residual risk 5. Formerly: `JARVIS_OPENAI_MODEL` required, no default. `_is_openai_model` must hold, or the engine refuses to start. The CloudEngine is built with only an OpenAI client, so other providers' keys in the environment are ignored. `~/.openjarvis/config.toml` (ollama) is never loaded | `test_missing_model_config_fails_clearly` |
| T14 | False success | Success is decided only by the deterministic `task_verifier`: approved-hash integrity of every written file, fresh output, typed criteria, and an anti-hardcoding check (a distinctive expected literal must not appear in the source). The LLM verifier only explains. Live tests check ground truth again, independently | `test_verification_failure_is_never_success…`, `test_hardcoded_answers_not_accepted` |
| T15 | Cross-site requests to the local UI | Unchanged from Phase 1: bound to 127.0.0.1. `/api/chat` requires the per-launch `X-Jarvis-Token` | `test_chat_requires_token` |

## 3. Credentials

Phase 2 adds no credential-entry path. The Phase 1 `CredentialGuard` still blocks all observation and input on an app during a human sign-in, and policy still refuses sensitive elements and protected processes. The OpenAI key is read from the process environment or `HKCU\Environment`. It is passed directly to `openai.OpenAI(api_key=…)`, never logged, and never written to disk by JARVIS. `.env` is in `.gitignore`, and `.env.example` contains placeholders only.

## 4. Residual risks (honest list)

1. **Approved Python still runs with the user's privileges.** The AST guard is conservative but is not a sandbox. CPU-bound abuse is still possible: an infinite loop is limited only by the user stopping it. Phase 3 should run approved scripts in a restricted job object or AppContainer, with a timeout.
2. **Expected values come from the intake model.** If the model computes a wrong expectation, the task fails, which is safe. If the program and the expectation are wrong *the same way*, a false success is possible. The anti-hardcoding check reduces this but does not remove it. The live tests compare against harness-computed ground truth.
3. **The live-test approver is operator pre-authorized automation.** It is enabled only by `JARVIS_LIVE_TESTS_AUTHORIZED=1`, approves only the narrow patterns, and prints everything it approves. It is test tooling, not a product path.
4. **Headed input remains a Phase 1 limitation.** If the user clicks elsewhere during typing, the run fails safely (Enter is withheld and the terminal is killed). It is not retried.
5. **The model provider sees the user's typed request** (redacted) and task-file contents produced in the task. With OpenRouter that is both OpenRouter and the upstream vendor of the chosen model. This is inherent to cloud inference.
