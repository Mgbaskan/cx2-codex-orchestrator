from __future__ import annotations

from pathlib import Path
import sys


CX_HOME = Path.home() / ".cx"

sys.path.insert(
    0,
    str(CX_HOME / "src"),
)

sys.path.insert(
    0,
    str(CX_HOME / "runtime" / "cx2"),
)


import cx
import router_adapter
import budget_adapter
import session_adapter
import telemetry_adapter


REPO = Path(
    r"C:\Users\example-user\Projects\sample-app"
).resolve()


assert cx.ROUTER_VERSION == "1.1.3"

assert (
    router_adapter.EXPECTED_ROUTER_VERSION
    == "1.1.3"
)

assert (
    budget_adapter.EXPECTED_ROUTER_VERSION
    == "1.1.3"
)

assert (
    session_adapter.EXPECTED_ROUTER_VERSION
    == "1.1.3"
)

assert (
    telemetry_adapter.EXPECTED_ROUTER_VERSION
    == "1.1.3"
)


policy = cx.load_policy()
repo = cx.detect_repo(REPO)


CASES = [
    # =========================================================
    # CHAT OUTPUT — READ ONLY
    # =========================================================
    (
        "11K-F-original",
        "Bir onceki kullanici sorusuna verdigin "
        "tek kelimelik cevabi aynen tekrar yaz. "
        "Arac kullanma.",
        False,
    ),
    (
        "answer-repeat",
        "Onceki cevabi aynen tekrar yaz.",
        False,
    ),
    (
        "answer-repeat-2",
        "Bana son cevabi tekrar yaz.",
        False,
    ),
    (
        "answer-only",
        "Cevabi yaz.",
        False,
    ),
    (
        "answer-here",
        "Sonucu buraya yaz.",
        False,
    ),
    (
        "11J-original",
        "Bu repoda package.json dosyasi var mi? "
        "Sadece EVET veya HAYIR yaz.",
        False,
    ),
    (
        "yazim-noun",
        "Turkce yazim kurallarini acikla.",
        False,
    ),
    (
        "yazilim-noun",
        "Bu yazilim ne yapiyor?",
        False,
    ),

    # =========================================================
    # REAL MUTATIONS — MUST STAY WORKSPACE-WRITE
    # =========================================================
    (
        "file-repeat",
        "README dosyasina aynen tekrar yaz.",
        True,
    ),
    (
        "file-answer-repeat",
        "README dosyasindaki cevabi aynen tekrar yaz.",
        True,
    ),
    (
        "file-code-repeat",
        "Bu kodu dosyaya tekrar yaz.",
        True,
    ),
    (
        "rewrite-function",
        "Bu fonksiyonu yeniden yaz.",
        True,
    ),
    (
        "rewrite-readme",
        "README dosyasini yeniden yaz.",
        True,
    ),
    (
        "real-code",
        "Bu endpoint icin test kodu yaz.",
        True,
    ),
    (
        "file-result",
        "Sonucu README dosyasina yaz.",
        True,
    ),
    (
        "fix",
        "README dosyasindaki yazim hatasini duzelt.",
        True,
    ),
]


print(
    "=== CX 1.1.3 WRITE-INTENT REGRESSION ==="
)


for (
    label,
    prompt,
    expected_mutating,
) in CASES:

    route = cx.classify(
        prompt,
        repo,
        policy,
    )

    mutating = bool(
        route.get(
            "mutating"
        )
    )

    sandbox = route.get(
        "sandbox"
    )

    expected_sandbox = (
        "workspace-write"
        if expected_mutating
        else "read-only"
    )

    ok = (
        mutating == expected_mutating
        and sandbox == expected_sandbox
    )

    print(
        f"{label:20} "
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
    "ALL REGRESSIONS    : PASS"
)

print(
    "Adapter versions   : PASS"
)

print(
    "Model inference    : ZERO"
)
