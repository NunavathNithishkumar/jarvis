"""Typed task plan + dependency validation through OpenJarvis's WorkflowGraph."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from openjarvis.workflow import WorkflowEdge, WorkflowGraph, WorkflowNode
from openjarvis.workflow.types import NodeType
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..intelligence.schemas import Capability

PLANNABLE_TOOLS = ("inspect_vscode", "open_terminal", "propose_file_creation", "open_file_in_editor",
                   "run_python_file", "read_task_artifact")
CAPABILITY_TOOLS = {Capability.CREATE_FILE: "propose_file_creation", Capability.OPEN_IN_EDITOR: "open_file_in_editor",
                    Capability.RUN_PYTHON: "run_python_file"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanStepDraft(_Strict):
    id: str = Field(pattern=r"^s[0-9]{1,2}$")
    title: str = Field(min_length=3, max_length=120)
    tool: Literal[PLANNABLE_TOOLS]  # type: ignore[valid-type]
    instructions: str = Field(min_length=3, max_length=2000,
                              description="What the computer-execution role must do in this step")
    depends_on: list[str] = Field(default_factory=list, max_length=10)
    success_criteria: str = Field(min_length=3, max_length=400)


class PlanDraft(_Strict):
    summary: str = Field(min_length=3, max_length=400)
    steps: list[PlanStepDraft] = Field(min_length=1, max_length=20)


PLAN_PARAMETERS = PlanDraft.model_json_schema()


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


@dataclass
class StepState:
    draft: PlanStepDraft
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    started_at: float | None = None
    ended_at: float | None = None
    result: dict | None = None
    error: dict | None = None

    def public(self) -> dict:
        return {"id": self.draft.id, "title": self.draft.title, "tool": self.draft.tool,
                "depends_on": self.draft.depends_on, "success_criteria": self.draft.success_criteria,
                "status": self.status.value, "attempts": self.attempts, "error": self.error,
                "duration_ms": round((self.ended_at - self.started_at) * 1000, 1)
                if self.started_at and self.ended_at else None}


@dataclass
class Plan:
    plan_id: str
    draft: PlanDraft
    order: list[str]
    revision: int = 0
    steps: dict[str, StepState] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def ordered(self) -> list[StepState]:
        return [self.steps[i] for i in self.order]

    def public(self) -> dict:
        return {"plan_id": self.plan_id, "revision": self.revision, "summary": self.draft.summary,
                "order": self.order, "steps": [s.public() for s in self.ordered()]}


def to_workflow_graph(draft: PlanDraft, name: str) -> WorkflowGraph:
    g = WorkflowGraph(name=name)
    for s in draft.steps:
        g.add_node(WorkflowNode(id=s.id, node_type=NodeType.TOOL, tools=[s.tool],
                                config={"tool_name": s.tool, "title": s.title}))
    for s in draft.steps:
        for d in s.depends_on:
            g.add_edge(WorkflowEdge(source=d, target=s.id))
    return g


def validate_plan(raw: dict, *, max_steps: int, required: list[Capability]) -> tuple[PlanDraft | None, list[str], list[str]]:
    """Returns (draft, topological_order, problems)."""
    try:
        draft = PlanDraft.model_validate(raw)
    except ValidationError as e:
        return None, [], [f"schema: {err['loc']}: {err['msg']}" for err in e.errors()[:8]]
    problems: list[str] = []
    ids = [s.id for s in draft.steps]
    if len(set(ids)) != len(ids):
        problems.append("duplicate step ids")
    if len(draft.steps) > max_steps:
        problems.append(f"plan has {len(draft.steps)} steps; maximum is {max_steps}")
    for s in draft.steps:
        for d in s.depends_on:
            if d not in ids:
                problems.append(f"step {s.id} depends on unknown step {d}")
            if d == s.id:
                problems.append(f"step {s.id} depends on itself")
    tools = {s.tool for s in draft.steps}
    for cap in required:
        t = CAPABILITY_TOOLS.get(Capability(cap))
        if t and t not in tools:
            problems.append(f"required capability '{Capability(cap).value}' has no '{t}' step")
    if problems:
        return None, [], problems
    try:
        graph = to_workflow_graph(draft, "jarvis-plan")
        ok, msg = graph.validate()   # OpenJarvis: cycle / unknown-node detection
        if not ok:
            return None, [], [f"dependency graph invalid: {msg}"]
        order = graph.topological_sort()
    except Exception as e:  # WorkflowGraph raises on unknown edges
        return None, [], [f"dependency graph invalid: {e}"]
    return draft, order, []
