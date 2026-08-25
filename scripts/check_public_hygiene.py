from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    category: str


def _forbidden_literals() -> tuple[tuple[str, str], ...]:
    # Keep the denied values constructible without recommitting the complete
    # private identifiers into the public tree that this script scans.
    return (
        ("private project identifier", "hi" + "brit"),
        ("private project identifier", "sei" + "toon"),
        ("developer username", "muu" + "go"),
        ("private workspace layout", "docker" + "_projects"),
        ("local backup path", ".cx" + "_backup_"),
        ("real workstation path", "C:" + "\\Projects"),
        ("real workstation path", "C:" + "/Projects"),
    )


SYNTHETIC_WINDOWS_USERS = frozenset({"example-user", "test-user", "..."})
WINDOWS_USER_PATH = re.compile(
    r"\b[A-Za-z]:[\\/]+Users[\\/]+([^\\/\s'\":]+)",
    re.IGNORECASE,
)
UNC_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*\Z")
UNC_SHARE = re.compile(r"[A-Za-z0-9][A-Za-z0-9$_.-]*")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PRIVATE_IPV4 = re.compile(
    r"(?<!\d)(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?!\d)"
)
HIGH_CONFIDENCE_SECRETS = (
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
        r"\.[A-Za-z0-9_-]{10,}\b"
    ),
    re.compile(
        r"\b(?:postgres(?:ql)?|mysql|mongodb|redis|amqp)://"
        r"[^\s/:@]+:[^\s@]+@",
        re.IGNORECASE,
    ),
)


def _unc_hosts(line: str) -> list[str]:
    """Return hosts from source-visible UNC path literals."""
    hosts: list[str] = []
    offset = 0
    while True:
        start = line.find("\\\\", offset)
        if start < 0:
            return hosts
        offset = start + 2
        if start and line[start - 1] not in " \t'\"(={":
            continue
        host_end = line.find("\\", offset)
        if host_end < 0:
            continue
        host = line[offset:host_end]
        share = UNC_SHARE.match(line, host_end + 1)
        if UNC_HOST.fullmatch(host) and share:
            hosts.append(host)


def scan_text(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    relative_lower = path.casefold()
    for category, literal in _forbidden_literals():
        if literal.casefold() in relative_lower:
            findings.append(Finding(path, 0, category))

    for line_number, line in enumerate(text.splitlines(), start=1):
        lowered = line.casefold()
        for category, literal in _forbidden_literals():
            if literal.casefold() in lowered:
                findings.append(Finding(path, line_number, category))

        for match in WINDOWS_USER_PATH.finditer(line):
            if match.group(1).casefold() not in SYNTHETIC_WINDOWS_USERS:
                findings.append(Finding(path, line_number, "developer user path"))

        for host in _unc_hosts(line):
            if host.casefold() not in {"example-host", "server"}:
                findings.append(Finding(path, line_number, "private UNC path"))

        if EMAIL.search(line):
            findings.append(Finding(path, line_number, "email address requires review"))
        if PRIVATE_IPV4.search(line):
            findings.append(Finding(path, line_number, "private network address"))
        for pattern in HIGH_CONFIDENCE_SECRETS:
            if pattern.search(line):
                findings.append(Finding(path, line_number, "credential candidate"))

    return findings


def tracked_files() -> list[str]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return sorted(
        item.decode("utf-8", errors="strict")
        for item in completed.stdout.split(b"\0")
        if item
    )


def scan_tracked_tree() -> tuple[list[str], list[Finding]]:
    files = tracked_files()
    findings: list[Finding] = []
    for relative in files:
        data = (ROOT / relative).read_bytes()
        text = data.decode("utf-8", errors="replace")
        findings.extend(scan_text(relative, text))
    return files, findings


def main() -> int:
    try:
        files, findings = scan_tracked_tree()
    except (OSError, subprocess.CalledProcessError, UnicodeError) as exc:
        print(f"Public hygiene check could not scan the tracked tree: {exc}", file=sys.stderr)
        return 2

    if findings:
        print("Public hygiene check failed:", file=sys.stderr)
        for finding in findings:
            location = f"{finding.path}:{finding.line}" if finding.line else finding.path
            print(f"  {location}: {finding.category}", file=sys.stderr)
        return 1

    print(f"Public hygiene check passed ({len(files)} tracked files scanned).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
