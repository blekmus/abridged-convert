from __future__ import annotations

import json
import math
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from abridged_convert.common import normalize_scope, path_in_scope, scope_categories
from abridged_convert.constants import CATEGORY_ORDER, VIDEO_EXTENSIONS
from abridged_convert.models import ConversionError


METADATA_CACHE_FILENAME = ".abridged-video-metadata.json"
METADATA_CACHE_VERSION = 2
DEPLOY_RUN_SIGNATURE_KEY = "deployRunSignature"
DEPLOY_RUN_STATUS_KEY = "deployRunStatus"
DEPLOY_RUN_STATUS_IN_PROGRESS = "inProgress"
METADATA_UPLOAD_PENDING_KEY = "metadataUploadPending"
ITEM_UPLOAD_PENDING_KEY = "uploaded"
SUBTITLES_KEY = "subtitles"
# Local journal only: remote subtitle files to delete once the interrupted run finishes.
STALE_REMOTE_PATHS_KEY = "staleRemotePaths"


@dataclass(frozen=True)
class MetadataUpdateSummary:
    reused: bool
    probed: bool
    failed: bool
    output_path: Path
    written: bool


@dataclass(frozen=True)
class MetadataPruneSummary:
    pruned: int
    output_path: Path
    written: bool
    pruned_keys: tuple[str, ...] = ()


def update_metadata_cache_item_with_tracking(
    archive_root: Path,
    video_path: Path,
    *,
    ffprobe_bin: str,
    deploy_item: dict[str, Any] | None = None,
) -> MetadataUpdateSummary:
    resolved_archive_root = archive_root.resolve()
    if not resolved_archive_root.is_dir():
        raise ConversionError(f"metadata archive root is not a directory: {archive_root}")

    cache_path = resolved_archive_root / METADATA_CACHE_FILENAME
    cache_key = cache_key_for_path(resolved_archive_root, video_path)
    existing_items = load_cache_items(cache_path)
    existing_deploy_items = load_cache_deploy_items(cache_path)
    existing_item = existing_items.get(cache_key)
    stat = video_path.stat()
    size = stat.st_size
    mod_time_unix_nano = stat.st_mtime_ns

    if item_is_fresh(existing_item, size, mod_time_unix_nano):
        if deploy_item is None:
            return MetadataUpdateSummary(reused=True, probed=False, failed=False, output_path=cache_path, written=False)
        existing_deploy_item = existing_deploy_items.get(cache_key)
        if existing_deploy_item == deploy_item:
            return MetadataUpdateSummary(reused=True, probed=False, failed=False, output_path=cache_path, written=False)
        write_cache(cache_path, existing_items, deploy_items={**existing_deploy_items, cache_key: deploy_item})
        return MetadataUpdateSummary(reused=True, probed=False, failed=False, output_path=cache_path, written=True)

    next_item = build_metadata_cache_item(video_path, ffprobe_bin=ffprobe_bin, deploy_item=deploy_item)
    if next_item is None:
        return MetadataUpdateSummary(reused=False, probed=False, failed=True, output_path=cache_path, written=False)

    existing_items[cache_key] = next_item
    write_cache(cache_path, existing_items)
    return MetadataUpdateSummary(reused=False, probed=True, failed=False, output_path=cache_path, written=True)


def prune_missing_metadata_cache_items(
    archive_root: Path,
    *,
    scope: Iterable[str] | None = None,
) -> MetadataPruneSummary:
    resolved_archive_root = archive_root.resolve()
    if not resolved_archive_root.is_dir():
        raise ConversionError(f"metadata archive root is not a directory: {archive_root}")

    cache_path = resolved_archive_root / METADATA_CACHE_FILENAME
    existing_items = load_cache_items(cache_path)
    if not existing_items:
        return MetadataPruneSummary(pruned=0, output_path=cache_path, written=False)

    existing_deploy_items = load_cache_deploy_items(cache_path)
    selected_scope = normalize_scope(scope)
    discovered_keys = {
        cache_key_for_path(resolved_archive_root, video_path)
        for video_path in discover_archive_videos(resolved_archive_root, categories=scope_categories(selected_scope))
    }
    next_items = {
        key: value
        for key, value in existing_items.items()
        if not path_in_scope(key, selected_scope) or key in discovered_keys
    }
    pruned_keys = tuple(sorted(set(existing_items) - set(next_items)))
    if not pruned_keys:
        return MetadataPruneSummary(pruned=0, output_path=cache_path, written=False)

    write_cache(cache_path, next_items, deploy_items=existing_deploy_items)
    return MetadataPruneSummary(pruned=len(pruned_keys), output_path=cache_path, written=True, pruned_keys=pruned_keys)


def prune_unexpected_metadata_cache_items(
    archive_root: Path,
    expected_cache_keys: set[str],
    *,
    scope: Iterable[str] | None = None,
    delete_files: bool = False,
) -> MetadataPruneSummary:
    resolved_archive_root = archive_root.resolve()
    if not resolved_archive_root.is_dir():
        raise ConversionError(f"metadata archive root is not a directory: {archive_root}")

    cache_path = resolved_archive_root / METADATA_CACHE_FILENAME
    existing_items = load_cache_items(cache_path)
    if not existing_items:
        return MetadataPruneSummary(pruned=0, output_path=cache_path, written=False)

    existing_deploy_items = load_cache_deploy_items(cache_path)
    selected_scope = normalize_scope(scope)
    next_items: dict[str, dict[str, Any]] = {}
    pruned_items: list[tuple[str, dict[str, Any]]] = []
    for key, item in existing_items.items():
        if path_in_scope(key, selected_scope) and key not in expected_cache_keys:
            pruned_items.append((key, item))
            continue
        next_items[key] = item

    pruned_keys = tuple(sorted(key for key, _ in pruned_items))
    if not pruned_keys:
        return MetadataPruneSummary(pruned=0, output_path=cache_path, written=False)

    if delete_files:
        kept_files = {path for key, item in next_items.items() for path in item_output_files(key, item)}
        for key, item in pruned_items:
            delete_pruned_output_files(resolved_archive_root, key, item, keep=kept_files)

    write_cache(cache_path, next_items, deploy_items=existing_deploy_items)
    return MetadataPruneSummary(pruned=len(pruned_keys), output_path=cache_path, written=True, pruned_keys=pruned_keys)


def build_metadata_cache_item(
    video_path: Path,
    *,
    ffprobe_bin: str,
    deploy_item: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    probed_item = probe_video_metadata(ffprobe_bin, video_path)
    if probed_item is None:
        return None
    stat = video_path.stat()
    item = {
        "size": stat.st_size,
        "modTimeUnixNano": stat.st_mtime_ns,
        **probed_item,
    }
    if deploy_item is not None:
        merge_deploy_tracking(item, deploy_item)
    return item


def merge_deploy_tracking(item: dict[str, Any], deploy_item: dict[str, Any]) -> None:
    for deploy_key in ("source", "outputPath", "coverPath", SUBTITLES_KEY, "deployedAt", ITEM_UPLOAD_PENDING_KEY):
        if deploy_key in deploy_item:
            item[deploy_key] = deploy_item[deploy_key]


def discover_archive_videos(archive_root: Path, *, categories: Iterable[str] | None = None) -> list[Path]:
    videos: list[Path] = []
    selected_categories = tuple(categories) if categories is not None else CATEGORY_ORDER

    series_root = archive_root / "Series"
    if "Series" in selected_categories and series_root.is_dir():
        for entry_dir in top_level_dirs(series_root):
            videos.extend(
                sorted(
                    child
                    for child in entry_dir.iterdir()
                    if child.is_file() and child.suffix.lower() in VIDEO_EXTENSIONS
                )
            )

    for category in ("Shorts", "Shots"):
        if category not in selected_categories:
            continue
        category_root = archive_root / category
        if not category_root.is_dir():
            continue
        for entry_dir in top_level_dirs(category_root):
            videos.extend(find_canonical_output_videos(entry_dir))

    songs_root = archive_root / "Songs"
    if "Songs" in selected_categories and songs_root.is_dir():
        for entry_dir in top_level_dirs(songs_root):
            if entry_dir.name == "AMV":
                continue
            videos.extend(find_canonical_output_videos(entry_dir))

        amv_root = songs_root / "AMV"
        if amv_root.is_dir():
            for entry_dir in top_level_dirs(amv_root):
                videos.extend(find_canonical_output_videos(entry_dir))

    return videos


def find_canonical_output_videos(entry_dir: Path) -> list[Path]:
    return sorted(
        child
        for child in entry_dir.iterdir()
        if child.is_file() and child.stem == "1" and child.suffix.lower() in VIDEO_EXTENSIONS
    )


def top_level_dirs(root: Path) -> list[Path]:
    return sorted(path for path in root.iterdir() if path.is_dir())


def load_cache_document(cache_path: Path) -> dict[str, Any]:
    if not cache_path.exists():
        return {}

    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(cache, dict):
        return {}
    if cache.get("version") != METADATA_CACHE_VERSION:
        return {}
    return cache


def load_cache_items(cache_path: Path) -> dict[str, dict[str, Any]]:
    cache = load_cache_document(cache_path)
    if not cache:
        return {}
    items = cache.get("items")
    if not isinstance(items, dict):
        return {}

    return {key: value for key, value in items.items() if isinstance(key, str) and isinstance(value, dict)}


def load_cache_deploy_items(cache_path: Path) -> dict[str, dict[str, Any]]:
    cache = load_cache_document(cache_path)
    if not cache:
        return {}

    items = cache.get("items")
    if not isinstance(items, dict):
        return {}

    merged_items = {
        key: deploy_state_from_metadata_item(value)
        for key, value in items.items()
        if isinstance(key, str) and isinstance(value, dict)
    }
    return {key: value for key, value in merged_items.items() if value is not None}


def deploy_state_from_metadata_item(item: dict[str, Any]) -> dict[str, Any] | None:
    source = item.get("source")
    output_path = item.get("outputPath")
    cover_path = item.get("coverPath")
    deployed_at = item.get("deployedAt")
    if not isinstance(source, dict) or not isinstance(output_path, str) or not isinstance(cover_path, str):
        return None

    state = {
        "source": source,
        "outputPath": output_path,
        "coverPath": cover_path,
    }
    if isinstance(deployed_at, str):
        state["deployedAt"] = deployed_at
    if isinstance(item.get(SUBTITLES_KEY), list):
        state[SUBTITLES_KEY] = item[SUBTITLES_KEY]
    return state


def item_subtitle_paths(item: dict[str, Any]) -> list[str]:
    """Relative paths of an item's subtitle sidecars (empty when none or not yet processed)."""
    subtitles = item.get(SUBTITLES_KEY)
    if not isinstance(subtitles, list):
        return []
    return [entry["path"] for entry in subtitles if isinstance(entry, dict) and isinstance(entry.get("path"), str)]


def subtitle_metadata_entries(root: Path, sidecars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert sidecars from media.extract_subtitle_sidecars (absolute paths) into metadata entries."""
    return [{**sidecar, "path": cache_key_for_path(root, Path(sidecar["path"]))} for sidecar in sidecars]


def item_is_fresh(item: dict[str, Any] | None, size: int, mod_time_unix_nano: int) -> bool:
    if item is None:
        return False
    return item.get("size") == size and item.get("modTimeUnixNano") == mod_time_unix_nano


def probe_video_metadata(ffprobe_bin: str, video_path: Path) -> dict[str, Any] | None:
    try:
        completed = subprocess.run(
            [
                ffprobe_bin,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                str(video_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        probe = json.loads(completed.stdout)
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return None

    format_data = probe.get("format")
    if not isinstance(format_data, dict):
        return None

    item: dict[str, Any] = {}
    duration = parse_duration_seconds(format_data.get("duration"))
    if duration is not None:
        item["durationSeconds"] = duration

    tags = format_data.get("tags")
    if isinstance(tags, dict):
        normalized_tags = {str(key).lower(): value for key, value in tags.items() if isinstance(value, str)}
        description = first_non_empty(normalized_tags.get("description"), normalized_tags.get("synopsis"))
        date = first_non_empty(normalized_tags.get("date"))
        if description:
            item["description"] = description
        if date:
            item["date"] = date

    return item


def parse_duration_seconds(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        duration = float(value)
    except ValueError:
        return None
    if not math.isfinite(duration) or duration < 0:
        return None
    return int(duration)


def first_non_empty(*values: str | None) -> str | None:
    for value in values:
        if value and value.strip():
            return value
    return None


def cache_key_for_path(archive_root: Path, video_path: Path) -> str:
    try:
        cache_path = video_path.resolve().relative_to(archive_root.resolve())
    except ValueError:
        cache_path = video_path.resolve()
    return cache_path.as_posix()


def item_output_files(cache_key: str, item: dict[str, Any]) -> list[str]:
    """Relative paths of every file an item owns: video, cover and subtitle sidecars."""
    output_path = item.get("outputPath")
    if not isinstance(output_path, str):
        output_path = cache_key
    cover_path = item.get("coverPath")
    if not isinstance(cover_path, str):
        cover_path = Path(cache_key).with_suffix(".webp").as_posix()
    return [output_path, cover_path, *item_subtitle_paths(item)]


def delete_pruned_output_files(
    archive_root: Path,
    cache_key: str,
    item: dict[str, Any],
    *,
    keep: set[str] | frozenset[str] = frozenset(),
) -> None:
    for relative_path in item_output_files(cache_key, item):
        # A surviving item can share a file name with a pruned one (e.g. 1.mp4 -> 1.webm both use 1.webp).
        if relative_path not in keep:
            delete_relative_file(archive_root, relative_path)


def delete_relative_file(root: Path, relative_path: str) -> None:
    path = safe_relative_path(root, relative_path)
    if path is None or not path.is_file():
        return
    try:
        path.unlink()
    except OSError as exc:
        raise ConversionError(f"failed to delete pruned output file {path}: {exc}") from exc
    prune_empty_dirs(path.parent, stop_at=root)


def safe_relative_path(root: Path, relative_path: str) -> Path | None:
    if not relative_path or Path(relative_path).is_absolute():
        return None
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def prune_empty_dirs(path: Path, *, stop_at: Path) -> None:
    current = path.resolve()
    resolved_stop = stop_at.resolve()
    while current != resolved_stop:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def write_cache(
    cache_path: Path,
    items: dict[str, dict[str, Any]],
    *,
    deploy_items: dict[str, dict[str, Any]] | None = None,
    deploy_run_signature: str | None = None,
    deploy_run_status: str | None = None,
    metadata_upload_pending: bool | None = None,
    stale_remote_paths: Iterable[str] | None = None,
    include_item_upload_state: bool = True,
) -> None:
    merged_items = {
        key: dict(value)
        for key, value in items.items()
    }
    if deploy_items is not None:
        for key, deploy_item in deploy_items.items():
            if key not in merged_items:
                continue
            merge_deploy_tracking(merged_items[key], deploy_item)

    cache = {
        "version": METADATA_CACHE_VERSION,
        "items": dict(sorted(clean_metadata_items(merged_items, include_upload_state=include_item_upload_state).items())),
    }
    if deploy_run_signature is not None:
        cache[DEPLOY_RUN_SIGNATURE_KEY] = deploy_run_signature
    if deploy_run_status is not None:
        cache[DEPLOY_RUN_STATUS_KEY] = deploy_run_status
    if metadata_upload_pending is not None:
        cache[METADATA_UPLOAD_PENDING_KEY] = metadata_upload_pending
    if stale_remote_paths:
        cache[STALE_REMOTE_PATHS_KEY] = sorted(set(stale_remote_paths))
    data = json.dumps(cache, indent=2, ensure_ascii=False)
    temp_path = cache_path.with_name(f"{cache_path.name}.tmp")
    try:
        temp_path.write_text(f"{data}\n", encoding="utf-8")
        os.replace(temp_path, cache_path)
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise ConversionError(f"failed to write metadata cache: {exc}") from exc


def clean_metadata_items(
    items: dict[str, dict[str, Any]],
    *,
    include_upload_state: bool,
) -> dict[str, dict[str, Any]]:
    if include_upload_state:
        return items
    cleaned: dict[str, dict[str, Any]] = {}
    for key, item in items.items():
        next_item = dict(item)
        next_item.pop(ITEM_UPLOAD_PENDING_KEY, None)
        cleaned[key] = next_item
    return cleaned
