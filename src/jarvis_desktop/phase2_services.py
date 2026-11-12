"""Phase 2 service wiring: one GuardedEngine/IntakeService per runtime, one Phase2Run per chat message."""
from __future__ import annotations

import time
import uuid

from .intelligence.config import Budgets, ModelConfig
from .intelligence.intake import IntakeService
from .intelligence.openai_provider import BudgetExceeded, GuardedEngine, LLMHalted, ProviderError
from .intelligence.schemas import IntentType
from .orchestration.openjarvis_runtime import Phase2Orchestrator


class Phase2Services:
    def __init__(self, rt, cfg: ModelConfig | None = None, budgets: Budgets | None = None, engine=None,
                 probe_cfg=None, workspace=None, approval_timeout_s: float | None = None):
        from .runtime import PROBE_WORKSPACE, probe_config
        self.rt = rt
        self.cfg = cfg or ModelConfig.from_env()
        self.budgets = budgets or Budgets.from_env()
        self.engine = engine or GuardedEngine(self.cfg, self.budgets)
        self.engine._on_call = self._on_llm_call   # every model call is audited
        self.probe_cfg = probe_cfg or probe_config(kill_terminal_after=False)
        self.workspace = workspace or PROBE_WORKSPACE
        self.approval_timeout_s = approval_timeout_s or rt.policy.approval_cfg.get("timeout_s", 120)
        self.intake = IntakeService(self.engine, self.cfg.model, workspace_scope=f"{self.workspace}\\task-*",
                                    time_limit_s=int(self.budgets.max_wall_s))

    def _on_llm_call(self, rec: dict) -> None:
        self.rt.audit.record(agent_or_component="llm", tool_name="openai", action_type="llm_call",
                             status="failed" if rec.get("error") else "succeeded",
                             execution_duration_ms=rec.get("duration_ms"), retry_count=rec.get("retries", 0),
                             error_code=rec.get("error"),
                             result_summary={k: rec.get(k) for k in ("call_id", "model", "usage", "cost",
                                                                     "n_tool_calls")})


def get_services(rt) -> Phase2Services:
    if rt.phase2 is None:
        rt.phase2 = Phase2Services(rt)
    return rt.phase2


class Phase2Run:
    """The job's `current` object: exposes .task / .window_id for the existing UI controls."""

    def __init__(self, svc: Phase2Services, rt):
        self.svc, self.rt = svc, rt
        self.orch: Phase2Orchestrator | None = None

    @property
    def task(self):
        return self.orch.session.task if self.orch and self.orch.session else None

    @property
    def window_id(self):
        return self.orch.session.window_id if self.orch and self.orch.session else None

    def handle(self, text: str) -> dict:
        rt, svc = self.rt, self.svc
        cid = "corr-" + uuid.uuid4().hex[:12]
        meter = svc.engine.reset_meter()
        svc.engine.stop_check = lambda: "emergency stop active" if rt.estop.tripped else None
        rt.audit.context = {"correlation_id": cid}
        rt.audit.record(agent_or_component="phase2.intake", action_type="chat_received", status="ok",
                        result_summary={"chars": len(text)})
        t0 = time.perf_counter()
        try:
            res = svc.intake.understand(text, cid)
        except (LLMHalted, BudgetExceeded, ProviderError) as e:
            rt.audit.context = {}
            msg = f"I couldn't process that ({type(e).__name__}: {e}). Nothing was done on your desktop."
            rt.timeline.set(phase="finished", outcome="FAILED", summary=msg, finished_at=time.time())
            return {"status": "FAILED", "outcome": "FAILED", "reply": msg, "correlation_id": cid,
                    "usage": meter.public()}
        intake_ms = round((time.perf_counter() - t0) * 1000, 1)
        if not res.ok:
            rt.audit.record(agent_or_component="phase2.intake", action_type="intent_rejected", status="failed",
                            error_code=res.error["code"], result_summary={"attempts": res.attempts})
            rt.audit.context = {}
            msg = "Sorry, I couldn't understand that reliably, so I did nothing. Could you rephrase?"
            rt.timeline.set(phase="finished", outcome="FAILED", summary=msg, finished_at=time.time())
            return {"status": "FAILED", "outcome": "FAILED", "reply": msg, "error": res.error,
                    "correlation_id": cid, "usage": meter.public()}
        intent = res.intent
        rt.audit.record(agent_or_component="phase2.intake", action_type="intent_classified", status="ok",
                        execution_duration_ms=intake_ms,
                        result_summary={"intent_type": intent.intent_type.value,
                                        "capabilities": [c.value for c in intent.required_capabilities],
                                        "criteria": len(intent.acceptance_criteria),
                                        "rejected_reasons": intent.rejected_reasons})
        rt.timeline.set(intent=intent.model_dump(mode="json"))
        if intent.intent_type != IntentType.TASK:
            reply = intent.assistant_reply or intent.user_visible_summary
            if intent.questions_for_user:
                reply = (reply + "\n" + "\n".join(f"- {q}" for q in intent.questions_for_user)).strip()
            rt.audit.context = {}
            rt.timeline.set(phase="finished", outcome=intent.intent_type.value.upper(), summary=reply,
                            finished_at=time.time(), usage=meter.public())
            return {"status": "COMPLETED", "outcome": intent.intent_type.value.upper(), "reply": reply,
                    "intent_type": intent.intent_type.value, "correlation_id": cid, "usage": meter.public(),
                    "tool_calls": []}
        self.orch = Phase2Orchestrator(rt.broker, svc.engine, svc.cfg.model, budgets=svc.budgets,
                                       probe_cfg=svc.probe_cfg, workspace=svc.workspace, timeline=rt.timeline,
                                       approval_timeout_s=svc.approval_timeout_s)
        rt.timeline.add("status", text=f"Understood: {intent.user_visible_summary}")
        result = self.orch.run(intent)
        result.update(status=result["task_state"], reply=result["summary"], intent_type="task",
                      intake_ms=intake_ms)
        return result
