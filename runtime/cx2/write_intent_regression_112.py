from __future__ import annotations

from pathlib import Path
import sys


CX_HOME = Path.home() / ".cx"
sys.path.insert(
    0,
    str(CX_HOME / "src"),
)

import cx


REPO = Path(
    r"C:\Users\example-user\Projects\sample-app"
).resolve()


if cx.ROUTER_VERSION != "1.1.2":
    raise RuntimeError(
        f"Router version mismatch: {cx.ROUTER_VERSION}"
    )


policy = cx.load_policy()
repo = cx.detect_repo(REPO)


CASES = [
    # READ
    (
        "11J-original",
        "Bu repoda package.json dosyasi var mi? "
        "Sadece EVET veya HAYIR yaz.",
        "read-only",
        False,
    ),
    (
        "answer-here",
        "Sonucu buraya yaz.",
        "read-only",
        False,
    ),
    (
        "answer-direct",
        "Cevabi yaz.",
        "read-only",
        False,
    ),
    (
        "answer-negated",
        "Cevabi burada yaz ve dosyalari degistirme.",
        "read-only",
        False,
    ),
    (
        "plain-read",
        "Bu repoda package.json dosyasi var mi?",
        "read-only",
        False,
    ),
    (
        "yazim-noun",
        "Turkce yazim kurallarini acikla.",
        "read-only",
        False,
    ),
    (
        "yazilim-noun",
        "Bu yazilim ne yapiyor?",
        "read-only",
        False,
    ),
    (
        "negated",
        "Dosyalara yazma, sadece incele.",
        "read-only",
        False,
    ),

    # WRITE
    (
        "file-write",
        "README dosyasina bir aciklama yaz.",
        "workspace-write",
        True,
    ),
    (
        "code-write",
        "Bu endpoint icin test kodu yaz.",
        "workspace-write",
        True,
    ),
    (
        "file-result",
        "Sonucu README dosyasina yaz.",
        "workspace-write",
        True,
    ),
    (
        "fix",
        "README dosyasindaki yazim hatasini duzelt.",
        "workspace-write",
        True,
    ),
    (
        "mixed",
        "Cevabi buraya yaz ve README dosyasini duzelt.",
        "workspace-write",
        True,
    ),
]


print(
    "=== CX 1.1.2 WRITE INTENT REGRESSION ==="
)


for (
    label,
    prompt,
    expected_sandbox,
    expected_mutating,
) in CASES:

    route = cx.classify(
        prompt,
        repo,
        policy,
    )

    sandbox = route.get(
        "sandbox"
    )

    mutating = bool(
        route.get(
            "mutating"
        )
    )

    ok = (
        sandbox == expected_sandbox
        and mutating == expected_mutating
    )

    print(
        f"{label:16} "
        f"sandbox={sandbox:15} "
        f"mutating={str(mutating):5} "
        f"{'PASS' if ok else 'FAIL'}"
    )

    if not ok:
        raise RuntimeError(
            f"Regression failed: {label}"
        )


print()
print(
    "ALL REGRESSIONS : PASS"
)

print(
    "MODEL INFERENCE : ZERO"
)
