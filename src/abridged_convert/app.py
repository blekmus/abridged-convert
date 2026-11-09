from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from collections.abc import Iterable

from abridged_convert.common import normalize_categories, source_signature
from abridged_convert.constants import CATEGORY_ORDER, DELETE_LEGEND
from abridged_convert.conversion import convert_job
from abridged_convert.deploy import (
    DeployJobPlan,
    DeploySummary,
    backfill_subtitles,
    bootstrap_deploy_from_existing_outputs,
    clean_staging,
    validate_remote_root,
    finalize_deploy,
    prepare_deploy_context,
    record_pending_deploy_item,
    relative_output_key,
)
from abridged_convert.discovery import discover_jobs, resolve_input_roots
from abridged_convert.help_format import DIM, RESET, ColorArgumentParser, color_enabled
from abridged_convert.media import cover_output_path_for_job, resolve_video_handling_plan, validate_tools
from abridged_convert.metadata import (
    SUBTITLES_KEY,
    cache_key_for_path,
    prune_missing_metadata_cache_items,
    prune_unexpected_metadata_cache_items,
    subtitle_metadata_entries,
    update_metadata_cache_item_with_tracking,
)
from abridged_convert.models import ConversionError, ConversionResult, Job, VideoEncodingConfig
from abridged_convert.ui import TerminalUI, deleted_file_line


PROG = "abridged-convert"
DEPLOY_POINTER_FILENAME = ".abridged-deploy.json"
STAGING_PREFIX = "abridged-deploy-"
AMV_DIR_NAME = "[~] AMVs"

PATH_RULES = """\
paths:
  ARCHIVE    = folder containing Series/ Shots/ Shorts/ Songs/   (not abridged/Series)
  OUTPUT     = web root, category folders go inside it           (not Web/Series)
  categories = Series, Shots, Shorts, Songs (any case)
  Narrow a run with --category / --entry, never with a deeper path.
  A run only adds, replaces, or prunes items inside its categories/entries."""

CONVERT_EXAMPLES = """\
  # Convert the whole archive into Web/
  abridged-convert convert abridged Web

  # Only one category (repeat --category for more)
  abridged-convert convert abridged Web --category Series

  # Only one entry: tab-complete the real path (repeat --entry for more)
  abridged-convert convert abridged Web --entry "abridged/Series/[Creator] Show"
  abridged-convert convert abridged Web --entry "abridged/Songs/[~] AMVs/[Creator] Song.mkv\""""

DEPLOY_EXAMPLES = """\
  # Preview a deploy, then run it
  abridged-convert deploy abridged bitpile:Abridged --dry-run
  abridged-convert deploy abridged bitpile:Abridged

  # Interrupted? Run the same command again to resume.

  # Deploy only one category or entry
  abridged-convert deploy abridged bitpile:Abridged --category Shots
  abridged-convert deploy abridged bitpile:Abridged --entry "abridged/Shots/[Creator] Shot\""""

REBUILD_EXAMPLES = """\
  # Recovery only: Web/ has videos but .abridged-video-metadata.json is gone
  abridged-convert rebuild-metadata abridged Web
  abridged-convert rebuild-metadata abridged Web --upload bitpile:Abridged"""


def _epilog(*example_blocks: str) -> str:
    return "examples:\n" + "\n\n".join(example_blocks) + "\n\n" + PATH_RULES


def category_arg(value: str) -> str:
    # Canonicalise case first so argparse's `choices` check accepts "series" and lists the valid names on a typo.
    return {category.lower(): category for category in CATEGORY_ORDER}.get(value.lower(), value)


def _add_scope_args(sub: argparse.ArgumentParser) -> None:
    scope = sub.add_mutually_exclusive_group()
    scope.add_argument(
        "--category",
        dest="categories",
        action="append",
        type=category_arg,
        choices=CATEGORY_ORDER,
        metavar="{" + ",".join(CATEGORY_ORDER) + "}",
        help="Only process this category (case-insensitive, repeatable).",
    )
    scope.add_argument(
        "--entry",
        dest="entries",
        action="append",
        type=Path,
        metavar="PATH",
        help="Only process this entry: ARCHIVE/<Category>/<entry> or ARCHIVE/Songs/[~] AMVs/<file> (repeatable).",
    )


def _add_tool_args(sub: argparse.ArgumentParser, *, rclone: bool) -> None:
    group = sub.add_argument_group("advanced")
    group.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg", metavar="PATH", help="ffmpeg executable.")
    group.add_argument("--ffprobe", default=shutil.which("ffprobe") or "ffprobe", metavar="PATH", help="ffprobe executable.")
    if rclone:
        group.add_argument("--rclone", default=shutil.which("rclone") or "rclone", metavar="PATH", help="rclone executable.")


def build_parser() -> argparse.ArgumentParser:
    formatter = argparse.RawDescriptionHelpFormatter
    parser = ColorArgumentParser(
        prog=PROG,
        description="Convert the abridged archive into web outputs and deploy them to an rclone remote.",
        epilog=_epilog(CONVERT_EXAMPLES, DEPLOY_EXAMPLES, REBUILD_EXAMPLES),
        formatter_class=formatter,
    )
    commands = parser.add_subparsers(dest="command", title="commands", metavar="COMMAND")
    commands.required = True

    convert = commands.add_parser(
        "convert",
        help="Convert ARCHIVE into OUTPUT locally.",
        description="Convert ARCHIVE into OUTPUT locally and write OUTPUT/.abridged-video-metadata.json.",
        epilog=_epilog(CONVERT_EXAMPLES),
        formatter_class=formatter,
    )
    convert.add_argument("archive", type=Path, metavar="ARCHIVE", help="Archive root containing Series/ Shots/ Shorts/ Songs/.")
    convert.add_argument("output", type=Path, metavar="OUTPUT", help="Web root to write into, e.g. Web.")
    convert.add_argument("--dry-run", action="store_true", help="Show what would be converted without writing anything.")
    convert.add_argument("--overwrite", action="store_true", help="Re-convert outputs that already exist.")
    _add_scope_args(convert)
    _add_tool_args(convert, rclone=False)

    deploy = commands.add_parser(
        "deploy",
        help="Convert what changed and upload it to REMOTE.",
        description=(
            "Convert new or changed entries and upload them to REMOTE.\n"
            "The remote .abridged-video-metadata.json decides what is already deployed.\n"
            "Converted files are staged in a temp dir and removed after a successful upload.\n"
            "If a run is interrupted, run the same command again to resume (until a reboot\n"
            "clears the temp dir, then it starts again). While a run is unfinished,\n"
            f"ARCHIVE/{DEPLOY_POINTER_FILENAME} records where its temp dir is."
        ),
        epilog=_epilog(DEPLOY_EXAMPLES),
        formatter_class=formatter,
    )
    deploy.add_argument("archive", type=Path, metavar="ARCHIVE", help="Archive root containing Series/ Shots/ Shorts/ Songs/.")
    deploy.add_argument("remote", metavar="REMOTE", help="rclone remote for the catalog folder, e.g. bitpile:Abridged.")
    deploy.add_argument("--dry-run", action="store_true", help="Plan uploads and deletes without changing the remote or the archive.")
    deploy.add_argument("--overwrite", action="store_true", help="Re-convert and re-upload even unchanged entries.")
    _add_scope_args(deploy)
    _add_tool_args(deploy, rclone=True)

    rebuild = commands.add_parser(
        "rebuild-metadata",
        help="Recovery: rebuild the metadata file from existing outputs.",
        description=(
            "Recovery only. Use when OUTPUT already holds converted videos and covers but\n"
            ".abridged-video-metadata.json is missing. Nothing is re-encoded.\n"
            "With --upload, also uploads the outputs and the rebuilt metadata to REMOTE."
        ),
        epilog=_epilog(REBUILD_EXAMPLES),
        formatter_class=formatter,
    )
    rebuild.add_argument("archive", type=Path, metavar="ARCHIVE", help="Archive root containing Series/ Shots/ Shorts/ Songs/.")
    rebuild.add_argument("output", type=Path, metavar="OUTPUT", help="Web root that holds the existing outputs.")
    rebuild.add_argument("--upload", dest="remote", metavar="REMOTE", help="Also upload to this rclone remote.")
    rebuild.add_argument("--dry-run", action="store_true", help="Report what would be written or uploaded.")
    _add_tool_args(rebuild, rclone=True)
    return parser


@dataclass(frozen=True)
class RunOptions:
    command: str
    archive: Path
    output: Path | None  # None for deploy until main() picks the temp staging dir
    remote: str | None
    dry_run: bool
    overwrite: bool
    categories: tuple[str, ...]  # as given; empty means every category
    entries: tuple[Path, ...]
    ffmpeg: str
    ffprobe: str
    rclone: str


def parse_args(argv: Iterable[str]) -> RunOptions:
    parser = build_parser()
    argv = list(argv)
    if not argv:
        parser.print_help(sys.stderr)
        raise SystemExit(2)
    args = parser.parse_args(argv)
    return RunOptions(
        command=args.command,
        archive=args.archive.resolve(),
        output=args.output.resolve() if args.command != "deploy" else None,
        remote=args.remote if args.command != "convert" else None,
        dry_run=args.dry_run,
        overwrite=getattr(args, "overwrite", False),
        categories=tuple(dict.fromkeys(getattr(args, "categories", None) or ())),
        entries=tuple(path.resolve() for path in getattr(args, "entries", None) or ()),
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
        rclone=getattr(args, "rclone", "rclone"),
    )


def _category_name(path: Path) -> str | None:
    return {category.lower(): category for category in CATEGORY_ORDER}.get(path.name.lower())


def _display(path: Path) -> str:
    try:
        text = path.relative_to(Path.cwd()).as_posix() or "."
    except ValueError:
        text = path.as_posix()
    return f'"{text}"' if " " in text else text


def suggested_command(options: RunOptions, archive: Path, output: Path | None, *, narrow_to: str | None) -> str:
    parts = [PROG, options.command, _display(archive)]
    if output is not None:
        parts.append(_display(output))
    if options.command == "deploy":
        parts.append(str(options.remote))
    elif options.remote is not None:
        parts.extend(["--upload", str(options.remote)])
    for flag, enabled in (("--dry-run", options.dry_run), ("--overwrite", options.overwrite)):
        if enabled:
            parts.append(flag)
    for category in options.categories:
        parts.extend(["--category", category])
    for entry in options.entries:
        parts.extend(["--entry", _display(entry)])
    if narrow_to and options.command != "rebuild-metadata" and not options.categories and not options.entries:
        parts.extend(["--category", narrow_to])
    return " ".join(parts)


def validate_cli_paths(options: RunOptions) -> None:
    """Reject category-level paths with a corrected command; never reinterpret them."""
    archive_category = _category_name(options.archive)
    output_category = _category_name(options.output) if options.output is not None else None
    if archive_category or output_category:
        archive = options.archive.parent if archive_category else options.archive
        output = options.output.parent if output_category and options.output is not None else options.output
        narrowed = archive_category or output_category
        what = "ARCHIVE must be the archive root" if archive_category else "OUTPUT must be the web root"
        try_command = suggested_command(options, archive, output, narrow_to=narrowed)
        raise ConversionError(f"{what}, not the {narrowed} category folder.\n  Try: {try_command}")
    if not any((options.archive / category).is_dir() for category in CATEGORY_ORDER):
        expected = ", ".join(f"{category}/" for category in CATEGORY_ORDER)
        raise ConversionError(f"ARCHIVE must be the archive root containing {expected}: {options.archive}")


ENTRY_SHAPES = "ARCHIVE/<Category>/<entry> (a folder) or ARCHIVE/Songs/[~] AMVs/<file>"


def entry_matches(job: Job, entry: Path) -> bool:
    if entry.is_dir():
        return job.entry_dir.resolve() == entry
    return job.source_video.resolve() == entry and entry.parent.name == AMV_DIR_NAME


def select_entries(jobs: list[Job], archive: Path, entries: tuple[Path, ...]) -> list[Job]:
    for entry in entries:
        try:
            relative = entry.relative_to(archive).as_posix()
        except ValueError:
            raise ConversionError(f"--entry must be inside ARCHIVE ({archive}): {entry}\n  expected {ENTRY_SHAPES}") from None
        if not entry.exists():
            raise ConversionError(f"--entry does not exist: {entry}")
        if not any(entry_matches(job, entry) for job in jobs):
            raise ConversionError(f"--entry is not a convertible entry: {relative}\n  expected {ENTRY_SHAPES}")
    return [job for job in jobs if any(entry_matches(job, entry) for entry in entries)]


def run_scope(output_dir: Path, jobs: list[Job], categories: list[str], entries: tuple[Path, ...]) -> tuple[str, ...]:
    if not entries:
        return tuple(categories)
    return tuple(dict.fromkeys(relative_output_key(output_dir, job.output_file.parent) for job in jobs))


def print_metadata_status(
    *,
    discovered: int,
    reused: int,
    probed: int,
    failed: int,
    pruned: int,
    output_path: Path,
    failures: int,
) -> None:
    print(
        "Metadata: "
        f"discovered={discovered} "
        f"reused={reused} "
        f"probed={probed} "
        f"failed={failed} "
        f"pruned={pruned} "
        f"output={output_path}",
        file=sys.stderr if failed or failures else sys.stdout,
    )


def print_deploy_summary(summary: DeploySummary, *, remote: str | None) -> None:
    print(
        "Deploy: "
        f"changed={summary.changed} "
        f"skipped={summary.skipped} "
        f"uploaded={summary.uploaded} "
        f"deleted={summary.deleted} "
        f"remote={remote}",
        file=sys.stdout,
    )


def print_deleted_files(summary: DeploySummary, *, dry_run: bool) -> None:
    """List every deleted remote file when there is no full-screen UI to show them (piped output, rebuild-metadata).

    Uses the same rows as the UI's final report.
    """
    if not summary.deleted_files:
        return
    color = color_enabled(sys.stdout)
    for relpath, reason in summary.deleted_files:
        print(deleted_file_line(relpath, reason, dry_run=dry_run, color=color), file=sys.stdout)
    print(f"{DIM}{DELETE_LEGEND}{RESET}" if color else DELETE_LEGEND, file=sys.stdout)


def finalize_deploy_plan_for_conversion(
    plan: DeployJobPlan,
    ffprobe_bin: str,
    ffmpeg_bin: str,
    staging_root: Path,
    encoding_by_job: dict[tuple[str, str], VideoEncodingConfig],
) -> DeployJobPlan:
    output_suffix, encoding = resolve_video_handling_plan(ffprobe_bin, ffmpeg_bin, plan.job.source_video)
    job = replace(plan.job, output_file=plan.job.output_file.with_suffix(output_suffix))
    encoding_by_job[(str(job.source_video), str(job.output_file))] = encoding
    return replace(
        plan,
        job=job,
        output_key=relative_output_key(staging_root, job.output_file),
        cover_key=relative_output_key(staging_root, cover_output_path_for_job(job)),
    )


def replace_deploy_context_plan(plans: list[DeployJobPlan], original_job: Job, next_plan: DeployJobPlan) -> None:
    for index, plan in enumerate(plans):
        if plan.job is original_job or str(plan.job.source_video) == str(next_plan.job.source_video):
            plans[index] = next_plan
            return
    plans.append(next_plan)


def discover_selected_jobs(options: RunOptions, log=None) -> tuple[list[Job], list[ConversionResult], list[str]]:
    if options.entries:
        requested = entry_categories(options.archive, options.entries) or CATEGORY_ORDER
    else:
        requested = options.categories or CATEGORY_ORDER
    _, categories, _ = resolve_input_roots(options.archive, normalize_categories(requested))
    started = time.monotonic()
    if log is not None:
        log(f"scanning archive {options.archive}: {', '.join(categories)}")
    jobs, failures = discover_jobs(options.archive, categories, options.output)
    if options.entries:
        jobs = select_entries(jobs, options.archive, options.entries)
        selected_dirs = {job.entry_dir.resolve() for job in jobs}
        failures = [result for result in failures if result.entry_dir.resolve() in selected_dirs or result.entry_dir.resolve() in options.entries]
    if log is not None:
        entries = len({job.entry_dir for job in jobs})
        problems = f", {len(failures)} entr(ies) with problems" if failures else ""
        log(f"found {len(jobs)} video(s) in {entries} entr(ies){problems} ({time.monotonic() - started:.1f}s)")
    return jobs, failures, list(categories)


def entry_categories(archive: Path, entries: tuple[Path, ...]) -> tuple[str, ...]:
    categories: list[str] = []
    for entry in entries:
        try:
            head = entry.relative_to(archive).parts[0]
        except (ValueError, IndexError):
            continue
        category = _category_name(Path(head))
        if category is not None and category not in categories:
            categories.append(category)
    return tuple(categories)


def run_rebuild_metadata(options: RunOptions, log) -> int:
    jobs, discovery_failures, categories = discover_selected_jobs(options, log)
    if discovery_failures:
        messages = "; ".join(result.message for result in discovery_failures)
        raise ConversionError(f"cannot rebuild metadata with discovery failures: {messages}")
    summary = bootstrap_deploy_from_existing_outputs(
        rclone_bin=options.rclone,
        remote_root=options.remote,
        input_root=options.archive,
        staging_root=options.output,
        jobs=jobs,
        prune_scope=categories,
        ffprobe_bin=options.ffprobe,
        ffmpeg_bin=options.ffmpeg,
        dry_run=options.dry_run,
        keep_staging=True,  # in recovery OUTPUT is the only copy of the converted files
        log=log,
    )
    print(
        "Rebuild: "
        f"items={summary.changed} "
        f"uploaded={summary.uploaded} "
        f"deleted={summary.deleted} "
        f"metadata={summary.metadata_path}",
        file=sys.stdout,
    )
    print_deleted_files(summary, dry_run=options.dry_run)
    return 0


def new_staging_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix=STAGING_PREFIX))


def read_deploy_pointer(archive: Path) -> dict[str, str] | None:
    try:
        pointer = json.loads((archive / DEPLOY_POINTER_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(pointer, dict) or not all(isinstance(pointer.get(key), str) for key in ("remote", "staging")):
        return None
    return pointer


def _is_our_staging(path: Path) -> bool:
    # A pointer is a plain file anyone can edit, so only ever reuse or delete dirs this tool created.
    return path.name.startswith(STAGING_PREFIX) and path.is_dir()


def clear_deploy_pointer(archive: Path) -> None:
    (archive / DEPLOY_POINTER_FILENAME).unlink(missing_ok=True)


def choose_staging(options: RunOptions, stack: ExitStack, log) -> tuple[Path, bool]:
    """Pick the deploy staging dir: resume an unfinished run's temp dir, or start a fresh one.

    Returns the dir and whether this call created it.
    """
    pointer = read_deploy_pointer(options.archive)
    if pointer is not None:
        previous = Path(pointer["staging"])
        if pointer["remote"] == options.remote and _is_our_staging(previous):
            return previous, False
        if pointer["remote"] != options.remote:
            log(f"unfinished deploy to {pointer['remote']} is discarded; starting again")
            if not options.dry_run and _is_our_staging(previous):
                shutil.rmtree(previous)
        else:
            log("previous run's staging is gone; starting again")
    if options.dry_run:
        return Path(stack.enter_context(tempfile.TemporaryDirectory(prefix=STAGING_PREFIX))), False
    staging = new_staging_dir()
    pointer_path = options.archive / DEPLOY_POINTER_FILENAME
    pointer_path.write_text(json.dumps({"remote": options.remote, "staging": str(staging)}, indent=2) + "\n", encoding="utf-8")
    return staging, True


def main(argv: Iterable[str] | None = None) -> int:
    options = parse_args(sys.argv[1:] if argv is None else argv)
    with ExitStack() as stack:
        return _run(options, stack)


def _run(options: RunOptions, stack: ExitStack) -> int:
    ui: TerminalUI | None = None
    deploy_context = None
    fresh_staging = False
    interrupted_message = "Interrupted. Run the same command again to resume." if options.command == "deploy" else "Interrupted."

    def early_log(message: str) -> None:
        print(f"Deploy: {message}", file=sys.stdout, flush=True)

    try:
        validate_cli_paths(options)
        status_log = early_log if options.command != "convert" else None
        if status_log is not None:
            status_log("checking ffmpeg and ffprobe")
        validate_tools(options.ffmpeg, options.ffprobe)
        if options.command == "rebuild-metadata":
            return run_rebuild_metadata(options, early_log)
        if options.command == "deploy":
            validate_remote_root(str(options.remote))
            staging, fresh_staging = choose_staging(options, stack, early_log)
            options = replace(options, output=staging)
        input_dir, output_dir = options.archive, options.output
        jobs, discovery_failures, categories = discover_selected_jobs(options, status_log)
        scope = run_scope(output_dir, jobs, categories, options.entries)
        encoding_by_job: dict[tuple[str, str], VideoEncodingConfig] = {}
        if options.command == "convert":
            planned_jobs = []
            for job in jobs:
                output_suffix, encoding = resolve_video_handling_plan(options.ffprobe, options.ffmpeg, job.source_video)
                planned_job = replace(job, output_file=job.output_file.with_suffix(output_suffix))
                planned_jobs.append(planned_job)
                encoding_by_job[(str(planned_job.source_video), str(planned_job.output_file))] = encoding
            jobs = planned_jobs
        if options.command == "deploy":
            deploy_context = prepare_deploy_context(
                rclone_bin=options.rclone,
                remote_root=options.remote,
                input_root=input_dir,
                staging_root=output_dir,
                jobs=jobs,
                prune_scope=scope,
                force=options.overwrite,
                dry_run=options.dry_run,
                log=early_log,
            )
            jobs = [plan.job for plan in deploy_context.plans]
        ui = TerminalUI(
            repo_root=input_dir,
            categories=categories,
            jobs=jobs,
            discovery_failures=discovery_failures,
            dry_run=options.dry_run,
            stream=sys.stdout,
        )
        ui.start()
        ui.set_phase("Ready", f"{len(jobs)} file(s) queued")

        if not jobs and not discovery_failures:
            ui.set_phase("Idle", "No convertible entries found")
            ui.set_final_summary(converted=0, skipped=0, failed=len(discovery_failures))
            ui.finish(1)
            if deploy_context is not None and not options.dry_run and not deploy_context.resumed:
                # Nothing was planned or uploaded, so leave no staging or pointer behind.
                clean_staging(output_dir, input_root=input_dir)
                clear_deploy_pointer(input_dir)
            print("No convertible entries found.", file=sys.stderr)
            return 1
    except ConversionError as exc:
        if ui is not None:
            ui.close()
        if fresh_staging and deploy_context is None:
            # Failed before anything was staged (e.g. remote unreachable): there is nothing to resume.
            clean_staging(options.output, input_root=options.archive)
            clear_deploy_pointer(options.archive)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        if ui is not None:
            ui.close()
        print(interrupted_message, file=sys.stderr)
        return 130

    try:
        failures = len(discovery_failures)
        skipped = 0
        converted = 0
        metadata_discovered = 0
        metadata_reused = 0
        metadata_probed = 0
        metadata_failed = 0
        metadata_pruned = 0
        metadata_pruned_keys: tuple[str, ...] = ()
        metadata_output_path: Path | None = None

        for result in discovery_failures:
            ui.print_result_line(result)

        deploy_plan_by_job = {}
        expected_metadata_keys = {
            cache_key_for_path(output_dir, job.output_file)
            for job in jobs
        }
        if deploy_context is not None:
            deploy_plan_by_job = {
                (str(plan.job.source_video), str(plan.job.output_file)): plan
                for plan in deploy_context.plans
            }

        for job in jobs:
            deploy_plan = deploy_plan_by_job.get((str(job.source_video), str(job.output_file)))
            if deploy_context is not None and deploy_plan is not None and not deploy_plan.changed:
                result = ConversionResult.for_job(job, success=True, skipped=True, message=deploy_plan.reason)
                ui.mark_result(job, result)
                ui.print_result_line(result)
                skipped += 1
                continue

            if deploy_context is not None and options.dry_run:
                if deploy_plan is not None and deploy_plan.needs_conversion:
                    deploy_plan = finalize_deploy_plan_for_conversion(
                        deploy_plan,
                        options.ffprobe,
                        options.ffmpeg,
                        deploy_context.staging_root,
                        encoding_by_job,
                    )
                    replace_deploy_context_plan(deploy_context.plans, job, deploy_plan)
                result = ConversionResult.for_job(
                    job,
                    success=True,
                    message=f"deploy dry run ({deploy_plan.reason if deploy_plan else 'planned'})",
                )
                ui.mark_result(job, result)
                ui.print_result_line(result)
                converted += 1
                continue

            if deploy_context is not None and deploy_plan is not None and deploy_plan.subtitles_only:
                ui.mark_running(job)
                result = ConversionResult.for_job(job)
                try:
                    subtitle_keys = backfill_subtitles(
                        options.rclone,
                        deploy_context,
                        deploy_plan,
                        ffmpeg_bin=options.ffmpeg,
                        ffprobe_bin=options.ffprobe,
                        log=early_log if not ui.enabled else None,
                    )
                except ConversionError as exc:
                    result.message = str(exc)
                    failures += 1
                else:
                    result.success = True
                    result.skipped = True
                    result.message = f"subtitles backfilled ({len(subtitle_keys)})"
                    skipped += 1
                ui.mark_result(job, result)
                ui.print_result_line(result)
                continue

            if deploy_context is not None and deploy_plan is not None and not deploy_plan.needs_conversion:
                result = ConversionResult.for_job(job, success=True, skipped=True, message=deploy_plan.reason)
                ui.mark_result(job, result)
                ui.print_result_line(result)
                skipped += 1
                continue

            display_job = job
            work_job = job
            if deploy_context is not None and deploy_plan is not None and deploy_plan.needs_conversion:
                deploy_plan = finalize_deploy_plan_for_conversion(
                    deploy_plan,
                    options.ffprobe,
                    options.ffmpeg,
                    deploy_context.staging_root,
                    encoding_by_job,
                )
                replace_deploy_context_plan(deploy_context.plans, job, deploy_plan)
                work_job = deploy_plan.job
            ui.mark_running(display_job)
            result = convert_job(
                work_job,
                ffmpeg_bin=options.ffmpeg,
                ffprobe_bin=options.ffprobe,
                encoding=encoding_by_job[(str(work_job.source_video), str(work_job.output_file))],
                overwrite=options.overwrite or deploy_context is not None,
                dry_run=options.dry_run,
            )
            ui.mark_result(display_job, result)
            ui.print_result_line(result)
            if result.success:
                if result.skipped:
                    skipped += 1
                else:
                    converted += 1
                    if deploy_context is not None and deploy_plan is not None:
                        record_pending_deploy_item(
                            deploy_context,
                            deploy_plan,
                            ffprobe_bin=options.ffprobe,
                            subtitles=result.subtitles,
                        )
                        metadata_discovered += 1
                        metadata_output_path = deploy_context.metadata_path
                        metadata_probed += 1
                if deploy_context is None and not options.dry_run:
                    output_key = cache_key_for_path(output_dir, result.output_file)
                    deploy_item = {
                        "source": source_signature(input_dir, job.source_video),
                        "outputPath": output_key,
                        "coverPath": cache_key_for_path(output_dir, cover_output_path_for_job(job)),
                    }
                    if result.subtitles is not None:
                        deploy_item[SUBTITLES_KEY] = subtitle_metadata_entries(output_dir, result.subtitles)
                    update_summary = update_metadata_cache_item_with_tracking(
                        output_dir,
                        result.output_file,
                        ffprobe_bin=options.ffprobe,
                        deploy_item=deploy_item,
                    )
                    metadata_discovered += 1
                    metadata_output_path = update_summary.output_path
                    if update_summary.reused:
                        metadata_reused += 1
                    if update_summary.probed:
                        metadata_probed += 1
                    if update_summary.failed:
                        metadata_failed += 1
            else:
                failures += 1

        deploy_summary = None

        def deploy_log(message: str) -> None:
            ui.set_phase("Deploy", message)
            if not ui.enabled:
                print(f"Deploy: {message}", file=sys.stdout, flush=True)

        if deploy_context is not None and not failures:
            ui.set_phase("Deploy", "Uploading staged files")
            deploy_summary = finalize_deploy(
                rclone_bin=options.rclone,
                context=deploy_context,
                dry_run=options.dry_run,
                keep_staging=False,
                input_root=input_dir,
                log=deploy_log,
            )
            if not options.dry_run:
                clear_deploy_pointer(input_dir)
        elif deploy_context is None and not options.dry_run and output_dir.is_dir() and not failures:
            prune_summary = prune_unexpected_metadata_cache_items(
                output_dir,
                expected_metadata_keys,
                scope=scope,
                delete_files=True,
            )
            metadata_pruned = prune_summary.pruned
            metadata_pruned_keys = prune_summary.pruned_keys
            metadata_output_path = metadata_output_path or prune_summary.output_path
        elif deploy_context is None and not options.dry_run and output_dir.is_dir():
            prune_summary = prune_missing_metadata_cache_items(output_dir, scope=scope)
            metadata_pruned = prune_summary.pruned
            metadata_pruned_keys = prune_summary.pruned_keys
            metadata_output_path = metadata_output_path or prune_summary.output_path

        ui.add_pruned_items(metadata_pruned_keys)
        if deploy_summary is not None:
            ui.add_deleted_items(deploy_summary.deleted_files)
        ui.set_final_summary(converted=converted, skipped=skipped, failed=failures, pruned=metadata_pruned)
        ui.finish(1 if failures else 0)
        print(
            f"Summary: converted={converted} skipped={skipped} failed={failures} pruned={metadata_pruned}",
            file=sys.stderr if failures else sys.stdout,
        )
        if metadata_output_path is not None:
            print_metadata_status(
                discovered=metadata_discovered,
                reused=metadata_reused,
                probed=metadata_probed,
                failed=metadata_failed,
                pruned=metadata_pruned,
                output_path=metadata_output_path,
                failures=failures,
            )
        if deploy_summary is not None:
            print_deploy_summary(deploy_summary, remote=options.remote)
            if not ui.enabled:  # the full-screen UI already listed them in its final report
                print_deleted_files(deploy_summary, dry_run=options.dry_run)
        return 1 if failures else 0
    except ConversionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(interrupted_message, file=sys.stderr)
        return 130
    finally:
        if ui is not None:
            ui.close()
