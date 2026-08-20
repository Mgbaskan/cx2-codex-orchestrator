from __future__ import annotations

import ast
from pathlib import Path
import re


CX_HOME = Path.home() / ".cx"
STAGE = CX_HOME / "runtime" / "cx2"

CX_FILE = CX_HOME / "src" / "cx.py"

ADAPTERS = (
    STAGE / "router_adapter.py",
    STAGE / "budget_adapter.py",
    STAGE / "session_adapter.py",
    STAGE / "telemetry_adapter.py",
)


def find_assignment(
    tree: ast.Module,
    name: str,
):
    for node in tree.body:

        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == name
                ):
                    return node

        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            return node

    return None


source = CX_FILE.read_text(
    encoding="utf-8-sig"
)


# =============================================================
# Preconditions
# =============================================================

if not re.search(
    r'''ROUTER_VERSION\s*=\s*["']1\.1\.2["']''',
    source,
):
    raise RuntimeError(
        "Production router 1.1.2 degil."
    )


marker = "CX113_OUTPUT_REPEAT_RULES"

if marker in source:
    raise RuntimeError(
        "1.1.3 repeat-output hotfix zaten mevcut."
    )


tree = ast.parse(
    source,
    filename=str(CX_FILE),
)

assignment = find_assignment(
    tree,
    "OUTPUT_ONLY_WRITE_RULES",
)

if assignment is None:
    raise RuntimeError(
        "OUTPUT_ONLY_WRITE_RULES bulunamadi."
    )

if not isinstance(
    assignment.value,
    (ast.Tuple, ast.List),
):
    raise RuntimeError(
        "OUTPUT_ONLY_WRITE_RULES tuple/list degil."
    )


print(
    "OUTPUT_ONLY_WRITE_RULES:",
    type(assignment.value).__name__,
    f"line={assignment.lineno}",
)


# =============================================================
# Insert only narrow conversation-output patterns.
#
# IMPORTANT:
# Do NOT add a broad:
#
#   cevabi ... tekrar yaz
#
# rule, because:
#
#   README dosyasindaki cevabi tekrar yaz
#
# must remain a mutation.
# =============================================================

new_rules = r'''    # CX113_OUTPUT_REPEAT_RULES
    # Explicit references to a PREVIOUS CHAT ANSWER only.
    r"\b(?:onceki|son)\s+(?:cevabi|yaniti|sonucu)\s+(?:aynen\s+)?tekrar\s+yaz(?:\b|in\b|iniz\b)",
    r"\b(?:bir\s+)?onceki\s+kullanici\s+sorusuna\s+verdigin(?:\s+\w+){0,5}\s+(?:cevabi|yaniti|sonucu)\s+(?:aynen\s+)?tekrar\s+yaz(?:\b|in\b|iniz\b)",
'''


lines = source.splitlines(
    keepends=True
)

closing_line_index = (
    assignment.value.end_lineno - 1
)

source = (
    "".join(
        lines[:closing_line_index]
    )
    + new_rules
    + "".join(
        lines[closing_line_index:]
    )
)


# =============================================================
# Version bump
# =============================================================

source, count = re.subn(
    r'''(ROUTER_VERSION\s*=\s*["'])1\.1\.2(["'])''',
    r"\g<1>1.1.3\2",
    source,
    count=1,
)

if count != 1:
    raise RuntimeError(
        "ROUTER_VERSION bump basarisiz."
    )


# Candidate must compile before write.
compile(
    source,
    str(CX_FILE),
    "exec",
)


# Verify inserted rules are actually part of the assignment.
tree = ast.parse(
    source,
    filename=str(CX_FILE),
)

assignment = find_assignment(
    tree,
    "OUTPUT_ONLY_WRITE_RULES",
)

if assignment is None:
    raise RuntimeError(
        "Final OUTPUT_ONLY_WRITE_RULES bulunamadi."
    )

values = [
    element.value
    for element in assignment.value.elts
    if isinstance(
        element,
        ast.Constant,
    )
    and isinstance(
        element.value,
        str,
    )
]

required_fragments = (
    "(?:onceki|son)",
    "onceki\\s+kullanici",
)

for fragment in required_fragments:
    if not any(
        fragment in value
        for value in values
    ):
        raise RuntimeError(
            f"Inserted rule AST'de yok: {fragment}"
        )


# =============================================================
# Write production
# =============================================================

CX_FILE.write_text(
    source,
    encoding="utf-8",
)


# =============================================================
# Synchronize version guards
# =============================================================

for adapter in ADAPTERS:

    text = adapter.read_text(
        encoding="utf-8-sig"
    )

    old = (
        'EXPECTED_ROUTER_VERSION = "1.1.2"'
    )

    new = (
        'EXPECTED_ROUTER_VERSION = "1.1.3"'
    )

    if old not in text:
        raise RuntimeError(
            f"1.1.2 version guard bulunamadi: {adapter}"
        )

    text = text.replace(
        old,
        new,
        1,
    )

    compile(
        text,
        str(adapter),
        "exec",
    )

    adapter.write_text(
        text,
        encoding="utf-8",
    )


print()
print("Production router : 1.1.3")
print("Router adapter    : 1.1.3")
print("Budget adapter    : 1.1.3")
print("Session adapter   : 1.1.3")
print("Telemetry adapter : 1.1.3")
print("Auth modified     : NO")
print("Policy modified   : NO")
