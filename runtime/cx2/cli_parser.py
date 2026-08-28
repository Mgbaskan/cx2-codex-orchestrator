from __future__ import annotations

import argparse

from input_adapter import CX2InputAction


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cx",
        description="CX 2.0 direct Codex App Server runtime",
    )
    parser.add_argument("prompt", nargs="*", help="Codex görevi")
    parser.add_argument(
        "--prompt-file", metavar="PATH",
        help="UTF-8 dosya içeriğini birincil prompt olarak kullanır",
    )
    parser.add_argument(
        "--stdin", action="store_true",
        help="UTF-8 promptu stdin üzerinden okur",
    )
    parser.add_argument(
        "--doctor", action="store_true", help="CX runtime sağlık kontrolü",
    )
    parser.add_argument(
        "--route", metavar="TEXT",
        help="Sadece lokal routing sonucunu göster; model turn'ü başlatmaz",
    )
    parser.add_argument(
        "--route-file", metavar="PATH",
        help="Dosyadaki promptun routing sonucunu model turn olmadan gösterir",
    )
    parser.add_argument(
        "--stats", action="store_true", help="Yerel token telemetri özetini göster",
    )
    parser.add_argument(
        "--quota", action="store_true",
        help="Codex kota anlık görüntüsünü yenileyip göster; model turnü başlatmaz",
    )
    parser.add_argument(
        "--session", action="store_true", help="Aktif repo session bilgisini göster",
    )
    parser.add_argument(
        "--new", action="store_true",
        help="Persisted repo session bağlantısını sıfırla",
    )
    parser.add_argument(
        "--version", action="store_true", help="CX2 runtime sürümünü göster",
    )
    parser.add_argument(
        "--attach", action=CX2InputAction, input_kind="attach", default=[],
        metavar="PATH",
        help="Yerel dosya ekle; resimler native localImage, diğer dosyalar path mention olarak gönderilir",
    )
    parser.add_argument(
        "--image", action=CX2InputAction, input_kind="image", default=[],
        metavar="PATH", help="Yerel resmi native image input olarak ekle",
    )
    parser.add_argument(
        "--image-url", action=CX2InputAction, input_kind="image_url", default=[],
        metavar="URL", help="Uzak resmi URL image input olarak ekle",
    )
    parser.add_argument(
        "--file", action=CX2InputAction, input_kind="file", default=[],
        metavar="PATH", help="Yerel dosya/PDF/binary path mention ekle",
    )
    parser.add_argument("--usage-db", metavar="PATH", help=argparse.SUPPRESS)
    return parser


__all__ = ["build_parser"]
