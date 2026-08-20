from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


# CX2_ATTACHMENT_INPUT_ADAPTER_V2
# CX2_ORDER_PRESERVING_ATTACHMENTS_V1

IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
}

IMAGE_DETAILS = {
    "auto",
    "low",
    "high",
    "original",
}


class CX2InputError(ValueError):
    pass


class CX2InputAction(argparse.Action):
    """
    argparse Action that preserves the exact cross-option order of
    --attach / --image / --image-url / --file.

    Standard action="append" keeps order only inside each individual
    option and loses the ordering between different option types.
    """

    def __init__(
        self,
        option_strings,
        dest,
        *,
        input_kind: str,
        **kwargs,
    ):

        self.input_kind = input_kind

        super().__init__(
            option_strings,
            dest,
            **kwargs,
        )


    def __call__(
        self,
        parser,
        namespace,
        values,
        option_string=None,
    ):

        current = list(
            getattr(
                namespace,
                self.dest,
                None,
            )
            or []
        )

        current.append(
            values
        )

        setattr(
            namespace,
            self.dest,
            current,
        )

        ordered = list(
            getattr(
                namespace,
                "_cx_input_specs",
                None,
            )
            or []
        )

        ordered.append(
            (
                self.input_kind,
                str(
                    values
                ),
            )
        )

        setattr(
            namespace,
            "_cx_input_specs",
            ordered,
        )


def resolve_local_file(
    value: str,
    cwd: Path,
) -> Path:

    if not isinstance(
        value,
        str,
    ) or not value.strip():
        raise CX2InputError(
            "Boş attachment path."
        )

    candidate = Path(
        value
    ).expanduser()

    if not candidate.is_absolute():
        candidate = (
            cwd
            / candidate
        )

    try:
        resolved = candidate.resolve(
            strict=True
        )

    except FileNotFoundError as exc:
        raise CX2InputError(
            f"Attachment bulunamadı: {value}"
        ) from exc

    if not resolved.is_file():
        raise CX2InputError(
            f"Attachment dosya değil: {resolved}"
        )

    return resolved


def local_image_item(
    path: Path,
    *,
    detail: str = "auto",
) -> dict[str, Any]:

    if detail not in IMAGE_DETAILS:
        raise CX2InputError(
            f"Geçersiz image detail: {detail}"
        )

    if path.suffix.casefold() not in IMAGE_EXTENSIONS:
        raise CX2InputError(
            "Native image input için desteklenmeyen uzantı: "
            f"{path.suffix or '<none>'}"
        )

    return {
        "type":
            "localImage",

        "detail":
            detail,

        "path":
            str(
                path
            ),
    }


def image_url_item(
    value: str,
    *,
    detail: str = "auto",
) -> dict[str, Any]:

    if detail not in IMAGE_DETAILS:
        raise CX2InputError(
            f"Geçersiz image detail: {detail}"
        )

    if not isinstance(
        value,
        str,
    ) or not value.strip():
        raise CX2InputError(
            "Boş image URL."
        )

    parsed = urlparse(
        value
    )

    if parsed.scheme.casefold() not in {
        "http",
        "https",
        "data",
    }:
        raise CX2InputError(
            "Image URL scheme desteklenmiyor: "
            f"{parsed.scheme or '<none>'}"
        )

    return {
        "type":
            "image",

        "detail":
            detail,

        "url":
            value,
    }


def mention_item(
    path: Path,
) -> dict[str, Any]:

    return {
        "type":
            "mention",

        "name":
            path.name,

        "path":
            str(
                path
            ),
    }


def build_cli_input_items(
    args: Any,
    cwd: Path,
) -> list[dict[str, Any]]:

    cwd = cwd.resolve()

    result: list[dict[str, Any]] = []

    seen: set[
        tuple[
            str,
            str,
        ]
    ] = set()


    def append(
        item: dict[str, Any],
    ) -> None:

        kind = str(
            item.get(
                "type",
                "",
            )
        )

        location = str(
            item.get(
                "path",
                item.get(
                    "url",
                    "",
                ),
            )
        )

        key = (
            kind,
            location.casefold()
            if kind in {
                "localImage",
                "mention",
            }
            else location,
        )

        if key in seen:
            return

        seen.add(
            key
        )

        result.append(
            item
        )


    def emit(
        kind: str,
        value: str,
    ) -> None:

        if kind == "image":

            path = resolve_local_file(
                value,
                cwd,
            )

            append(
                local_image_item(
                    path
                )
            )

            return


        if kind == "image_url":

            append(
                image_url_item(
                    value
                )
            )

            return


        if kind == "file":

            path = resolve_local_file(
                value,
                cwd,
            )

            append(
                mention_item(
                    path
                )
            )

            return


        if kind == "attach":

            path = resolve_local_file(
                value,
                cwd,
            )

            if (
                path.suffix.casefold()
                in IMAGE_EXTENSIONS
            ):
                append(
                    local_image_item(
                        path
                    )
                )

            else:
                append(
                    mention_item(
                        path
                    )
                )

            return


        raise CX2InputError(
            "Bilinmeyen attachment kind: "
            + repr(
                kind
            )
        )


    ordered_specs = getattr(
        args,
        "_cx_input_specs",
        None,
    )


    if ordered_specs:

        for kind, value in ordered_specs:

            emit(
                str(
                    kind
                ),
                str(
                    value
                ),
            )

        return result


    # Compatibility fallback for programmatic Namespace callers.
    for kind, attr in (
        (
            "image",
            "image",
        ),
        (
            "image_url",
            "image_url",
        ),
        (
            "file",
            "file",
        ),
        (
            "attach",
            "attach",
        ),
    ):

        values = getattr(
            args,
            attr,
            None,
        )

        if values is None:
            continue

        if isinstance(
            values,
            str,
        ):
            values = [
                values
            ]

        for value in values:

            emit(
                kind,
                str(
                    value
                ),
            )


    return result