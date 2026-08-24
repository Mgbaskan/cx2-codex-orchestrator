from __future__ import annotations

"""
CX2 2.0.10 Prompt Transport Layer.

Responsible solely for prompt ingestion, decoding, size validation,
UTF-8/BOM handling, and multiline capture.

Zero router logic, zero model selection logic, zero verification semantics.
"""

from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any, Callable

# CX_HOME and import bootstrap
CX_HOME = Path.home() / ".cx"
PRODUCTION_SRC = CX_HOME / "src"

for candidate in (
    str(PRODUCTION_SRC),
    str(Path(__file__).resolve().parent.parent.parent / "src"),
):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from cx import (  # type: ignore[import-untyped]
    InvalidUnicodeInputError,
    normalize_external_text,
)

# 1 MiB upper bound to protect against CLI misuse and accidental huge binary/log ingestion.
# This is NOT a claim about Codex App Server model context limit.
MAX_PROMPT_BYTES: int = 1 * 1024 * 1024


class PromptTransportError(ValueError):
    """Raised when prompt ingestion or transport validation fails."""
    pass


def validate_prompt_bytes(
    data: bytes,
    source_name: str = "prompt",
) -> str:
    """
    Validate and decode raw prompt bytes according to CX2 transport contracts.

    Invariants:
    1. Size must not exceed MAX_PROMPT_BYTES (1 MiB).
    2. Must not contain NUL bytes (rejects binary files).
    3. UTF-8 BOM is stripped if present.
    4. Decodes with strict UTF-8 (no silent mojibake or lossy substitution).
    5. Validates Unicode with normalize_external_text().
    6. Must contain non-whitespace text.
    7. Exact content, indentation, blank lines, and line endings are preserved.
    """
    if not isinstance(data, bytes):
        raise PromptTransportError(f"{source_name} beklenen bytes türünde değil.")

    actual_size = len(data)
    if actual_size > MAX_PROMPT_BYTES:
        raise PromptTransportError(
            f"Prompt boyutu izin verilen sınırı aşıyor: {actual_size} bayt "
            f"(maksimum: {MAX_PROMPT_BYTES} bayt / {MAX_PROMPT_BYTES // (1024 * 1024)} MiB)."
        )

    if b"\x00" in data:
        raise PromptTransportError(
            f"{source_name} binary veri (NUL bayt) içeriyor; metin prompt olarak kabul edilmez."
        )

    # Strip UTF-8 BOM if present
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]

    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PromptTransportError(
            f"{source_name} geçerli bir UTF-8 metni değil: {exc.reason} (pozisyon {exc.start})."
        ) from exc

    try:
        normalized = normalize_external_text(decoded)
    except InvalidUnicodeInputError as exc:
        raise PromptTransportError(
            f"{source_name} geçersiz Unicode karakterleri içeriyor; metin kayıpsız çözülemedi."
        ) from exc

    if not normalized.strip():
        raise PromptTransportError(
            f"{source_name} boş veya yalnızca boşluk karakterlerinden oluşuyor."
        )

    return normalized


def validate_prompt_text(
    text: str,
    source_name: str = "prompt",
) -> str:
    """
    Validate an in-memory prompt text string.
    """
    if not isinstance(text, str):
        raise PromptTransportError(f"{source_name} beklenen str türünde değil.")

    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PromptTransportError(
            f"{source_name} UTF-8 olarak kodlanamadı: {exc}."
        ) from exc

    actual_size = len(encoded)
    if actual_size > MAX_PROMPT_BYTES:
        raise PromptTransportError(
            f"Prompt boyutu izin verilen sınırı aşıyor: {actual_size} bayt "
            f"(maksimum: {MAX_PROMPT_BYTES} bayt / {MAX_PROMPT_BYTES // (1024 * 1024)} MiB)."
        )

    if "\x00" in text:
        raise PromptTransportError(
            f"{source_name} binary karakter (NUL) içeriyor; metin prompt olarak kabul edilmez."
        )

    try:
        normalized = normalize_external_text(text)
    except InvalidUnicodeInputError as exc:
        raise PromptTransportError(
            f"{source_name} geçersiz Unicode karakterleri içeriyor; metin kayıpsız çözülemedi."
        ) from exc

    if not normalized.strip():
        raise PromptTransportError(
            f"{source_name} boş veya yalnızca boşluk karakterlerinden oluşuyor."
        )

    return normalized


def read_prompt_file(
    path: str | Path,
    cwd: Path | None = None,
) -> str:
    """
    Read and validate a UTF-8 prompt file from local filesystem.
    """
    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise PromptTransportError("Geçersiz veya boş dosya yolu.")

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        base = cwd or Path.cwd()
        candidate = base / candidate

    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        raise PromptTransportError(f"Prompt dosyası bulunamadı: {path}")
    except PermissionError:
        raise PromptTransportError(f"Prompt dosyasına erişim engellendi: {path}")
    except OSError as exc:
        raise PromptTransportError(f"Prompt dosyası çözülemedi: {path} ({exc})")

    if not resolved.is_file():
        raise PromptTransportError(f"Prompt yolu bir dosya değil: {path}")

    try:
        raw_bytes = resolved.read_bytes()
    except PermissionError:
        raise PromptTransportError(f"Prompt dosyası okunamadı (erişim engellendi): {path}")
    except OSError as exc:
        raise PromptTransportError(f"Prompt dosyası okunamadı: {path} ({exc})")

    return validate_prompt_bytes(raw_bytes, source_name=f"'{path}' dosyası")


def read_prompt_stdin(
    is_tty_error: bool = True,
) -> str:
    """
    Read and validate prompt text from sys.stdin.buffer.
    """
    if is_tty_error and sys.stdin.isatty():
        raise PromptTransportError(
            "--stdin kullanımı piped veya yönlendirilmiş girdi gerektirir (interaktif giriş için /paste kullanın)."
        )

    try:
        buffer_obj = getattr(sys.stdin, "buffer", sys.stdin)
        if hasattr(buffer_obj, "read"):
            raw_input = buffer_obj.read()
            if isinstance(raw_input, str):
                raw_bytes = raw_input.encode("utf-8", errors="surrogateescape")
            else:
                raw_bytes = raw_input
        else:
            raw_bytes = sys.stdin.read().encode("utf-8", errors="surrogateescape")
    except Exception as exc:
        raise PromptTransportError(f"stdin okunurken hata oluştu: {exc}")

    return validate_prompt_bytes(raw_bytes, source_name="stdin")


def capture_multiline_paste(
    input_func: Callable[[str], str] = input,
    print_func: Callable[..., None] = print,
) -> str | None:
    """
    Interactively capture a multiline prompt until .send or .cancel.

    Escape rules:
    - Single line '.send' triggers submission.
    - Single line '.cancel' cancels without running.
    - Single line '..send' is unescaped to literal '.send'.
    - Single line '..cancel' is unescaped to literal '.cancel'.
    - All other lines are preserved as-is.
    """
    print_func("[cx] Çok satırlı giriş modu.")
    print_func("[cx] Göndermek için tek satırda .send")
    print_func("[cx] İptal etmek için tek satırda .cancel")

    lines: list[str] = []
    try:
        while True:
            try:
                line = input_func("paste> ")
            except (EOFError, KeyboardInterrupt):
                print_func("\n[cx] Çok satırlı giriş iptal edildi.")
                return None

            trimmed = line.strip()
            if trimmed == ".send":
                break
            if trimmed == ".cancel":
                print_func("[cx] Çok satırlı giriş iptal edildi.")
                return None

            # Handle escaping
            if trimmed == "..send":
                line = line.replace("..send", ".send", 1)
            elif trimmed == "..cancel":
                line = line.replace("..cancel", ".cancel", 1)

            lines.append(line)

    except Exception:
        print_func("\n[cx] Çok satırlı giriş iptal edildi.")
        return None

    if not lines:
        print_func("[cx] Boş prompt; model turnü başlatılmadı.")
        return None

    full_text = "\n".join(lines)
    if not full_text.strip():
        print_func("[cx] Boş prompt; model turnü başlatılmadı.")
        return None

    try:
        return validate_prompt_text(full_text, source_name="multiline paste")
    except PromptTransportError as exc:
        print_func(f"[cx] Prompt hatası: {exc}")
        return None


@dataclass
class ResolvedPromptSource:
    prompt: str | None
    is_route_only: bool
    source_kind: str  # "positional" | "prompt-file" | "stdin" | "route" | "route-file" | "interactive"
    source_path: str | None = None


def resolve_prompt_source(
    args: Any,
    cwd: Path,
) -> ResolvedPromptSource:
    """
    Deterministically resolve primary prompt text and detect conflicting sources.
    """
    raw_positional = " ".join(getattr(args, "prompt", []) or []).strip()
    prompt_file = getattr(args, "prompt_file", None)
    use_stdin = bool(getattr(args, "stdin", False))
    route_text = getattr(args, "route", None)
    route_file = getattr(args, "route_file", None)

    # 1. Route conflicts
    if route_text is not None and route_file is not None:
        raise PromptTransportError("Hem --route hem --route-file aynı anda verilemez.")

    is_route_only = route_text is not None or route_file is not None

    if is_route_only:
        if raw_positional:
            raise PromptTransportError(
                "Route önizleme argümanları (--route, --route-file) ile positional prompt aynı anda verilemez."
            )
        if prompt_file:
            raise PromptTransportError(
                "Route önizleme argümanları (--route, --route-file) ile --prompt-file aynı anda verilemez."
            )
        if use_stdin:
            raise PromptTransportError(
                "Route önizleme argümanları (--route, --route-file) ile --stdin aynı anda verilemez."
            )

        if route_file:
            text = read_prompt_file(route_file, cwd)
            return ResolvedPromptSource(
                prompt=text,
                is_route_only=True,
                source_kind="route-file",
                source_path=str(route_file),
            )
        else:
            text = validate_prompt_text(route_text, source_name="--route")
            return ResolvedPromptSource(
                prompt=text,
                is_route_only=True,
                source_kind="route",
            )

    # 2. Execution prompt conflicts
    exec_sources: list[str] = []
    if raw_positional:
        exec_sources.append("positional prompt")
    if prompt_file:
        exec_sources.append("--prompt-file")
    if use_stdin:
        exec_sources.append("--stdin")

    if len(exec_sources) > 1:
        sources_str = " ve ".join(exec_sources)
        raise PromptTransportError(
            f"Birden fazla prompt kaynağı belirtildi ({sources_str}). Lütfen yalnız bir prompt kaynağı seçin."
        )

    if prompt_file:
        text = read_prompt_file(prompt_file, cwd)
        return ResolvedPromptSource(
            prompt=text,
            is_route_only=False,
            source_kind="prompt-file",
            source_path=str(prompt_file),
        )

    if use_stdin:
        text = read_prompt_stdin(is_tty_error=True)
        return ResolvedPromptSource(
            prompt=text,
            is_route_only=False,
            source_kind="stdin",
        )

    if raw_positional:
        text = validate_prompt_text(raw_positional, source_name="positional prompt")
        return ResolvedPromptSource(
            prompt=text,
            is_route_only=False,
            source_kind="positional",
        )

    return ResolvedPromptSource(
        prompt=None,
        is_route_only=False,
        source_kind="interactive",
    )
