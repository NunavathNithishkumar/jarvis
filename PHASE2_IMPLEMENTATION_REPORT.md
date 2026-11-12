# JARVIS Phase 2: Implementation report

Date: 2026-11-13 · Branch `feature/phase2-openjarvis` (Phase 1 baseline commit `043d064`) · Windows 11 Home 10.0.26300, Python 3.11.3

## Status: **PARTIAL**

| Gate | Result | Evidence |
|---|---|---|
| (a) Phase 1 regression suite green | **Unit: 58/58 pass**, before and after Phase 2. The real-desktop Phase 1 suite (`-m desktop`) was **not re-run** in this session | `artifacts/test_results/phase1-regression-after-phase2-junit.xml` |
| (b) Real OpenJarvis proven in execution | **Met for the framework.** Real `OrchestratorAgent`, `ToolExecutor`, `WorkflowGraph` and `EventBus` from the pinned commit run in every orchestration test (48 OpenJarvis events in a typical run). **The model was scripted**; no real OpenAI call has been made yet | `test_p2_orchestrator.py`, trace in §6 |
| (c) Two distinct NL workflows on the real desktop, verified | **NOT RUN.** No model API key (`OPENROUTER_API_KEY`) was configured on this machine, and live runs need your authorization | `tests/phase2/live/` (ready) |
| (d) Approvals, e-stop, no unauthorized shell, redaction, injection | **Pass**, with a scripted model and the fake desktop. Not yet repeated on the real desktop | §5 |

What remains: set the key and model, run `-m llm` (real-model intake and planning), then run the live suite on the real desktop with your authorization, then re-run the Phase 1 `-m desktop` suite. Commands are in §8.

## 1. Phase 1 baseline findings (inspected, not assumed)

- The repo had no git history. The baseline was committed as `043d064`, then branched.
- Unit tests: 58/58 passed before any change.
- `PHASE1_TEST_REPORT.md` states that **Test J (20× reliability) is not met** and that **real-desktop Test F is not verified**. The user reports these were completed later, but no artifact in the repo shows it. This report does not count them as done.
- `PHASE1_IMPLEMENTATION_REPORT.md` and `PHASE1_DEMO_GUIDE.md` **do not exist**.

## 2. Architecture as built

```
chat (UI /api/chat, token-auth)
 -> JobManager.start_chat -> Phase2Run
 -> IntakeService ── GuardedEngine ── OpenJarvis CloudEngine (OpenRouter default / OpenAI) ── json_schema -> Intent (pydantic, extra=forbid)
      chat / question / clarification -> text reply, zero tools
      task -> Phase2Orchestrator (OpenJarvis)
           planner   OrchestratorAgent[submit_plan] -> PlanDraft -> WorkflowGraph.validate() + topological_sort()
           plan review (human, informational)
           per step: OrchestratorAgent[step tool + read-only], before_tool_call = BrokerGateway.precheck
                     BrokerGateway (7 typed BaseTools, desktop lock, budgets) -> Phase 1 Broker.submit
                        -> policy -> human approval (exact, single-use) -> agent-desktop / broker file write
           task_verifier (deterministic, filesystem evidence)   ── verifier OrchestratorAgent explains failures
           bounded replan (max_replans=2) -> evidence-based summary
 -> Timeline (/api/phase2/state) + AuditLogger (correlation_id, plan_id, step_id, role on every record)
```

| Module | Purpose |
|---|---|
| [intelligence/config.py](src/jarvis_desktop/intelligence/config.py) | Env and `HKCU\Environment` config. The model is required and there is no provider switch. Budgets |
| [intelligence/openai_provider.py](src/jarvis_desktop/intelligence/openai_provider.py) | `GuardedEngine(InferenceEngine)`: e-stop and cancel check, budgets, OpenAI-only check, redaction, timeout, bounded transient retries, usage meter |
| [intelligence/schemas.py](src/jarvis_desktop/intelligence/schemas.py), [intake.py](src/jarvis_desktop/intelligence/intake.py) | Closed intent schema. Chat, task or clarification routing. System-owned constraints |
| [orchestration/openjarvis_runtime.py](src/jarvis_desktop/orchestration/openjarvis_runtime.py) | Planner, executor and verifier roles on `OrchestratorAgent`. Replan loop. EventBus bridge |
| [orchestration/plan.py](src/jarvis_desktop/orchestration/plan.py), [timeline.py](src/jarvis_desktop/orchestration/timeline.py) | Plan model and `WorkflowGraph` validation. Live UI state |
| [tools/broker_gateway.py](src/jarvis_desktop/tools/broker_gateway.py) | Typed tools: `inspect_vscode`, `open_terminal`, `propose_file_creation`, `open_file_in_editor`, `run_python_file`, `read_task_artifact`, `get_task_status` |
| [task_verifier.py](src/jarvis_desktop/task_verifier.py) | Integrity, freshness, typed criteria, anti-hardcoding |
| [content_guard.py](src/jarvis_desktop/content_guard.py) + `policy.yaml` `file_write_rules` + `Broker._do_write_workspace_file` | Approved scope extension (user-approved): broker-owned task-file write |
| [phase2_services.py](src/jarvis_desktop/phase2_services.py), `runtime.py`, `ui/server.py`, `ui/static/index.html` | Chat job, `/api/chat`, `/api/phase2/state`, chat, plan and timeline panels, approval cards with full file content and plan |

## 3. OpenJarvis: version, imports and runtime calls

- **Version.** `openjarvis 1.0.5.dev162+g570d754` (commit `570d75425d50592d931bac7a17ce6b2621561c06`) and `openjarvis-rust 0.1.0` (local wheel, sha256 `8cc0250a…7a`). Both are installed in the project `.venv`. `openai` is pinned to 2.54.0, the version in OpenJarvis's `uv.lock`. Pins are listed in `requirements-phase2.txt`, and the full freeze is in `artifacts/test_results/phase2-venv-freeze.txt`.
- **Imports.**
  - `openjarvis.agents.orchestrator.OrchestratorAgent`
  - `openjarvis.tools._stubs.{BaseTool, ToolSpec}`
  - `openjarvis.engine._stubs.{InferenceEngine, ResponseFormat}`
  - `openjarvis.engine.cloud.{CloudEngine, _is_openai_model}`
  - `openjarvis.workflow.{WorkflowGraph, WorkflowNode, WorkflowEdge}`
  - `openjarvis.core.events.{EventBus, EventType}`
  - `openjarvis.core.types.{Message, Role, ToolResult}`
- **Runtime calls.**
  - `OrchestratorAgent(engine, model, tools=…, bus=…, mode="function_calling", parallel_tools=False, before_tool_call=…, max_turns=…).run(text)`
  - `WorkflowGraph.validate()`, `topological_sort()`
  - `EventBus.subscribe(...)`
  - `CloudEngine._generate_openrouter` (default) or `_generate_openai`, reached through `GuardedEngine`
- **Not used.**
  - `JarvisSystem` and the SDK. These would load `~/.openjarvis` (ollama/qwen) and start analytics. A test confirms no posthog client is created.
  - `WorkflowEngine.run`, which executes parallel stages.
  - All OpenJarvis built-in tools.

## 4. Model details

- **Provider.** OpenRouter, chosen explicitly by the user ("use any model from open router"), with OpenAI selectable via `JARVIS_LLM_PROVIDER=openai`. Both go through OpenJarvis `CloudEngine` with an explicit client (OpenRouter base URL, timeout 60 s, `max_retries=0`). Two adapter fixes for OpenJarvis's OpenRouter path at `570d754`:
  - Its nested tool-call shape (`function.name`) is flattened to the `name`/`arguments` shape `OrchestratorAgent` reads. Without this every tool call arrives unnamed.
  - It drops `response_format`, so the JSON schema is stated in the prompt and the output is still strictly validated with a bounded re-ask.
  - Cost is estimated from OpenRouter's public price list.
  - Tested in `test_p2_openrouter.py`. JARVIS does its own retries: two, for transient errors only.
- **Model.** `JARVIS_OPENROUTER_MODEL`, default `anthropic/claude-sonnet-5.5` (supports tools and structured outputs; $2 / $10 per M input/output tokens per OpenRouter's list on 2026-11-13). **No key is set on this machine, so no real model was called. Latency, tokens and cost are unmeasured.** Each run records them (`llm_call` audit records; `usage` in results and the UI).
- **Budgets per task.** 40 calls, 150k tokens, $1.00 estimated, 2 replans, 30 tool calls, 900 s wall time. All are configurable.
- **Expected call count.** For the 5-step demo with a scripted model: 1 intake call, plus 2 planner calls, plus about 2 per step, for **12 calls** in total. A real model may need more (bounded by the budgets).

## 5. Tests

### Before and after

| Suite | Before Phase 2 | After Phase 2 |
|---|---|---|
| Phase 1 unit (`tests/unit`) | 58/58 | **58/58** |
| Phase 2 (`tests/phase2`, default markers) | none | **89/89** (incl. 7 OpenRouter-path tests) |
| Default run, total | 58 | **147 passed, 15 deselected** (desktop/llm opt-in) |

`artifacts/test_results/phase2-default-junit.xml` and `phase2-results-summary.json`.

### Evidence level per required test

Evidence levels:
- **S** = scripted model + real OpenJarvis + Phase 1 FakeAdapter
- **P** = policy or broker unit test
- **R** = real OpenAI
- **D** = real desktop

| # | Requirement | Test(s) | Level | Result |
|---|---|---|---|---|
| 1 | Chat: no tools | `test_chat_no_tools`, `test_chat_message_answered_without_desktop` | S | pass |
| 2 | Ambiguous request: clarification, zero side effects | `test_ambiguous_task_clarification_zero_side_effects`, `test_unsupported_app_forces_clarification` | S | pass |
| 3 | Hinglish request: structured intent | `test_hinglish_task_structured_and_system_owns_constraints` (canned output); `test_real_hinglish_task` | S / R | pass / **not run** |
| 4 | Schema-invalid output: bounded retry, no execution | `test_invented_capability_rejected_then_retry_bounded`, `test_invalid_then_valid_recovers`, `test_extra_fields_rejected` | S | pass |
| 5 | Real OpenJarvis initialization and orchestration | every `test_p2_orchestrator` test; `test_planner_agent_is_real_openjarvis_class` | S (framework real) | pass |
| 6 | Plan, dependencies, state transitions | `test_full_flow…`, `test_invalid_plans_rejected_and_bounded` | S | pass |
| 7 | Tool registration and routing | `test_tool_registry_is_exactly_the_narrow_set`, `test_executor_cannot_use_tool_outside_its_step` | P/S | pass |
| 8 | No direct OS or shell access from the planner | `test_planning_layer_has_no_os_or_adapter_access` (AST), `test_openjarvis_builtin_tools_not_registered` | P | pass |
| 9 | Approval denied: no side effect | `test_file_approval_denied_no_side_effects`, `test_plan_review_denied_executes_nothing`, `test_deny_via_http_blocks` | S | pass |
| 10 | Changed command or content: fresh approval | `test_changed_file_content_needs_fresh_approval`, `test_same_run_command_never_replayed`, Phase 1 approval tests | P/S | pass |
| 11 | Prompt injection: no policy override | `test_prompt_injection_in_program_output_changes_nothing`, `test_injection_in_request…`; `test_real_prompt_injection…` | S / R | pass / **not run** |
| 12 | E-stop: no further actions | `test_emergency_stop_halts_llm_and_broker`, `test_cancel_during_planning…`, `test_estop_blocks_chat` | S | pass |
| 13 | Focus loss: no delayed Enter | `test_focus_loss_during_run_no_enter_no_replay` | S | pass |
| 14 | Duplicate tabs | `test_duplicate_editor_tabs_refused` | S | pass |
| 15 | Stale reference | `test_stale_reference_reobserved` | S | pass |
| 16 | Timeout, provider outage, replan bound | `test_gateway_desktop_lock_rejects_overlap`, `test_provider_outage_bounded_retries_no_side_effects` (3 attempts, 0 desktop actions), `test_budget_exceeded_stops`, `test_verification_failure…replans_bounded` | S | pass |
| 17 | Verification failure is never reported as success | `test_verification_failure_is_never_success…`, `test_hardcoded_answers_not_accepted` | S | pass |
| 18 | Correlation chain and redaction | `test_correlation_chain_and_no_file_content_in_audit`, `test_secrets_redacted_before_reaching_model`, `test_task_via_chat_end_to_end_with_http_approvals` | S | pass |
| 19 | Real NL-to-Windows task, independently verified | `test_19_hinglish_sum_primes_fib_on_real_desktop` | R+D | **NOT RUN** |
| 20 | A second, different task with a different plan | `test_20_word_frequency…` (D); `test_different_request_different_plan` (S); `test_real_model_orchestration_two_tasks_different_plans` (R) | S / R / D | S pass; R and D **not run** |
| 21 | Several consecutive live runs | `test_21_consecutive_live_runs` (`JARVIS_LIVE_RUNS`, default 3) | R+D | **NOT RUN** |

## 6. Trace excerpt

From a scripted-model run of the full flow, built from the audit log. Correlation fields are present on every line.

```
role                step  component               action                    status
planner             -     openjarvis              oj/tool_call_start        observed   (submit_plan)
planner             -     phase2.planner          plan_created              ok
planner             -     approvals               approval_resolved         approved   (plan review)
computer_execution  s1    openjarvis              oj/tool_call_start        observed   (inspect_vscode)
computer_execution  s1    phase2.broker_gateway   tool/inspect_vscode       succeeded
computer_execution  s3    approvals               approval_requested        pending    (file + full content shown)
computer_execution  s3    phase2.broker_gateway   write_workspace_file      succeeded  approval=approved
computer_execution  s4    phase2.broker_gateway   terminal_command          succeeded  approval=approved (code -r)
computer_execution  s5    phase2.broker_gateway   terminal_command          succeeded  approval=approved (python | Tee-Object)
verification        -     verification            verify_task               succeeded
-                   -     phase2.orchestrator     phase2_task_finished      VERIFIED_SUCCESS
usage: 12 model calls, 1080 tokens (scripted); 48 OpenJarvis events
```

Audit records hold file sha256 and size. File content and secrets never appear (tested).

## 7. Failures found and fixed during Phase 2 development

1. **A duplicated timeline keyword caused phantom tool failures.** Each successful tool call was followed by a second record marked `INVALID_ARGUMENTS`, because a broad `except TypeError` wrapped the logging code. Fixes:
   - Arguments are now validated with `inspect.signature().bind()` *before* dispatch.
   - Logging moved out of the error path.
   - The success test now asserts the exact tool sequence and that every call succeeded.
2. **The two `conftest.py` files collided** in pytest's module cache. Shared helpers moved to `tests/phase2/p2harness.py`.
3. **The test-injected engine skipped the `llm_call` audit hook.** `Phase2Services` now always attaches it.
4. **The opt-in suite wrote an empty results file when skipped.** It now writes results only when something ran.

## 8. To finish Phase 2 (what you need to do)

```powershell
[Environment]::SetEnvironmentVariable('OPENROUTER_API_KEY','<key>','User')   # model defaults to anthropic/claude-sonnet-5.5
cd C:\Desktop\Jarvis\jarvis-desktop-broker
.venv\Scripts\python -m pytest -m llm tests/phase2/integration -v
code -n .\artifacts\probe-workspace            # probe window open, then leave the desktop alone
$env:JARVIS_LIVE_TESTS_AUTHORIZED = "1"; $env:JARVIS_LIVE_RUNS = "3"
.venv\Scripts\python -m pytest -m "desktop and llm" tests/phase2/live -v -s
.venv\Scripts\python -m pytest -m desktop tests/integration -v
```

Or run the demo interactively: `PHASE2_DEMO_GUIDE.md`.

## 9. Known limitations and remaining risks

See `docs/phase2_security_review.md` §4 for details. In short:
- Approved Python runs unsandboxed. The AST guard is not a sandbox.
- Expected values come from the intake model.
- Headed terminal input means the desktop must be left alone during a run.
- Terminal output is not readable through UIA, so output is captured through `Tee-Object` into a file.
- Model quality, latency and cost are unmeasured until a key is configured.

## 10. Phase 3 candidates

- Sandboxed script execution (job object or AppContainer, with a timeout).
- A broker-governed agent-browser adapter (`docs/third_party_evaluation.md`).
- Voice input through the `start_chat` entry point (STT, TTS and wake word are deferred).
- Phase 1 Test J (20× reliability) and real-desktop Test F.
- Plan-level batching of approvals with exact-command lists. `ApprovalManager.request_plan` already exists.
