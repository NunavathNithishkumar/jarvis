# Phase 2: Third-party evaluation

Evidence was collected on 2026-11-13 from each repository's current default branch: GitHub API metadata, the README and the top-level file tree. These were read-only checks. Nothing was installed or run from OpenDots, Open Interpreter or agent-browser, and no conclusion below comes from an older summary. The full source trees were not audited. A deeper review is required before any future integration.

| Repository | Revision inspected | Language / runtime | License | Activity |
|---|---|---|---|---|
| open-jarvis/OpenJarvis | `570d754` (installed); upstream active 2026-11-12 | Python 3.10–3.13 + mandatory Rust extension | Apache-2.0 | active |
| vercel-labs/agent-desktop | 1.2.0 (npm, installed in Phase 1) | Rust CLI | Apache-2.0 | active |
| CopilotKit/OpenDots | `46eaf96` (2026-11-10) | TypeScript web/mobile app (Vite, Docker compose) | MIT | active; README labels it **alpha** and calls it a community-maintained template |
| openinterpreter/openinterpreter | `80d8f55` (2026-11-11) | **Rust**. It is now a fork of OpenAI's Codex CLI (`codex-rs`, Bazel), not the old Python `interpreter` package | Apache-2.0 | active |
| vercel-labs/agent-browser | `1cc1952`, latest release v0.42.0 (2026-11-10) | Rust CLI + background daemon over Chrome DevTools Protocol | Apache-2.0 | active |

## What OpenJarvis plus the Phase 1 broker already cover

- Tool-calling orchestration (`OrchestratorAgent`), a tool registry and executor with timeouts, a fail-closed governance hook, loop guard, event bus, a workflow DAG and an OpenAI engine. All of these are integrated in Phase 2.
- Desktop execution, policy, approvals, audit, e-stop and credential handoff. These come from the Phase 1 broker over agent-desktop.
- A chat UI with a live timeline and approval cards, from the Phase 1 FastAPI UI as extended in Phase 2.

## CopilotKit/OpenDots: **REFERENCE_ONLY**

- **What it is.** A self-hosted template for "always-on AI coworkers". Each "Dot" can have its own computer: a container supervisor with browser, files and shell, and per-Dot permissions. It uses CopilotKit and AG-UI.
- **Service dependency.** The README says conversations require *CopilotKit Intelligence*: either a hosted service, a local Docker evaluation, or a licensed self-hosted deployment. Computer tools need further containerized services, described in `docs/COMPUTERS.md` and the `compose.computers*.yml` files.
- **Patterns worth borrowing.** The human review card shown inline in chat. A live computer view with human takeover. Per-agent tool permissions. Autosave and revision checks on documents. The Phase 2 UI already shows approvals inline, with the exact file content and the plan steps. Human takeover maps onto our existing credential handoff.
- **Why not integrated.**
  - It would add a second conversation backend (a CopilotKit Intelligence service) and container computers with their own shell and browser permissions. Execution would then move out of the native-host broker.
  - It needs Docker and network services the user has not approved.
  - It is alpha, and the README describes it as a starter template.
- **Revisit when.** A multi-user or mobile front end is in scope (Phase 3+). Even then, only its UI patterns should be used, on top of the broker's API.

## openinterpreter/openinterpreter: **REJECTED** as an execution engine; **REFERENCE_ONLY** for ideas

- **What it is now.** A terminal coding agent that the README calls "a fork of OpenAI's Codex", written in Rust. It offers harness emulation, ACP, MCP, the Codex exec protocol and native command sandboxing. It is installed with `irm …/install.ps1 | iex`. Its computer use comes from a QA skill that drives agent-browser or `trycua`.
- **Overlap.** Planning and tool-calling are already provided by OpenJarvis. Desktop and terminal execution are already provided by the broker.
- **Extra privilege surface.**
  - It is a general shell-executing coding agent with its own permission model, so it would be a second autonomous executor.
  - It would bypass broker policy, the exact-command approvals and the audit log.
  - It would compete for the terminal and for foreground focus.
  - Its installer pipes a remote script into PowerShell, which is a machine-level install.
- **Useful ideas.**
  - Its OS-native sandboxing of commands could inform a future hardening of `run_python_file`, for example running approved scripts in a restricted job object.
  - Its portable `AGENTS.md` and skills conventions.
- **Decision.** Not installed, and nothing is vendored. Any future use would have to sit behind the broker as a *single* narrow tool, with its own security review.

## vercel-labs/agent-browser: **DEFERRED** (future broker-governed adapter)

- **What it is.** A Rust CLI from the same family as agent-desktop. It has a snapshot/ref model, semantic locators, fails when a click target is covered, and supports sessions, CDP attach, `confirm`/`deny`, and an MCP server.
- **Fit.** Its design closely matches the existing agent-desktop adapter (`refs`, re-snapshot after change, JSON envelopes). A future `BrowserAdapter` could follow the same pattern as [adapter.py](../src/jarvis_desktop/adapter.py):
  - one module that spawns the pinned binary, with killable subprocesses so the e-stop works
  - broker policy scopes keyed by origin/URL instead of window title
  - deny by default for `eval`, `cookies`, `storage`, `clipboard`, `upload`, `download` and `network`
  - approvals for navigation to new origins and for form submits
  - credential handoff for login pages, reusing `CredentialGuard`
  - a dedicated `--session` profile, never the user's signed-in Chrome profile (so `--auto-connect` is not used)
- **Coexistence with desktop automation.**
  - A single broker owns both adapters and keeps one foreground or focus lock.
  - Headless browser steps can run without focus.
  - Headed or visible browser steps are serialized with desktop steps, using the same `DESKTOP_BUSY` lock in the gateway.
- **Why deferred.** No Phase 2 task needs the web. Adding it now would silently grant network and browser permissions, which the brief explicitly forbids. The planned target is Phase 3, after its own threat model.

## Summary

| Project | Decision |
|---|---|
| OpenJarvis | **INTEGRATED** (mandatory; real runtime, see `phase2_integration_decisions.md`) |
| agent-desktop | **INTEGRATED** (Phase 1 foundation, unchanged) |
| OpenDots | **REFERENCE_ONLY** |
| Open Interpreter | **REJECTED** as an executor / REFERENCE_ONLY for ideas |
| agent-browser | **DEFERRED** to Phase 3 as a broker-governed adapter |
