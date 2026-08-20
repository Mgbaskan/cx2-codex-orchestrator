from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


# CX2_NUMERIC_SELECTION_CONTEXT_V1


@dataclass
class SelectionEntry:
    index: int
    thread_id: str
    cwd: str | None
    title: str
    status: str | None
    updated_at: Any


class SelectionContext:
    """
    Process-local in-memory numeric thread selection layer.

    Maintains sequential integer aliases (1..N) mapping to native Codex
    thread IDs for the most recently rendered /history or /search output.
    Does not persist across processes and does not mutate native state.
    """

    def __init__(self) -> None:
        self.source: str = ""
        self.title: str = ""
        self.entries: dict[int, SelectionEntry] = {}

    def set_entries(
        self,
        source: str,
        title: str,
        entries: list[dict[str, Any]],
    ) -> None:
        self.source = source
        self.title = title
        self.entries = {}

        for idx, entry in enumerate(entries, start=1):
            tid = str(entry.get("id") or "").strip()
            if not tid:
                continue
            self.entries[idx] = SelectionEntry(
                index=idx,
                thread_id=tid,
                cwd=entry.get("cwd"),
                title=str(entry.get("nameOrPreview") or "(isimsiz thread)").strip(),
                status=entry.get("status"),
                updated_at=entry.get("updatedAt"),
            )

    def clear(self) -> None:
        self.source = ""
        self.title = ""
        self.entries.clear()

    def update_title_for_thread(
        self,
        thread_id: str,
        new_title: str,
    ) -> None:
        target_tid = str(thread_id).strip()
        for entry in self.entries.values():
            if entry.thread_id == target_tid:
                entry.title = new_title.strip()

    def resolve(
        self,
        ref: str,
    ) -> str:
        ref_str = str(ref).strip()
        if not ref_str:
            return ""

        # A. Canonical positive decimal alias (1, 2, 10, 25)
        if re.match(r"^[1-9][0-9]*$", ref_str):
            idx = int(ref_str)
            if not self.entries:
                from history_manager import HistoryManagerError

                raise HistoryManagerError(
                    "Aktif bir numaralı thread listesi yok. "
                    "Önce /history veya /search kullan."
                )
            if idx not in self.entries:
                from history_manager import HistoryManagerError

                max_idx = len(self.entries)
                raise HistoryManagerError(
                    f"Geçersiz thread numarası: {idx}. "
                    f"Geçerli aralık: 1-{max_idx}."
                )
            return self.entries[idx].thread_id

        # B. Invalid numeric-like selector (0, 02, -1, +1)
        if re.match(r"^[+-]?[0-9]+$", ref_str):
            from history_manager import HistoryManagerError

            if ref_str.startswith(("+", "-")):
                raise HistoryManagerError(
                    f"Geçersiz thread numarası: '{ref_str}'. "
                    "Pozitif tamsayı (1, 2, 3...) olmalıdır."
                )
            if ref_str == "0" or ref_str.startswith("0"):
                raise HistoryManagerError(
                    f"Geçersiz thread numarası: '{ref_str}'. "
                    "Baştaki sıfırlar desteklenmiyor; pozitif tamsayı (1, 2, 3...) olmalıdır."
                )
            raise HistoryManagerError(
                f"Geçersiz thread numarası: '{ref_str}'."
            )

        # C. Native thread reference (UUID or alphanumeric ID)
        return ref_str
