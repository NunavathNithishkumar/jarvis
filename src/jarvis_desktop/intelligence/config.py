"""Model-provider and budget configuration (Phase 2). No secrets are stored here.

The API key is read from the process environment, or else from the Windows
user-level environment (HKCU\\Environment), so a key set with
[Environment]::SetEnvironmentVariable(..., 'User') works without a new shell.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(Exception):
    """Missing or invalid model configuration. Fails closed: no LLM call is made."""


def _user_env(name: str) -> str | None:
    v = os.environ.get(name)
    if v:
        return v
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                val, _ = winreg.QueryValueEx(k, name)
                return str(val) if val else None
        except OSError:
            return None
    return None


def get_api_key() -> str:
    key = _user_env("OPENAI_API_KEY")
    if not key:
        raise ConfigError("OPENAI_API_KEY is not set (process or Windows user environment). "
                          "Set it with [Environment]::SetEnvironmentVariable('OPENAI_API_KEY','<key>','User').")
    return key


def _num(name: str, default, cast=float):
    raw = _user_env(name)
    if raw in (None, ""):
        return default
    try:
        return cast(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}")


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    model: str
    timeout_s: float = 30.0
    max_retries: int = 2               # transient errors only
    temperature: float = 0.2
    max_tokens: int = 2048             # per call

    @classmethod
    def from_env(cls) -> "ModelConfig":
        provider = (_user_env("JARVIS_LLM_PROVIDER") or "openai").lower()
        if provider != "openai":
            raise ConfigError(f"JARVIS_LLM_PROVIDER={provider!r}: only 'openai' is supported in Phase 2")
        model = _user_env("JARVIS_OPENAI_MODEL")
        if not model:
            raise ConfigError("JARVIS_OPENAI_MODEL is not set; choose an OpenAI model explicitly "
                              "(no silent default).")
        return cls(provider, model, timeout_s=_num("JARVIS_LLM_TIMEOUT_S", 30.0),
                   max_retries=_num("JARVIS_LLM_MAX_RETRIES", 2, int),
                   max_tokens=_num("JARVIS_LLM_MAX_TOKENS", 2048, int))


@dataclass(frozen=True)
class Budgets:
    """Per-task ceilings. Exceeding any of them stops the run (no silent continuation)."""
    max_llm_calls: int = 40
    max_total_tokens: int = 150_000
    max_cost_usd: float = 1.00
    max_plan_steps: int = 10
    max_replans: int = 2
    max_tool_calls: int = 30
    max_executor_turns: int = 5
    max_wall_s: float = 900.0

    @classmethod
    def from_env(cls) -> "Budgets":
        return cls(max_llm_calls=_num("JARVIS_TASK_MAX_LLM_CALLS", 40, int),
                   max_total_tokens=_num("JARVIS_TASK_MAX_TOKENS", 150_000, int),
                   max_cost_usd=_num("JARVIS_TASK_MAX_COST_USD", 1.00),
                   max_replans=_num("JARVIS_TASK_MAX_REPLANS", 2, int),
                   max_wall_s=_num("JARVIS_TASK_MAX_WALL_S", 900.0))
