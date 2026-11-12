"""Strict, closed schemas for LLM-produced intent (Phase 2).

Everything a model produces is parsed into these models with extra="forbid";
unknown capabilities, check kinds or fields are validation errors, never accepted.
"""
from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IntentType(str, Enum):
    CHAT = "chat"
    QUESTION = "question"
    TASK = "task"
    CLARIFICATION_REQUIRED = "clarification_required"


class Capability(str, Enum):
    """The closed set of things Phase 2 can do on the desktop. Nothing else exists."""
    VSCODE_WORKSPACE = "vscode_workspace"   # find/inspect the VS Code probe window, open a terminal
    CREATE_FILE = "create_file"             # write a new task file (broker-approved)
    OPEN_IN_EDITOR = "open_in_editor"       # open a task file in the VS Code editor
    RUN_PYTHON = "run_python"               # run a task .py file in the VS Code terminal
    VERIFY_OUTPUT = "verify_output"         # independent verification of files/output


SUPPORTED_APPLICATIONS = {"vscode", "vs code", "visual studio code", "code", "python", "terminal",
                          "vscode terminal", "powershell"}


class OutputJsonEquals(Strict):
    """The program prints one JSON object; `key` must equal `value`."""
    kind: Literal["output_json_equals"]
    key: str = Field(min_length=1, max_length=64)
    value: Any


class OutputContains(Strict):
    kind: Literal["output_contains"]
    text: str = Field(min_length=1, max_length=200)


class FileExists(Strict):
    kind: Literal["file_exists"]
    filename: str = Field(pattern=r"^[a-z0-9_]{1,40}\.(py|txt)$")


Check = Annotated[Union[OutputJsonEquals, OutputContains, FileExists], Field(discriminator="kind")]


class Constraints(Strict):
    workspace_scope: str = "probe-workspace task directory"
    risk_level: Literal["low", "medium", "high"] = "low"
    time_limit_s: int = Field(default=600, ge=30, le=1800)
    notes: list[str] = Field(default_factory=list, max_length=10)


class IntentDraft(Strict):
    """What the model is asked to return (the schema sent to OpenAI)."""
    intent_type: IntentType
    normalized_objective: str = Field(max_length=600)
    user_visible_summary: str = Field(max_length=400)
    assistant_reply: str = Field(default="", max_length=1500,
                                 description="Reply text for chat/question, or the clarification message")
    constraints: Constraints = Field(default_factory=Constraints)
    acceptance_criteria: list[Check] = Field(default_factory=list, max_length=20)
    requested_applications: list[str] = Field(default_factory=list, max_length=5)
    required_capabilities: list[Capability] = Field(default_factory=list, max_length=5)
    ambiguities: list[str] = Field(default_factory=list, max_length=10)
    questions_for_user: list[str] = Field(default_factory=list, max_length=5)
    approval_expectations: list[str] = Field(default_factory=list, max_length=10)


class Intent(IntentDraft):
    """Validated intent plus fields the system (not the model) owns."""
    original_request: str
    correlation_id: str
    plan_id: str | None = None
    rejected_reasons: list[str] = Field(default_factory=list)
