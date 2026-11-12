"""OpenRouter provider path (user-selected): config, routing into OpenJarvis CloudEngine,
tool-call shape normalization, schema-as-instruction, cost estimate. No network."""
import json

import pytest
from p2harness import P2, AutoApprover, DEFAULT_PLAN, GOOD_SCRIPT, ScriptedEngine, default_responder, text

from jarvis_desktop.intelligence import config as C
from jarvis_desktop.intelligence.config import Budgets, ConfigError, ModelConfig
from jarvis_desktop.intelligence.openai_provider import GuardedEngine, normalize_tool_calls
from jarvis_desktop.intelligence.intake import IntakeService

OR_MODEL = "anthropic/claude-sonnet-5.5"


def env_vars(monkeypatch, **vals):
    monkeypatch.setattr(C, "_user_env", lambda n: vals.get(n))


def test_openrouter_is_default_with_default_model(monkeypatch):
    env_vars(monkeypatch)
    cfg = ModelConfig.from_env()
    assert cfg.provider == "openrouter" and cfg.model == OR_MODEL


def test_model_override_and_unknown_provider(monkeypatch):
    env_vars(monkeypatch, JARVIS_OPENROUTER_MODEL="openai/gpt-5.5")
    assert ModelConfig.from_env().model == "openai/gpt-5.5"
    env_vars(monkeypatch, JARVIS_LLM_PROVIDER="ollama")
    with pytest.raises(ConfigError):
        ModelConfig.from_env()


def test_missing_openrouter_key_fails_clearly(monkeypatch):
    env_vars(monkeypatch)
    with pytest.raises(ConfigError) as e:
        GuardedEngine(ModelConfig.from_env(), Budgets())
    assert "OPENROUTER_API_KEY" in str(e.value)


def test_real_cloud_engine_routes_to_openrouter_client(monkeypatch):
    env_vars(monkeypatch, OPENROUTER_API_KEY="sk-or-test-not-real")
    monkeypatch.setattr("jarvis_desktop.intelligence.openai_provider.fetch_openrouter_pricing", lambda m: (2e-6, 1e-5))
    g = GuardedEngine(ModelConfig("openrouter", OR_MODEL), Budgets())
    from openjarvis.engine.cloud import CloudEngine
    assert isinstance(g.inner, CloudEngine)
    assert g.inner._openrouter_client is not None and g.inner._openai_client is None
    assert str(g.inner._openrouter_client.base_url).startswith("https://openrouter.ai/api/v1")
    assert g.route_model == "openrouter/" + OR_MODEL

    seen = {}

    class FakeCompletions:
        def create(self, **kw):
            seen.update(kw)
            from types import SimpleNamespace as N
            tc = N(id="t1", type="function", function=N(name="submit_plan", arguments='{"a":1}'))
            return N(model=OR_MODEL, usage=N(prompt_tokens=1000, completion_tokens=100, total_tokens=1100),
                     choices=[N(message=N(content="", tool_calls=[tc]), finish_reason="tool_calls")])
    g.inner._openrouter_client.chat = type("X", (), {"completions": FakeCompletions()})()
    from openjarvis.core.types import Message, Role
    res = g.generate([Message(role=Role.USER, content="hi")], model=OR_MODEL)
    assert seen["model"] == OR_MODEL                                  # prefix stripped by OpenJarvis
    assert res["tool_calls"] == [{"id": "t1", "name": "submit_plan", "arguments": '{"a":1}'}]
    assert abs(g.meter.cost_usd - (1000 * 2e-6 + 100 * 1e-5)) < 1e-12   # from the public price list


def test_normalize_keeps_flat_calls():
    flat = {"tool_calls": [{"id": "a", "name": "x", "arguments": "{}"}]}
    assert normalize_tool_calls(flat)["tool_calls"] == flat["tool_calls"]


def _or_engine(responder):
    eng = ScriptedEngine(responder)
    return GuardedEngine(ModelConfig("openrouter", OR_MODEL), Budgets(), inner=eng, sleep=lambda s: None), eng


def test_schema_becomes_instruction_and_intake_still_validates(env):
    task = {"intent_type": "chat", "normalized_objective": "hi", "user_visible_summary": "hi", "assistant_reply": "Hello!"}
    g, eng = _or_engine(lambda role, m, k, e: text("```json\n" + json.dumps(task) + "\n```"))
    r = IntakeService(g, OR_MODEL, workspace_scope="ws").understand("hello")
    assert r.ok and r.intent.assistant_reply == "Hello!"
    call = eng.calls[0]
    assert "response_format" not in call["kwargs"]
    assert "JSON Schema" in call["messages"][-1].content and "intent_type" in call["messages"][-1].content


def test_full_orchestration_with_nested_tool_call_shape(env):
    """Simulates OpenJarvis's OpenRouter output shape end to end: without normalization every
    tool call would arrive with an empty name and the task could not run."""
    base = default_responder()

    def nested(role, messages, kwargs, eng):
        r = base(role, messages, kwargs, eng)
        if r.get("tool_calls"):
            r["tool_calls"] = [{"id": t["id"], "type": "function",
                                "function": {"name": t["name"], "arguments": t["arguments"]}} for t in r["tool_calls"]]
        return r
    p2 = P2(env, nested)
    g, _ = _or_engine(nested)
    p2.orch.engine, p2.orch.model = g, OR_MODEL
    ap = AutoApprover(env)
    r = p2.orch.run(p2.intent())
    ap.stop()
    assert r["outcome"] == "VERIFIED_SUCCESS", r["error"]
    assert all(t["ok"] for t in r["tool_calls"])
