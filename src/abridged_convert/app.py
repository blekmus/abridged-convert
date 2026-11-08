from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from abridged_convert.common import normalize_categories, source_signature
from abridged_convert.constants import CATEGORY_ORDER
from abridged_convert.conversion import convert_job
from abridged_convert.deploy import (
    DeployJobPlan,
    DeploySummary,
    backfill_subtitles,
    bootstrap_deploy_from_existing_outputs,
    finalize_deploy,
    prepare_deploy_context,
    record_pending_deploy_item,
    relative_output_key,
)
from abridged_convert.discovery import discover_jobs, resolve_input_roots
from abridged_convert.media import cover_output_path_for_job, resolve_video_handling_plan, validate_tools
from abridged_convert.metadata import (
    SUBTITLES_KEY,
    cache_key_for_path,
    prune_missing_metadata_cache_items,
    prune_unexpected_metadata_cache_items,
    subtitle_metadata_entries,
    update_metadata_cache_item_with_tracking,
)
from abridged_convert.models import ConversionError, ConversionResult, VideoEncodingConfig
from abridged_convert.ui import TerminalUI


CLI_DESCRIPTION = "Convert entries from the abridged archive layout into mirrored web outputs."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=CLI_DESCRIPTION)
    parser.add_argument(
        "input_dir",
        type=Path,
        nargs="?",
        help="Input directory containing Series/Shots/Shorts/Songs, or a single category directory.",
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
        "--overwrite",
        action="store_true",
        help="Replace existing output files instead of skipping them.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover and report planned work without writing output files.",
    )
    parser.add_argument(
        "--deploy",
        metavar="REMOTE",
        help="Upload converted outputs to an rclone remote and clean local staging after success.",
    )
    parser.add_argument(
        "--rclone",
        default=shutil.which("rclone") or "rclone",
        help="Path to the rclone executable used by --deploy.",
    )
    parser.add_argument(
        "--deploy-dry-run",
        action="store_true",
        help="Plan deploy uploads and deletes without mutating the remote or writing converted outputs.",
    )
    parser.add_argument(
        "--keep-staging",
        action="store_true",
        help="Keep deploy staging files after a successful upload.",
    )
    parser.add_argument(
        "--bootstrap-deploy",
        action="store_true",
        help="Generate deploy metadata/state from existing output files instead of converting.",
    )
    return parser


def parse_args(argv: Iterable[str]) -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    parser = build_parser()
    return parser, parser.parse_args(list(argv))


def resolve_cli_dirs(parser: argparse.ArgumentParser, args: argparse.Namespace) -> tuple[Path, Path]:
    input_dir = args.input_dir or args.legacy_input_dir
    output_dir = args.output_dir or args.legacy_output_dir
    if input_dir is None:
        parser.error("the following arguments are required: input_dir, output_dir")
    if output_dir is None:
        if args.legacy_input_dir is None:
            parser.error("the following arguments are required: input_dir, output_dir")
        output_dir = input_dir / "Web"
    return input_dir.resolve(), output_dir.resolve()


def source_signature_root_for_input(input_dir: Path, category_roots: dict[str, Path]) -> Path:
    resolved_input = input_dir.resolve()
    if any(category_root.resolve() == resolved_input for category_root in category_roots.values()):
        return resolved_input.parent
    return resolved_input


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


def replace_deploy_context_plan(plans: list[DeployJobPlan], original_job: object, next_plan: DeployJobPlan) -> None:
    for index, plan in enumerate(plans):
        if plan.job is original_job or str(plan.job.source_video) == str(next_plan.job.source_video):
            plans[index] = next_plan
            return
    plans.append(next_plan)


def main(argv: Iterable[str] | None = None) -> int:
    parser, args = parse_args(argv or sys.argv[1:])
    input_dir, output_dir = resolve_cli_dirs(parser, args)
    ui: TerminalUI | None = None
    deploy_context = None
    operation_dry_run = args.dry_run or args.deploy_dry_run

    def early_log(message: str) -> None:
        print(f"Deploy: {message}", file=sys.stdout, flush=True)

    try:
        validate_tools(args.ffmpeg, args.ffprobe)
        requested_categories = normalize_categories(args.categories or CATEGORY_ORDER)
        category_roots, categories, _ = resolve_input_roots(input_dir, requested_categories)
        source_signature_root = source_signature_root_for_input(input_dir, category_roots)
        jobs, discovery_failures = discover_jobs(input_dir, categories, output_dir)
        encoding_by_job: dict[tuple[str, str], VideoEncodingConfig] = {}
        if not args.bootstrap_deploy and not args.deploy:
            planned_jobs = []
            for job in jobs:
                output_suffix, encoding = resolve_video_handling_plan(args.ffprobe, args.ffmpeg, job.source_video)
                planned_job = replace(job, output_file=job.output_file.with_suffix(output_suffix))
                planned_jobs.append(planned_job)
                encoding_by_job[(str(planned_job.source_video), str(planned_job.output_file))] = encoding
            jobs = planned_jobs
        if args.bootstrap_deploy:
            if discovery_failures:
                messages = "; ".join(result.message for result in discovery_failures)
                raise ConversionError(f"cannot bootstrap deploy metadata with discovery failures: {messages}")
            bootstrap_summary = bootstrap_deploy_from_existing_outputs(
                rclone_bin=args.rclone,
                remote_root=args.deploy,
                input_root=input_dir,
                source_signature_root=source_signature_root,
                staging_root=output_dir,
                jobs=jobs,
                prune_categories=categories,
                ffprobe_bin=args.ffprobe,
                ffmpeg_bin=args.ffmpeg,
                dry_run=operation_dry_run,
                keep_staging=args.keep_staging,
                log=early_log,
            )
            print(
                "Bootstrap: "
                f"items={bootstrap_summary.changed} "
                f"uploaded={bootstrap_summary.uploaded} "
                f"deleted={bootstrap_summary.deleted} "
                f"metadata={bootstrap_summary.metadata_path}",
                file=sys.stdout,
            )
            return 0
        if args.deploy:
            deploy_context = prepare_deploy_context(
                rclone_bin=args.rclone,
                remote_root=args.deploy,
                input_root=input_dir,
                source_signature_root=source_signature_root,
                staging_root=output_dir,
                jobs=jobs,
                prune_categories=categories,
                force=args.overwrite,
                log=early_log,
            )
            jobs = [plan.job for plan in deploy_context.plans]
        ui = TerminalUI(
            repo_root=input_dir,
            categories=categories,
            encoding=None,
            jobs=jobs,
            discovery_failures=discovery_failures,
            persist_final_report=operation_dry_run,
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
        successful_deploy_outputs: set[str] = set()
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
                result = ConversionResult(
                    category=job.category,
                    entry_dir=job.entry_dir,
                    source_video=job.source_video,
                    output_file=job.output_file,
                    success=True,
                    skipped=True,
                    message=deploy_plan.reason,
                )
                ui.mark_result(job, result)
                ui.print_result_line(result)
                skipped += 1
                continue

            if deploy_context is not None and operation_dry_run:
                if deploy_plan is not None and deploy_plan.needs_conversion:
                    deploy_plan = finalize_deploy_plan_for_conversion(
                        deploy_plan,
                        args.ffprobe,
                        args.ffmpeg,
                        deploy_context.staging_root,
                        encoding_by_job,
                    )
                    replace_deploy_context_plan(deploy_context.plans, job, deploy_plan)
                result = ConversionResult(
                    category=job.category,
                    entry_dir=job.entry_dir,
                    source_video=job.source_video,
                    output_file=job.output_file,
                    success=True,
                    message=f"deploy dry run ({deploy_plan.reason if deploy_plan else 'planned'})",
                )
                ui.mark_result(job, result)
                ui.print_result_line(result)
                converted += 1
                continue

            if deploy_context is not None and deploy_plan is not None and deploy_plan.subtitles_only:
                ui.mark_running(job)
                result = ConversionResult(
                    category=job.category,
                    entry_dir=job.entry_dir,
                    source_video=job.source_video,
                    output_file=job.output_file,
                )
                try:
                    subtitle_keys = backfill_subtitles(
                        args.rclone,
                        deploy_context,
                        deploy_plan,
                        ffmpeg_bin=args.ffmpeg,
                        ffprobe_bin=args.ffprobe,
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
                    successful_deploy_outputs.add(deploy_plan.output_key)
                ui.mark_result(job, result)
                ui.print_result_line(result)
                continue

            if deploy_context is not None and deploy_plan is not None and not deploy_plan.needs_conversion:
                result = ConversionResult(
                    category=job.category,
                    entry_dir=job.entry_dir,
                    source_video=job.source_video,
                    output_file=job.output_file,
                    success=True,
                    skipped=True,
                    message=deploy_plan.reason,
                )
                ui.mark_result(job, result)
                ui.print_result_line(result)
                skipped += 1
                successful_deploy_outputs.add(deploy_plan.output_key)
                continue

            display_job = job
            work_job = job
            if deploy_context is not None and deploy_plan is not None and deploy_plan.needs_conversion:
                deploy_plan = finalize_deploy_plan_for_conversion(
                    deploy_plan,
                    args.ffprobe,
                    args.ffmpeg,
                    deploy_context.staging_root,
                    encoding_by_job,
                )
                replace_deploy_context_plan(deploy_context.plans, job, deploy_plan)
                work_job = deploy_plan.job
            ui.mark_running(display_job)
            result = convert_job(
                work_job,
                ffmpeg_bin=args.ffmpeg,
                ffprobe_bin=args.ffprobe,
                encoding=encoding_by_job[(str(work_job.source_video), str(work_job.output_file))],
                overwrite=args.overwrite or deploy_context is not None,
                dry_run=args.dry_run,
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
                            ffprobe_bin=args.ffprobe,
                            subtitles=result.subtitles,
                        )
                        metadata_discovered += 1
                        metadata_output_path = deploy_context.metadata_path
                        metadata_probed += 1
                        successful_deploy_outputs.add(deploy_plan.output_key)
                if deploy_context is None and not args.dry_run:
                    output_key = cache_key_for_path(output_dir, result.output_file)
                    deploy_item = {
                        "source": source_signature(source_signature_root, job.source_video),
                        "outputPath": output_key,
                        "coverPath": cache_key_for_path(output_dir, cover_output_path_for_job(job)),
                    }
                    if result.subtitles is not None:
                        deploy_item[SUBTITLES_KEY] = subtitle_metadata_entries(output_dir, result.subtitles)
                    update_summary = update_metadata_cache_item_with_tracking(
                        output_dir,
                        result.output_file,
                        ffprobe_bin=args.ffprobe,
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
                rclone_bin=args.rclone,
                context=deploy_context,
                ffprobe_bin=args.ffprobe,
                successful_output_keys=successful_deploy_outputs,
                dry_run=operation_dry_run,
                keep_staging=args.keep_staging,
                input_root=input_dir,
                log=deploy_log,
            )
        elif deploy_context is None and not args.dry_run and output_dir.is_dir() and not failures:
            prune_summary = prune_unexpected_metadata_cache_items(
                output_dir,
                expected_metadata_keys,
                categories=categories,
                delete_files=True,
            )
            metadata_pruned = prune_summary.pruned
            metadata_pruned_keys = prune_summary.pruned_keys
            metadata_output_path = metadata_output_path or prune_summary.output_path
        elif deploy_context is None and not args.dry_run and output_dir.is_dir():
            prune_summary = prune_missing_metadata_cache_items(output_dir, categories=categories)
            metadata_pruned = prune_summary.pruned
            metadata_pruned_keys = prune_summary.pruned_keys
            metadata_output_path = metadata_output_path or prune_summary.output_path

        ui.add_pruned_items(metadata_pruned_keys)
        ui.set_final_summary(converted=converted, skipped=skipped, failed=failures)
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
            print_deploy_summary(deploy_summary, remote=args.deploy)
        return 1 if failures else 0
    except ConversionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    finally:
        if ui is not None:
            ui.close()
