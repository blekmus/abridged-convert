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


def normalize_scope(values: Iterable[str] | None) -> tuple[str, ...]:
    """Normalize a run scope: output-relative path prefixes, each a category or an entry inside one.

    ``None`` means every category. The leading category segment is matched case-insensitively.
    """
    if values is None:
        return CATEGORY_ORDER
    normalized: list[str] = []
    for value in values:
        head, separator, rest = value.strip("/").partition("/")
        (category,) = normalize_categories([head], error_label="scope category")
        prefix = f"{category}{separator}{rest}"
        if prefix not in normalized:
            normalized.append(prefix)
    return tuple(normalized)


def path_in_scope(path: str, scope: Iterable[str]) -> bool:
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in scope)


def scope_categories(scope: Iterable[str]) -> tuple[str, ...]:
    return normalize_categories(prefix.split("/", 1)[0] for prefix in scope)


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
