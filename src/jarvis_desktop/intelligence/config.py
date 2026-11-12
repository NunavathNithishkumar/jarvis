"""Model-provider and budget configuration (Phase 2). No secrets are stored here.

The API key is read from the process environment, or else from the Windows
user-level environment (HKCU\\Environment), so a key set with
[Environment]::SetEnvironmentVariable(..., 'User') works without a new shell.
"""
from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass


class ConfigError(Exception):
    """Missing or invalid model configuration. Fails closed: no LLM call is made."""


PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DOTENV_CACHE: dict | None = None


def _dotenv() -> dict:
    """Parse the project's git-ignored .env (KEY=VALUE lines). Values are never logged.
    Placeholder values from .env.example (containing 'REPLACE') are ignored."""
    global _DOTENV_CACHE
    if _DOTENV_CACHE is None:
        path = Path(os.environ.get("JARVIS_ENV_FILE") or PROJECT_ROOT / ".env")
        vals: dict = {}
        try:
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip().removeprefix("export ").strip()
                v = v.split(" #", 1)[0].strip().strip('"').strip("'")
                if k and v and "REPLACE" not in v:
                    vals[k] = v
        except OSError:
            pass
        _DOTENV_CACHE = vals
    return _DOTENV_CACHE


def _user_env(name: str) -> str | None:
    """Lookup order: process environment, project .env, Windows user environment (HKCU)."""
    v = os.environ.get(name)
    if v:
        return v
    v = _dotenv().get(name)
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


# Providers the user has explicitly chosen to allow. OpenRouter was selected by the user
# on 2026-11-13 ("use any model from open router"); it is therefore the default.
PROVIDERS = {
    "openrouter": {"key_env": "OPENROUTER_API_KEY", "model_env": "JARVIS_OPENROUTER_MODEL",
                   "default_model": "anthropic/claude-sonnet-5.5"},
    "openai": {"key_env": "OPENAI_API_KEY", "model_env": "JARVIS_OPENAI_MODEL", "default_model": None},
    # Azure OpenAI, selected by the user on 2026-11-13. "model" is the Azure *deployment* name
    # (the user's resource exposes the deployment "gpt-4.1").
    "azure_openai": {"key_env": "AZURE_OPENAI_API_KEY", "alt_key_env": "AZURE_OPENAI_KEY",
                     "model_env": "AZURE_OPENAI_DEPLOYMENT", "default_model": "gpt-4.1"},
}
DEFAULT_PROVIDER = "openrouter"
AZURE_DEFAULT_API_VERSION = "2024-08-01-preview"


def azure_settings() -> tuple[str, str]:
    """(endpoint, api_version) for Azure OpenAI. Endpoint must be an https *.azure.com host."""
    endpoint = _user_env("AZURE_OPENAI_ENDPOINT")
    if not endpoint:
        raise ConfigError("AZURE_OPENAI_ENDPOINT is not set")
    host = endpoint.split("//", 1)[-1].split("/", 1)[0].lower()
    if not endpoint.lower().startswith("https://") or not (host.endswith(".azure.com")):
        raise ConfigError("AZURE_OPENAI_ENDPOINT must be an https://<resource>...azure.com URL")
    return endpoint, _user_env("AZURE_OPENAI_API_VERSION") or AZURE_DEFAULT_API_VERSION


def get_api_key(provider: str = "openai") -> str:
    env = PROVIDERS[provider]["key_env"]
    key = _user_env(env) or (_user_env(PROVIDERS[provider]["alt_key_env"])
                             if PROVIDERS[provider].get("alt_key_env") else None)
    if not key:
        raise ConfigError(f"{env} is not set (process or Windows user environment). "
                          f"Set it with [Environment]::SetEnvironmentVariable('{env}','<key>','User').")
    bad = [i for i, ch in enumerate(key) if ord(ch) > 126 or ord(ch) < 33]
    if bad:
        # Report positions only, never the key. Typical cause: key retyped from a photo/OCR.
        raise ConfigError(f"{env} contains {len(bad)} invalid (non-ASCII or whitespace) character(s) at "
                          f"position(s) {bad[:5]}; copy the key directly from the provider's portal.")
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
        provider = (_user_env("JARVIS_LLM_PROVIDER") or DEFAULT_PROVIDER).lower()
        if provider not in PROVIDERS:
            raise ConfigError(f"JARVIS_LLM_PROVIDER={provider!r}: supported providers are {sorted(PROVIDERS)}")
        p = PROVIDERS[provider]
        model = _user_env(p["model_env"]) or p["default_model"]
        if not model:
            raise ConfigError(f"{p['model_env']} is not set; choose a model explicitly for provider {provider!r}.")
        return cls(provider, model, timeout_s=_num("JARVIS_LLM_TIMEOUT_S", 60.0 if provider == "openrouter" else 30.0),
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
