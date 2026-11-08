from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from abridged_convert.common import normalize_categories
from abridged_convert.constants import CATEGORY_ORDER, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from abridged_convert.models import ConversionError, ConversionResult, Job


def find_top_level_dirs(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


def find_primary_video(entry_dir: Path) -> Path:
    candidates = sorted(
        child
        for child in entry_dir.iterdir()
        if child.is_file() and child.stem == "1" and child.suffix.lower() in VIDEO_EXTENSIONS
    )
    if not candidates:
        raise ConversionError("missing canonical primary video named 1.<ext>")
    if len(candidates) > 1:
        names = ", ".join(path.name for path in candidates)
        raise ConversionError(f"multiple primary video candidates found: {names}")
    return candidates[0]


def find_external_cover(entry_dir: Path) -> Path | None:
    for child in sorted(entry_dir.iterdir()):
        if child.is_file() and child.stem.lower() == "cover" and child.suffix.lower() in IMAGE_EXTENSIONS:
            return child
    return None


def resolve_input_roots(
    input_root: Path,
    categories: Iterable[str],
) -> tuple[dict[str, Path], list[str], Path]:
    resolved_input = input_root.resolve()
    requested_categories = normalize_categories(categories)
    category_dirs = {
        category: resolved_input / category
        for category in CATEGORY_ORDER
        if (resolved_input / category).is_dir()
    }
    if category_dirs:
        selected_categories = [category for category in requested_categories if category in category_dirs]
        if not selected_categories:
            available = ", ".join(category_dirs)
            raise ConversionError(
                f"input directory does not contain any requested categories; available categories: {available}"
            )
        return (
            {category: category_dirs[category] for category in selected_categories},
            selected_categories,
            resolved_input / "Web",
        )

    valid_categories = {category.lower(): category for category in CATEGORY_ORDER}
    direct_category = valid_categories.get(resolved_input.name.lower())
    if direct_category is not None:
        if direct_category not in requested_categories:
            requested = ", ".join(requested_categories)
            raise ConversionError(
                f"input directory is {direct_category}, which does not match the requested categories: {requested}"
            )
        return ({direct_category: resolved_input}, [direct_category], resolved_input.parent / "Web")

    expected = ", ".join(CATEGORY_ORDER)
    raise ConversionError(
        f"input directory must either contain {expected} folders or point to one of those category folders directly"
    )


def discovery_failure(
    category: str,
    entry_dir: Path,
    output_file: Path,
    message: str,
) -> ConversionResult:
    return ConversionResult(
        category=category,
        entry_dir=entry_dir,
        source_video=entry_dir,
        output_file=output_file,
        message=message,
    )


def parse_entry_name(entry_name: str) -> tuple[str, str]:
    match = re.match(r"^\[(?P<creator>[^\]]+)\]\s*(?P<title>.+)$", entry_name)
    if not match:
        return entry_name, entry_name
    return match.group("creator").strip(), match.group("title").strip()


def relative_label(repo_root: Path, job: Job | ConversionResult) -> str:
    try:
        relative_output = job.output_file.relative_to(repo_root)
    except ValueError:
        relative_output = job.output_file
    return f"{job.category}/{job.entry_dir.name}/{job.source_video.name} -> {relative_output}"


def compact_label(job: Job | ConversionResult) -> str:
    creator, title = parse_entry_name(job.entry_dir.name)
    creator_label = f"[{creator}]"
    if job.category == "Series":
        return f"{job.category}  {creator_label} {title}  {job.source_video.stem}"
    return f"{job.category}  {creator_label} {title}"


def derived_output_title(job: Job) -> str:
    _, entry_title = parse_entry_name(job.entry_dir.name)
    if job.category == "Series":
        return f"{entry_title} - {job.source_video.stem}"
    return entry_title


def discover_single_item_job(category: str, entry_dir: Path, output_file: Path) -> Job | ConversionResult:
    try:
        source_video = find_primary_video(entry_dir)
    except ConversionError as exc:
        return discovery_failure(
            category,
            entry_dir,
            output_file,
            str(exc),
        )

    return Job(
        category=category,
        entry_dir=entry_dir,
        source_video=source_video,
        output_file=output_file,
        external_cover=find_external_cover(entry_dir),
    )


def discover_amv_jobs(amv_root: Path, output_root: Path) -> list[Job]:
    covers_dir = amv_root / "covers"
    jobs: list[Job] = []
    for child in sorted(amv_root.iterdir()):
        if child.is_dir() or child.suffix.lower() != ".mkv":
            continue
        _, title = parse_entry_name(child.stem)
        jobs.append(
            Job(
                category="Songs",
                entry_dir=amv_root / child.stem,
                source_video=child,
                output_file=output_root / "AMV" / child.stem / "1.mp4",
                cover_search_dir=covers_dir,
                cover_match_source=child.with_name(f"{title}{child.suffix}"),
            )
        )
    return jobs


def discover_jobs_for_root(category: str, input_root: Path, output_root: Path) -> tuple[list[Job], list[ConversionResult]]:
    jobs: list[Job] = []
    failures: list[ConversionResult] = []
    for entry_dir in find_top_level_dirs(input_root):
        if category in {"Shots", "Shorts", "Songs"}:
            if category == "Songs" and entry_dir.name == "[~] AMVs":
                jobs.extend(discover_amv_jobs(entry_dir, output_root))
                continue

            discovered = discover_single_item_job(category, entry_dir, output_root / entry_dir.name / "1.mp4")
            if isinstance(discovered, ConversionResult):
                failures.append(discovered)
            else:
                jobs.append(discovered)
            continue

        for child in sorted(entry_dir.iterdir()):
            if child.is_dir():
                continue
            if child.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            jobs.append(
                Job(
                    category=category,
                    entry_dir=entry_dir,
                    source_video=child,
                    output_file=output_root / entry_dir.name / f"{child.stem}.mp4",
                    external_cover=find_external_cover(entry_dir),
                )
            )
    return jobs, failures


def discover_jobs(
    input_root: Path,
    categories: Iterable[str],
    output_root: Path | None = None,
) -> tuple[list[Job], list[ConversionResult]]:
    jobs: list[Job] = []
    failures: list[ConversionResult] = []
    category_roots, _, inferred_output_root = resolve_input_roots(input_root, categories)
    resolved_output_root = (output_root or inferred_output_root).resolve()
    for category, category_root in category_roots.items():
        category_jobs, category_failures = discover_jobs_for_root(
            category,
            category_root,
            resolved_output_root / category,
        )
        jobs.extend(category_jobs)
        failures.extend(category_failures)
    return jobs, failures

