from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Literal, overload

from abridged_convert.constants import CATEGORY_ORDER
from abridged_convert.models import ConversionError


@overload
def normalize_categories(
    values: Iterable[str] | None,
    *,
    allow_none: Literal[False] = False,
    error_label: str = "category",
) -> tuple[str, ...]: ...


@overload
def normalize_categories(
    values: Iterable[str] | None,
    *,
    allow_none: Literal[True],
    error_label: str = "category",
) -> tuple[str, ...] | None: ...


def normalize_categories(
    values: Iterable[str] | None,
    *,
    allow_none: bool = False,
    error_label: str = "category",
) -> tuple[str, ...] | None:
    if values is None:
        if allow_none:
            return None
        values = CATEGORY_ORDER

    normalized: list[str] = []
    valid = {category.lower(): category for category in CATEGORY_ORDER}
    for value in values:
        category = valid.get(value.lower())
        if category is None:
            raise ConversionError(f"unknown {error_label}: {value}")
        if category not in normalized:
            normalized.append(category)
    return tuple(normalized)


def path_is_in_categories(path: str, categories: Iterable[str]) -> bool:
    return path.split("/", 1)[0] in set(categories)


def source_signature(input_root: Path, source_path: Path) -> dict[str, Any]:
    stat = source_path.stat()
    try:
        relative_source_path = source_path.resolve().relative_to(input_root.resolve()).as_posix()
    except ValueError:
        relative_source_path = source_path.resolve().as_posix()
    return {
        "path": relative_source_path,
        "size": stat.st_size,
        "modTimeUnixNano": stat.st_mtime_ns,
    }
