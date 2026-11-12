"""GuardedEngine: an OpenJarvis InferenceEngine that wraps OpenJarvis's CloudEngine (OpenAI).

Every model call made in Phase 2 (intake, planner, executor, verifier) goes through
this class. It enforces, before any network traffic:
  * emergency stop / task cancel  -> LLMHalted (no call)
  * per-task budgets (calls, tokens, cost) -> BudgetExceeded (no call)
  * OpenAI-only model routing (CloudEngine would otherwise route claude-*/gemini-* elsewhere)
  * redaction + size caps on every outgoing message (no secrets, no raw snapshots)
and applies an explicit timeout with bounded retries for transient provider errors only.
"""
from __future__ import annotations

import dataclasses
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence

from openjarvis.core.types import Message
from openjarvis.engine._stubs import InferenceEngine

from ..audit import redact
from .config import Budgets, ConfigError, ModelConfig, get_api_key

MAX_MESSAGE_CHARS = 16_000


class LLMHalted(Exception):
    """Emergency stop or cancel was active when a model call was about to be made."""


class BudgetExceeded(Exception):
    pass


class ProviderError(Exception):
    """Provider failure after bounded retries (or a non-retryable error)."""


@dataclasses.dataclass
class UsageMeter:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    retries: int = 0
    failures: int = 0
    log: list = dataclasses.field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def public(self) -> dict:
        return {"calls": self.calls, "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens, "total_tokens": self.total_tokens,
                "cost_usd_estimate": round(self.cost_usd, 6), "retries": self.retries, "failures": self.failures}


def _transient_errors() -> tuple:
    try:
        import openai
        return (openai.APITimeoutError, openai.APIConnectionError, openai.RateLimitError,
                openai.InternalServerError)
    except Exception:  # pragma: no cover
        return (TimeoutError, ConnectionError)


def sanitize_message(m: Message) -> Message:
    content = m.content
    if isinstance(content, str):
        content = redact(content)
        if len(content) > MAX_MESSAGE_CHARS:
            content = content[:MAX_MESSAGE_CHARS] + "\n[...truncated by JARVIS context cap...]"
    return dataclasses.replace(m, content=content, images=None)


class GuardedEngine(InferenceEngine):
    engine_id = "jarvis-guarded-openai"
    is_cloud = True
    _publishes_events = False   # let OpenJarvis agents publish INFERENCE_START/END on the bus

    def __init__(self, cfg: ModelConfig, budgets: Budgets, *, inner: InferenceEngine | None = None,
                 stop_check: Callable[[], Optional[str]] = lambda: None, on_call: Callable[[dict], None] | None = None,
                 sleep=time.sleep):
        self.cfg, self.budgets = cfg, budgets
        self.stop_check = stop_check
        self.meter = UsageMeter()
        self._on_call = on_call or (lambda rec: None)
        self._sleep = sleep
        self._lock = threading.Lock()
        if inner is None:
            inner = self._build_cloud_engine(cfg)
        self.inner = inner

    @staticmethod
    def _build_cloud_engine(cfg: ModelConfig) -> InferenceEngine:
        from openjarvis.engine import cloud as oj_cloud
        if not oj_cloud._is_openai_model(cfg.model):
            raise ConfigError(f"model {cfg.model!r} is not an OpenAI model; CloudEngine would route it to "
                              "another provider. Refusing (no silent provider switch).")
        key = get_api_key()
        import openai
        eng = oj_cloud.CloudEngine.__new__(oj_cloud.CloudEngine)
        _init_cloud_without_env(eng, oj_cloud)
        # Explicit client: our timeout, no SDK-internal retries (we retry transient errors ourselves).
        eng._openai_client = openai.OpenAI(api_key=key, timeout=cfg.timeout_s, max_retries=0)
        return eng

    def reset_meter(self) -> UsageMeter:
        self.meter = UsageMeter()
        return self.meter

    # -- InferenceEngine ---------------------------------------------------------
    def generate(self, messages: Sequence[Message], *, model: str, temperature: float = 0.7,
                 max_tokens: int = 1024, **kwargs: Any) -> Dict[str, Any]:
        reason = self.stop_check()
        if reason:
            raise LLMHalted(reason)
        if model != self.cfg.model:
            raise ConfigError(f"agent requested model {model!r}; only {self.cfg.model!r} is configured")
        b, m = self.budgets, self.meter
        with self._lock:
            if m.calls >= b.max_llm_calls:
                raise BudgetExceeded(f"LLM call budget reached ({b.max_llm_calls})")
            if m.total_tokens >= b.max_total_tokens:
                raise BudgetExceeded(f"token budget reached ({b.max_total_tokens})")
            if m.cost_usd >= b.max_cost_usd:
                raise BudgetExceeded(f"cost budget reached (${b.max_cost_usd:.2f})")
            m.calls += 1
        safe = [sanitize_message(x) for x in messages]
        call_id = "llm-" + uuid.uuid4().hex[:10]
        transient = _transient_errors()
        attempt, t0 = 0, time.perf_counter()
        while True:
            try:
                res = self.inner.generate(safe, model=model, temperature=temperature,
                                          max_tokens=min(max_tokens, self.cfg.max_tokens), **kwargs)
                break
            except transient as e:
                if attempt >= self.cfg.max_retries or self.stop_check():
                    m.failures += 1
                    self._record(call_id, model, t0, attempt, error=f"{type(e).__name__}")
                    raise ProviderError(f"{type(e).__name__} after {attempt + 1} attempt(s)") from e
                attempt += 1
                m.retries += 1
                self._sleep(min(2 ** attempt, 8))
            except (LLMHalted, BudgetExceeded, ConfigError):
                raise
            except Exception as e:
                m.failures += 1
                self._record(call_id, model, t0, attempt, error=f"{type(e).__name__}")
                raise ProviderError(f"{type(e).__name__}: {str(e)[:200]}") from e
        usage = res.get("usage") or {}
        with self._lock:
            m.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            m.completion_tokens += int(usage.get("completion_tokens") or 0)
            m.cost_usd += float(res.get("cost_usd") or 0.0)
        self._record(call_id, model, t0, attempt, usage=usage, cost=res.get("cost_usd"),
                     n_tool_calls=len(res.get("tool_calls") or []))
        res.setdefault("jarvis_call_id", call_id)
        return res

    def _record(self, call_id, model, t0, retries, **extra):
        rec = {"call_id": call_id, "model": model, "retries": retries,
               "duration_ms": round((time.perf_counter() - t0) * 1000, 1), **extra}
        self.meter.log.append(rec)
        try:
            self._on_call(rec)
        except Exception:
            pass

    async def stream(self, messages, *, model, temperature=0.7, max_tokens=1024, **kwargs):  # pragma: no cover
        raise NotImplementedError("streaming is not used in Phase 2")
        yield ""

    def list_models(self) -> List[str]:
        return [self.cfg.model]

    def health(self) -> bool:
        return getattr(self.inner, "_openai_client", True) is not None


def _init_cloud_without_env(eng, oj_cloud) -> None:
    """Initialise CloudEngine's client slots without letting it pick up other providers'
    keys from the environment (e.g. ANTHROPIC_API_KEY). Only the OpenAI client is set."""
    for attr in ("_openai_client", "_anthropic_client", "_google_client", "_openrouter_client",
                 "_atlascloud_client", "_minimax_client", "_deepseek_client", "_codex_client"):
        setattr(eng, attr, None)
    eng._thought_sigs = {}
