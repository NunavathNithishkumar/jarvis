# Phase 2: Integration decisions (verified against installed source)

Written before implementation. It records what was actually found in the installed code, not what a README claims.

## 1. Baseline

| Item | Value |
|---|---|
| Phase 1 baseline commit | `043d064701f18b46ddfc02e5b30d1ec40e8daa81` (master); work happens on `feature/phase2-openjarvis` |
| Phase 1 unit tests before changes | 58 / 58 passed (re-run 2026-11-13) |
| Phase 1 open items (from `PHASE1_TEST_REPORT.md`) | Test J (20× reliability) not met, and real-desktop Test F not verified. The user reports these were done later, but no artifact in the repo shows it |
| Missing docs | `PHASE1_IMPLEMENTATION_REPORT.md` and `PHASE1_DEMO_GUIDE.md` do not exist |

## 2. OpenJarvis: exact revision and the APIs used

- Source: `https://github.com/open-jarvis/OpenJarvis`, checked out locally at `%LOCALAPPDATA%\OpenJarvis\src`.
- Commit `570d75425d50592d931bac7a17ce6b2621561c06` (2026-10-31), package version `1.0.5.dev162+g570d754`. License Apache-2.0.
- The Rust extension `openjarvis_rust` 0.1.0 is mandatory (see `_rust_bridge.py`). It is installed from the locally built wheel `rust/target/wheels/openjarvis_rust-0.1.0-cp311-cp311-win_amd64.whl`, sha256 `8cc0250abeed1d6931b9d2845483127e050f082e4cb521da124176961466497a`.
- It is installed into the project `.venv`, not machine-wide. `openai` is pinned to 2.54.0, the version in OpenJarvis's `uv.lock`. Phase 1 pins (fastapi 0.115.6, uvicorn 0.32.1, PyYAML 6.0.2) are unchanged, and the 58 Phase 1 unit tests still pass after the install.

APIs confirmed by reading the source and importing it at runtime:

| API | Where | How Phase 2 uses it |
|---|---|---|
| `OrchestratorAgent(engine, model, tools=, bus=, max_turns=, temperature=, max_tokens=, mode="function_calling", system_prompt=, parallel_tools=, before_tool_call=)` | `openjarvis/agents/orchestrator.py` | All three roles: planner, executor and verifier. Each runs with `parallel_tools=False`. |
| `before_tool_call(name, args) -> bool` | same | The gateway's pre-check. Returning False injects a governance denial. Invalid arguments or an exception in the hook also deny, so the hook fails closed. |
| `BaseTool`, `ToolSpec`, `ToolExecutor` | `openjarvis/tools/_stubs.py` | Each broker tool is a `BaseTool` subclass. `ToolExecutor` runs them on a bounded thread runner with `spec.timeout_seconds`. **A tool that times out keeps running**, so the gateway takes a desktop lock (see §5). |
| `InferenceEngine` ABC (`generate`, `stream`, `list_models`, `health`) | `openjarvis/engine/_stubs.py` | `GuardedEngine` implements it and wraps `CloudEngine`. |
| `CloudEngine` with `_generate_openai`, and `ResponseFormat(type="json_schema", schema=)` | `openjarvis/engine/cloud.py` | OpenAI chat completions, tool calls and structured output. `_is_openai_model()` is used to refuse models that `CloudEngine` would route to a different provider. |
| `WorkflowGraph`, `WorkflowNode`, `WorkflowEdge`, `NodeType` | `openjarvis/workflow/` | Plan dependency graph: `validate()` (cycles and unknown nodes) and `topological_sort()`. |
| `EventBus`, `EventType` (`AGENT_TURN_*`, `INFERENCE_*`, `TOOL_CALL_*`, `TOOL_TIMEOUT`, `SECURITY_ALERT`, `LOOP_GUARD_TRIGGERED`) | `openjarvis/core/events.py` | Bridged into the audit log and the UI timeline. This is the runtime proof that OpenJarvis ran. |
| `LoopGuard` (created by default in `ToolUsingAgent`) | `openjarvis/agents/loop_guard.py` | Left on. It blocks repeated identical tool calls. |

Not used, and why:
- `JarvisSystem` and the `Jarvis` SDK. They load `~/.openjarvis/config.toml` (here: ollama + qwen), which would silently change the provider. They also start analytics (posthog), memory and telemetry stores.
- `WorkflowEngine.run()`. It runs `execution_stages()` in parallel through a ThreadPoolExecutor, and agent nodes need a `JarvisSystem`. Desktop steps must never run concurrently, so Phase 2 walks `topological_sort()` itself.
- OpenJarvis built-in tools (`shell_exec`, `file_write`, `code_interpreter`, `browser`, …). These are exactly the unrestricted surfaces Phase 2 must not expose. None is registered.

Analytics: the posthog client is created only inside `JarvisSystem`/the SDK path, which Phase 2 never builds. A unit test asserts that no posthog client is created.

## 3. Wiring

```
typed chat -> IntakeService (GuardedEngine -> CloudEngine/OpenAI, json_schema) -> Intent (pydantic)
  chat/question/clarification -> reply only (zero tools)
  task -> Phase2Orchestrator
            planner  OrchestratorAgent [submit_plan]                  -> Plan -> WorkflowGraph.validate/topo
            human plan review (ApprovalManager, informational; not an execution grant)
            per step executor OrchestratorAgent [step tool + read-only tools], before_tool_call = gateway
                 BrokerGateway -> Phase 1 Broker.submit / request_approval / wait_for_approval
                                -> DesktopAdapter (agent-desktop) -> VS Code
            TaskVerifier (deterministic; files + output) ; verifier OrchestratorAgent explains failures only
            bounded replan (<= max_replans) -> final evidence-based summary
```

Broker tools (typed, narrow): `inspect_vscode`, `open_terminal`, `propose_file_creation`, `open_file_in_editor`, `run_python_file`, `read_task_artifact`, `get_task_status`. No shell, generic file, click, type or credential tool exists.

**Typed in-process adapter, not MCP.** OpenJarvis has an MCP client and server. An MCP server for the broker, though, would add a listener and transport authentication without adding isolation: the planner and broker already run in one process. Every tool here is a thin typed shim over `Broker`, and the broker still makes every policy and approval decision. If the planner later moves to a separate process, the same tool schemas can be served over MCP.

## 4. Broker scope expansion (approved by the user)

The `task_create_file` template allows only one line of at most 400 characters with no `'`, `$` or backtick. Phase 2 adds the broker action `write_workspace_file`:
- `approval_required`. It is not tied to a window; it has its own `file_write_rules` in `policy.yaml`.
- Path: `task-<8 hex>\<[a-z0-9_]{1,40}>.(py|txt)`. The resolved path must be inside the probe workspace. `..`, symlinks and overwriting are refused. Maximum size is 8 KB, UTF-8.
- `.py` content goes through an AST guard *in the policy layer*, before any approval is shown. Only allowlisted stdlib imports pass. `open/exec/eval/compile/__import__/getattr/...` are refused, and so is attribute access to `_`-prefixed names. This matters because approved Python is executed.
- The approval shows the full content. The fingerprint covers path and content, so any edit needs a new single-use approval.
- Opening and running files still go through the existing Phase 1 templates `task_open_file` and `task_run_python`, using visible headed terminal input with exact-command, single-use approvals.

## 5. Critical risks and their controls

| Risk | Control |
|---|---|
| A `ToolExecutor` timeout leaves the tool thread running, so a second desktop action could race it | The gateway holds a single desktop lock and a new call gets `DESKTOP_BUSY`. Tool timeouts are set longer than the approval timeout. |
| The LLM invents capabilities or widens scope | Closed enums in pydantic with `extra="forbid"`. `constraints` are overwritten from broker config. The tool allowlist is enforced per role and per step. |
| A plan approval is mistaken for authorization | Plan review is informational only. Every mutating action still needs its own broker approval. |
| Prompt injection in file, terminal or tool output | Tool output is wrapped as UNTRUSTED data. Policy lives in the broker, and no tool can change policy or approvals. |
| Non-idempotent replay | The gateway records dispatched commands and refuses the same run twice. There is no automatic retry after `FOREGROUND_LOST`, `TIMEOUT` or `KILLED`. |
| E-stop during an LLM turn | `GuardedEngine.generate` checks the e-stop and cancel state before every call. The gateway checks again before every tool. The broker checks again before every action. |
| Provider switch or outage | Only OpenAI models are accepted. Retries are bounded (transient errors only). Missing credentials fail clearly. |
| Secrets reach the model or logs | `audit.redact` is applied to every outgoing message. Messages have size caps. Snapshots are never sent to the model. Audit logs store hashes of file content, not the content. |

## 6. Files touched

New: `src/jarvis_desktop/intelligence/`, `orchestration/`, `tools/`, `task_verifier.py`, `content_guard.py`, `tests/phase2/`, `docs/*`, `.env.example`, `requirements-phase2.txt`.
Modified (additive only): `config/policy.yaml`, `policy.py`, `broker.py`, `audit.py` (correlation context), `runtime.py`, `ui/server.py`, `ui/static/index.html`, `pyproject.toml`, `.gitignore`.
