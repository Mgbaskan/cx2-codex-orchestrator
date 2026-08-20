from __future__ import annotations

import ast
from pathlib import Path
import re


CX_HOME = Path.home() / ".cx"

CX_FILE = CX_HOME / "src" / "cx.py"

ROUTER_ADAPTER = (
    CX_HOME
    / "runtime"
    / "cx2"
    / "router_adapter.py"
)

BUDGET_ADAPTER = (
    CX_HOME
    / "runtime"
    / "cx2"
    / "budget_adapter.py"
)


def parse(source: str) -> ast.Module:
    return ast.parse(
        source,
        filename=str(CX_FILE),
    )


def replace_function(
    source: str,
    function_name: str,
    replacement: str,
) -> str:

    tree = parse(source)

    target = None

    for node in tree.body:
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == function_name
        ):
            target = node
            break

    if target is None:
        raise RuntimeError(
            f"Function bulunamadi: {function_name}"
        )

    lines = source.splitlines(
        keepends=True
    )

    return (
        "".join(lines[: target.lineno - 1])
        + replacement
        + "".join(lines[target.end_lineno :])
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


# ============================================================
# Preconditions
# ============================================================

if not re.search(
    r'''ROUTER_VERSION\s*=\s*["']1\.1\.1["']''',
    source,
):
    raise RuntimeError(
        "Production router 1.1.1 degil; patch durduruldu."
    )


if (
    "OUTPUT_ONLY_WRITE_RULES"
    in source
    or "WRITE_VERB_RULE"
    in source
):
    raise RuntimeError(
        "Hotfix sabitleri zaten mevcut."
    )


tree = parse(source)

write_assignment = find_assignment(
    tree,
    "WRITE_RULES",
)

if write_assignment is None:
    raise RuntimeError(
        "WRITE_RULES AST assignment bulunamadi."
    )


print(
    "WRITE_RULES assignment:",
    type(write_assignment.value).__name__,
    f"line={write_assignment.lineno}",
)


# ============================================================
# 1. Replace pre-write scanner
# ============================================================

new_strip_function = r'''def strip_negated_write_phrases(
    text: str,
) -> str:
    """
    Remove phrases that must not imply repository mutation
    before WRITE_RULES scanning.

    Explicit negated mutation instructions and explicit
    chat/output-only write instructions are removed. Any other
    positive mutation instruction remains visible.
    """
    result = text

    for pattern in NEGATED_WRITE_RULES:
        result = re.sub(
            pattern,
            " ",
            result,
            flags=re.IGNORECASE,
        )

    for pattern in OUTPUT_ONLY_WRITE_RULES:
        result = re.sub(
            pattern,
            " ",
            result,
            flags=re.IGNORECASE,
        )

    return result


'''


source = replace_function(
    source,
    "strip_negated_write_phrases",
    new_strip_function,
)


# ============================================================
# 2. Locate WRITE_RULES again after function replacement
# ============================================================

tree = parse(source)

write_assignment = find_assignment(
    tree,
    "WRITE_RULES",
)

if write_assignment is None:
    raise RuntimeError(
        "WRITE_RULES ikinci AST taramasinda bulunamadi."
    )


lines = source.splitlines(
    keepends=True
)


constants = r'''# "write/yaz" can describe the answer format instead of a
# repository mutation. These cases are removed before
# WRITE_RULES scanning.
OUTPUT_ONLY_WRITE_RULES = (
    r"\bsadece\s+(?:evet|hayir)(?:\s+veya\s+(?:evet|hayir))?\s+yaz(?:\b|in\b|iniz\b)",
    r"\b(?:cevabi|yaniti|sonucu)\s+yaz(?:\b|in\b|iniz\b)",
    r"\b(?:cevap\w*|yanit\w*|sonuc\w*)\s+(?:buraya|burada|ekrana|sohbete|chatte)\s+yaz(?:\b|in\b|iniz\b)",
    r"\b(?:buraya|burada|ekrana|sohbete|chatte)\s+yaz(?:\b|in\b|iniz\b)",
    r"\b(?:tek|bir)\s+kelime(?:yle)?\s+yaz(?:\b|in\b|iniz\b)",
    r"\bwrite\s+(?:only\s+)?(?:yes|no)(?:\s+or\s+(?:yes|no))?\b",
    r"\bwrite\s+(?:the\s+)?(?:answer|result)\s+(?:here|in\s+(?:the\s+)?chat)\b",
)


# Common Turkish write-verb forms.
#
# Deliberately does NOT match nouns such as:
#   yazim
#   yazilim
WRITE_VERB_RULE = (
    r"\bwrite\b|"
    r"\byaz\b|"
    r"\byazar\s+misin\b|"
    r"\byazabilir\s+misin\b|"
    r"\byaz(?:in|iniz|sin|siniz|alim)\b|"
    r"\byaz(?:iyor|iyorum|iyoruz|iyorsun|iyorlar)\b|"
    r"\byazdir\w*|"
    r"\byazacak\w*|"
    r"\byazmali\w*|"
    r"\byazip\b|"
    r"\byazarak\b"
)


'''


insert_at = write_assignment.lineno - 1

source = (
    "".join(lines[:insert_at])
    + constants
    + "".join(lines[insert_at:])
)


# ============================================================
# 3. Replace only the old broad WRITE_RULES element via AST
# ============================================================

tree = parse(source)

write_assignment = find_assignment(
    tree,
    "WRITE_RULES",
)

if write_assignment is None:
    raise RuntimeError(
        "WRITE_RULES ucuncu AST taramasinda bulunamadi."
    )


value = write_assignment.value

if not isinstance(
    value,
    (ast.List, ast.Tuple),
):
    raise RuntimeError(
        "WRITE_RULES list/tuple degil: "
        + type(value).__name__
    )


old_value = r"\bwrite\b|\byaz\w*"

target_element = None

for element in value.elts:
    if (
        isinstance(element, ast.Constant)
        and element.value == old_value
    ):
        target_element = element
        break


if target_element is None:
    raise RuntimeError(
        "Eski broad write/yaz regex AST icinde bulunamadi."
    )


segment = ast.get_source_segment(
    source,
    target_element,
)

if not segment:
    raise RuntimeError(
        "WRITE_RULE source segment alinamadi."
    )


print(
    "Old rule source:",
    segment,
)


source = source.replace(
    segment,
    "WRITE_VERB_RULE",
    1,
)


# ============================================================
# 4. Version bump
# ============================================================

source, count = re.subn(
    r'''(ROUTER_VERSION\s*=\s*["'])1\.1\.1(["'])''',
    r"\g<1>1.1.2\2",
    source,
    count=1,
)

if count != 1:
    raise RuntimeError(
        "ROUTER_VERSION bump basarisiz."
    )


# ============================================================
# 5. Candidate verification BEFORE write
# ============================================================

compile(
    source,
    str(CX_FILE),
    "exec",
)


tree = parse(source)

write_assignment = find_assignment(
    tree,
    "WRITE_RULES",
)

if write_assignment is None:
    raise RuntimeError(
        "Final WRITE_RULES bulunamadi."
    )


if old_value in [
    element.value
    for element in write_assignment.value.elts
    if isinstance(element, ast.Constant)
]:
    raise RuntimeError(
        "Eski broad write regex halen WRITE_RULES icinde."
    )


# ============================================================
# 6. Write production source
# ============================================================

CX_FILE.write_text(
    source,
    encoding="utf-8",
)


# ============================================================
# 7. Synchronize CX2 production-version guards
# ============================================================

for adapter in (
    ROUTER_ADAPTER,
    BUDGET_ADAPTER,
):

    text = adapter.read_text(
        encoding="utf-8-sig"
    )

    old = (
        'EXPECTED_ROUTER_VERSION = "1.1.1"'
    )

    new = (
        'EXPECTED_ROUTER_VERSION = "1.1.2"'
    )

    if old not in text:
        raise RuntimeError(
            f"1.1.1 adapter guard bulunamadi: {adapter}"
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
print(
    "Production router : 1.1.2"
)

print(
    "Router adapter    : 1.1.2"
)

print(
    "Budget adapter    : 1.1.2"
)

print(
    "Auth modified     : NO"
)

print(
    "Policy modified   : NO"
)
