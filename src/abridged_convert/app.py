from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Iterable

from abridged_convert.constants import CATEGORY_ORDER
from abridged_convert.conversion import convert_job
from abridged_convert.discovery import discover_jobs, normalize_categories, resolve_input_roots
from abridged_convert.media import resolve_video_encoding_config, validate_tools
from abridged_convert.models import ConversionError
from abridged_convert.ui import TerminalUI


CLI_DESCRIPTION = "Convert entries from the abridged archive layout into mirrored MP4 outputs."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=CLI_DESCRIPTION)
    parser.add_argument(
        "input_dir",
        type=Path,
        nargs="?",
        help="Input directory containing Shots/Shorts/Series, or a single category directory.",
    )
    parser.add_argument(
        "output_dir",
        type=Path,
        nargs="?",
        help="Output root directory, typically named Web.",
    )
    parser.add_argument(
        "--repo-root",
        dest="legacy_input_dir",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--output-root",
        dest="legacy_output_dir",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--category",
        dest="categories",
        action="append",
        help="Limit conversion to a category. Repeat the flag to include multiple categories.",
    )
    parser.add_argument(
        "--ffmpeg",
        default=shutil.which("ffmpeg") or "ffmpeg",
        help="Path to the ffmpeg executable used for video conversion and cover sidecar generation.",
    )
    parser.add_argument(
        "--ffprobe",
        default=shutil.which("ffprobe") or "ffprobe",
        help="Path to the ffprobe executable used to inspect streams and metadata before conversion.",
    )
    parser.add_argument(
        "--video-mode",
        choices=("auto", "nvidia", "cpu"),
        default="auto",
        help="Video encoder selection: auto prefers NVIDIA NVENC when available, nvidia requires it, cpu uses libx264.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing output files instead of skipping them.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover and report planned work without writing output files.",
    )
    return parser


def parse_args(argv: Iterable[str]) -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    parser = build_parser()
    return parser, parser.parse_args(list(argv))


def resolve_cli_dirs(parser: argparse.ArgumentParser, args: argparse.Namespace) -> tuple[Path, Path]:
    input_dir = args.input_dir or args.legacy_input_dir
    output_dir = args.output_dir or args.legacy_output_dir
    if input_dir is None or output_dir is None:
        parser.error("the following arguments are required: input_dir, output_dir")
    return input_dir.resolve(), output_dir.resolve()


def main(argv: Iterable[str] | None = None) -> int:
    parser, args = parse_args(argv or sys.argv[1:])
    input_dir, output_dir = resolve_cli_dirs(parser, args)
    ui: TerminalUI | None = None
    try:
        validate_tools(args.ffmpeg, args.ffprobe)
        encoding = resolve_video_encoding_config(args.ffmpeg, args.video_mode)
        requested_categories = normalize_categories(args.categories or CATEGORY_ORDER)
        _, categories, _ = resolve_input_roots(input_dir, requested_categories)
        jobs, discovery_failures = discover_jobs(input_dir, categories, output_dir)
        ui = TerminalUI(
            repo_root=input_dir,
            categories=categories,
            encoding=encoding,
            jobs=jobs,
            discovery_failures=discovery_failures,
            persist_final_report=args.dry_run,
            stream=sys.stdout,
        )
        ui.start()
        ui.set_phase("Ready", f"{len(jobs)} file(s) queued")

        if not jobs and not discovery_failures:
            ui.set_phase("Idle", "No convertible entries found")
            ui.set_final_summary(converted=0, skipped=0, failed=len(discovery_failures))
            ui.finish(1)
            print("No convertible entries found.", file=sys.stderr)
            return 1
    except ConversionError as exc:
        if ui is not None:
            ui.close()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        if ui is not None:
            ui.close()
        print("Interrupted.", file=sys.stderr)
        return 130

    try:
        failures = len(discovery_failures)
        skipped = 0
        converted = 0

        for result in discovery_failures:
            ui.print_result_line(result)

        for job in jobs:
            ui.mark_running(job)
            result = convert_job(
                job,
                ffmpeg_bin=args.ffmpeg,
                ffprobe_bin=args.ffprobe,
                encoding=encoding,
                overwrite=args.overwrite,
                dry_run=args.dry_run,
            )
            ui.mark_result(job, result)
            ui.print_result_line(result)
            if result.success:
                if result.skipped:
                    skipped += 1
                else:
                    converted += 1
            else:
                failures += 1

        ui.set_final_summary(converted=converted, skipped=skipped, failed=failures)
        ui.finish(1 if failures else 0)
        print(
            f"Summary: converted={converted} skipped={skipped} failed={failures}",
            file=sys.stderr if failures else sys.stdout,
        )
        return 1 if failures else 0
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    finally:
        if ui is not None:
            ui.close()
