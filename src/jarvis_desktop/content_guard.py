"""Static guard for file content the broker is asked to write (Phase 2).

Approved Python is executed later, so content that could escape the task directory,
touch the OS, the network or the interpreter internals is refused before a human is
even asked. This is a policy check owned by the broker; the LLM cannot change it.
It is deliberately conservative: false refusals are acceptable, false passes are not.
"""
from __future__ import annotations

import ast

DEFAULT_ALLOWED_IMPORTS = frozenset({
    "math", "json", "re", "collections", "itertools", "functools", "statistics", "string", "textwrap",
})
DEFAULT_DENIED_NAMES = frozenset({
    "open", "exec", "eval", "compile", "__import__", "input", "globals", "locals", "vars", "getattr",
    "setattr", "delattr", "breakpoint", "help", "memoryview", "exit", "quit", "__builtins__", "__loader__",
    "__spec__", "classmethod", "staticmethod", "type", "object", "super",
})


def check_python(content: str, allowed_imports=DEFAULT_ALLOWED_IMPORTS,
                 denied_names=DEFAULT_DENIED_NAMES) -> list[str]:
    """Return a list of violations (empty means acceptable)."""
    try:
        tree = ast.parse(content)
    except SyntaxError as e:
        return [f"syntax error: {e.msg} (line {e.lineno})"]
    problems: list[str] = []
    allowed, denied = set(allowed_imports), set(denied_names)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in allowed:
                    problems.append(f"import of '{a.name}' is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "").split(".")[0] not in allowed:
                problems.append(f"import from '{node.module}' is not allowed")
        elif isinstance(node, ast.Name) and node.id in denied:
            problems.append(f"use of '{node.id}' is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            problems.append(f"access to private attribute '{node.attr}' is not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            problems.append("global/nonlocal statements are not allowed")
    return sorted(set(problems))


def check_file(rel_path: str, content: str, *, max_bytes: int, allowed_imports=DEFAULT_ALLOWED_IMPORTS,
               denied_names=DEFAULT_DENIED_NAMES) -> list[str]:
    if not isinstance(content, str):
        return ["content must be text"]
    problems = []
    if "\x00" in content:
        problems.append("NUL bytes are not allowed")
    size = len(content.encode("utf-8"))
    if size == 0:
        problems.append("content is empty")
    if size > max_bytes:
        problems.append(f"content is {size} bytes; limit is {max_bytes}")
    if rel_path.lower().endswith(".py") and not problems:
        problems += check_python(content, allowed_imports, denied_names)
    return problems
