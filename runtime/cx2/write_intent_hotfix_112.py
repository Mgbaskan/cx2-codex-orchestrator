from __future__ import annotations

import ast
from pathlib import Path
import re


CX_HOME = Path.home() / ".cx"

CX_FILE = (
    CX_HOME
    / "src"
    / "cx.py"
)

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


source = CX_FILE.read_text(
    encoding="utf-8-sig"
)


# =============================================================
# Safety: patch ONLY expected production 1.1.1
# =============================================================

version_patterns = (
    'ROUTER_VERSION = "1.1.1"',
    "ROUTER_VERSION = '1.1.1'",
)

matched_version = next(
    (
        value
        for value in version_patterns
        if value in source
    ),
    None,
)

if matched_version is None:
    raise RuntimeError(
        "Expected ROUTER_VERSION 1.1.1 bulunamadi."
    )


# =============================================================
# Locate old strip_negated_write_phrases function by AST
# =============================================================

tree = ast.parse(
    source,
    filename=str(CX_FILE),
)

target = None

for node in tree.body:

    if (
        isinstance(
            node,
            ast.FunctionDef,
        )
        and node.name
        == "strip_negated_write_phrases"
    ):
        target = node
        break

if target is None:
    raise RuntimeError(
        "strip_negated_write_phrases bulunamadi."
    )


lines = source.splitlines(
    keepends=True
)

start = target.lineno - 1
end = target.end_lineno


new_function = r'''def strip_negated_write_phrases(
    text: str,
) -> str:
    """
    Remove phrases that must not imply repository mutation before
    WRITE_RULES scanning.

    Two cases are filtered:

    1. Explicit negated write instructions such as "dosyaya yazma".
    2. Explicit response-format instructions such as
       "sonucu buraya yaz" or "sadece EVET/HAYIR yaz".

    Other positive repository-write instructions remain visible to
    WRITE_RULES and still select workspace-write.
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


source = (
    "".join(
        lines[:start]
    )
    + new_function
    + "".join(
        lines[end:]
    )
)


# =============================================================
# Insert exact output-only rules immediately before WRITE_RULES
# =============================================================

marker = "WRITE_RULES = ["

position = source.find(
    marker
)

if position < 0:
    raise RuntimeError(
        "WRITE_RULES marker bulunamadi."
    )


constants = r'''# A write verb is not always a repository mutation.
#
# These patterns describe explicit response-format instructions.
# They are removed before WRITE_RULES scanning.
OUTPUT_ONLY_WRITE_RULES = (
    # Turkish: "sadece EVET/HAYIR yaz"
    r"\bsadece\s+(?:evet|hayir)(?:\s+veya\s+(?:evet|hayir))?\s+yaz(?:\b|in\b|iniz\b)",

    # Turkish: "cevabi/sonucu/yaniti yaz"
    r"\b(?:cevabi|yaniti|sonucu)\s+yaz(?:\b|in\b|iniz\b)",

    # Turkish: "sonucu buraya/burada yaz"
    r"\b(?:cevap\w*|yanit\w*|sonuc\w*)\s+(?:buraya|burada|ekrana|sohbete|chatte)\s+yaz(?:\b|in\b|iniz\b)",

    # Turkish: "buraya/burada yaz"
    r"\b(?:buraya|burada|ekrana|sohbete|chatte)\s+yaz(?:\b|in\b|iniz\b)",

    # Turkish: "tek/bir kelime yaz"
    r"\b(?:tek|bir)\s+kelime(?:yle)?\s+yaz(?:\b|in\b|iniz\b)",

    # English response-only forms.
    r"\bwrite\s+(?:only\s+)?(?:yes|no)(?:\s+or\s+(?:yes|no))?\b",
    r"\bwrite\s+(?:the\s+)?(?:answer|result)\s+(?:here|in\s+(?:the\s+)?chat)\b",
)


# Narrow Turkish "yaz" matching to verb forms.
#
# Important:
#   yazim   -> must NOT match
#   yazilim -> must NOT match
#
# while:
#   yaz
#   yazmak
#   yazdir...
#   yazacak...
#   yazin
#   yaziniz
# remain write verbs.
WRITE_VERB_RULE = (
    r"\bwrite\b|"
    r"\byaz(?:"
    r"\b|"
    r"mak\b|"
    r"ma\b|"
    r"may\w*|"
    r"dir\w*|"
    r"acak\w*|"
    r"alim\b|"
    r"in\b|"
    r"iniz\b|"
    r"sin\b|"
    r"siniz\b|"
    r"iyorum\b|"
    r"iyoruz\b|"
    r"ip\b|"
    r"arak\b|"
    r"il(?:sin|acak|mali|mis|di|iyor)\w*"
    r")"
)


'''


source = (
    source[:position]
    + constants
    + source[position:]
)


# =============================================================
# Replace ONLY the old broad write/yaz literal
# =============================================================

old_candidates = (
    r'r"\bwrite\b|\byaz\w*"',
    r"r'\bwrite\b|\byaz\w*'",
    r'"\bwrite\b|\byaz\w*"',
    r"'\bwrite\b|\byaz\w*'",
)

matched_rule = next(
    (
        candidate
        for candidate in old_candidates
        if candidate in source
    ),
    None,
)

if matched_rule is None:
    raise RuntimeError(
        "Old broad write/yaz rule bulunamadi."
    )

source = source.replace(
    matched_rule,
    "WRITE_VERB_RULE",
    1,
)


# =============================================================
# Version bump
# =============================================================

source = source.replace(
    matched_version,
    matched_version.replace(
        "1.1.1",
        "1.1.2",
    ),
    1,
)


# =============================================================
# Verify resulting source BEFORE writing
# =============================================================

compile(
    source,
    str(CX_FILE),
    "exec",
)


if (
    "ROUTER_VERSION"
    not in source
    or "1.1.2"
    not in source
):
    raise RuntimeError(
        "Version bump verification failed."
    )


if matched_rule in source:
    raise RuntimeError(
        "Old broad write/yaz rule halen mevcut."
    )


CX_FILE.write_text(
    source,
    encoding="utf-8",
)


# =============================================================
# CX2 adapters are version-pinned to production router.
# Synchronize guards only; no routing logic copied.
# =============================================================

for file in (
    ROUTER_ADAPTER,
    BUDGET_ADAPTER,
):

    text = file.read_text(
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
            f"Expected version guard bulunamadi: {file}"
        )

    text = text.replace(
        old,
        new,
        1,
    )

    compile(
        text,
        str(file),
        "exec",
    )

    file.write_text(
        text,
        encoding="utf-8",
    )


print(
    "Production router patched : 1.1.2"
)

print(
    "CX2 version guards        : 1.1.2"
)

print(
    "Policy changed            : NO"
)

print(
    "Auth changed              : NO"
)
