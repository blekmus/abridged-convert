from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from abridged_convert.common import normalize_categories, path_is_in_categories, source_signature
from abridged_convert.constants import CATEGORY_ORDER
from abridged_convert.media import cover_output_path_for_job
from abridged_convert.metadata import (
    METADATA_CACHE_FILENAME,
    build_metadata_cache_item,
    load_cache_deploy_items,
    load_cache_items,
    write_cache,
)
from abridged_convert.models import ConversionError, Job


DeployLogger = Callable[[str], None]


@dataclass(frozen=True)
class DeployJobPlan:
    job: Job
    output_key: str
    cover_key: str
    source_signature: dict[str, Any]
    changed: bool
    needs_conversion: bool
    reason: str
    staged_metadata_item: dict[str, Any] | None = None


@dataclass(frozen=True)
class DeployContext:
    remote_root: str
    staging_root: Path
    metadata_path: Path
    state_path: Path
    metadata_items: dict[str, dict[str, Any]]
    state_items: dict[str, dict[str, Any]]
    remote_files: set[str]
    plans: list[DeployJobPlan]
    prune_categories: tuple[str, ...]


@dataclass(frozen=True)
class DeploySummary:
    uploaded: int
    deleted: int
    skipped: int
    changed: int
    metadata_path: Path
    state_path: Path


def bootstrap_deploy_from_existing_outputs(
    *,
    rclone_bin: str,
    remote_root: str | None,
    input_root: Path,
    staging_root: Path,
    jobs: list[Job],
    prune_categories: Iterable[str],
    ffprobe_bin: str,
    dry_run: bool,
    keep_staging: bool,
    log: DeployLogger | None = None,
) -> DeploySummary:
    resolved_staging = staging_root.resolve()
    if not resolved_staging.is_dir():
        raise ConversionError(f"bootstrap output directory does not exist: {staging_root}")
    emit(log, f"bootstrap using existing outputs in {resolved_staging}")

    metadata_path = resolved_staging / METADATA_CACHE_FILENAME
    state_path = metadata_path
    plans = [
        DeployJobPlan(
            job=job,
            output_key=relative_output_key(resolved_staging, job.output_file),
            cover_key=relative_output_key(resolved_staging, cover_output_path_for_job(job)),
            source_signature=source_signature(input_root, job.source_video),
            changed=True,
            needs_conversion=False,
            reason="bootstrap",
        )
        for job in jobs
    ]
    uploaded_paths: list[str] = []
    metadata_items: dict[str, dict[str, Any]] = {}
    deploy_items: dict[str, dict[str, Any]] = {}
    now = datetime.now(tz=UTC).isoformat()

    emit(log, f"bootstrap discovered {len(plans)} expected item(s)")
    for index, plan in enumerate(plans, start=1):
        if not plan.job.output_file.is_file():
            raise ConversionError(f"bootstrap output file is missing: {plan.job.output_file}")
        cover_path = cover_output_path_for_job(plan.job)
        if not cover_path.is_file():
            raise ConversionError(f"bootstrap cover file is missing: {cover_path}")

        if not dry_run:
            emit(log, f"probing metadata {index}/{len(plans)}: {plan.output_key}")
            deploy_item = deploy_tracking_item(plan, deployed_at=now)
            item = build_metadata_cache_item(
                plan.job.output_file,
                ffprobe_bin=ffprobe_bin,
                deploy_item=deploy_item,
            )
            if item is None:
                raise ConversionError(f"failed to probe metadata for bootstrap output: {plan.job.output_file}")
            metadata_items[plan.output_key] = item
            deploy_items[plan.output_key] = deploy_item
        uploaded_paths.extend([plan.output_key, plan.cover_key])

    prune_scope = normalize_categories(prune_categories)
    expected_files = expected_remote_files(plans)
    remote_files: set[str] = set()
    safe_remote_root = None
    if remote_root is not None:
        safe_remote_root = validate_remote_root(remote_root)
        emit(log, f"listing remote files: {safe_remote_root}")
        remote_files = list_remote_files(rclone_bin, safe_remote_root)
        emit(log, f"remote has {len(remote_files)} file(s)")

    dangling = sorted(dangling_remote_files(remote_files, expected_files, prune_scope))
    if dry_run:
        emit(log, f"dry run: would upload {len(uploaded_paths) + (1 if remote_root else 0)} file(s)")
        emit(log, f"dry run: would delete {len(dangling)} dangling remote file(s)")
        return DeploySummary(
            uploaded=len(uploaded_paths) + (1 if remote_root else 0),
            deleted=len(dangling),
            skipped=0,
            changed=len(plans),
            metadata_path=metadata_path,
            state_path=state_path,
        )

    emit(log, f"writing {METADATA_CACHE_FILENAME}")
    write_cache(metadata_path, metadata_items, deploy_items=deploy_items)

    if safe_remote_root is not None:
        uploaded_paths.append(METADATA_CACHE_FILENAME)
        upload_remote_files(rclone_bin, safe_remote_root, resolved_staging, uploaded_paths, log=log)
        delete_remote_files(rclone_bin, safe_remote_root, dangling, log=log)
        if not keep_staging:
            emit(log, f"cleaning staging directory: {resolved_staging}")
            clean_staging(resolved_staging, input_root=input_root)
        else:
            emit(log, "keeping staging directory")

    return DeploySummary(
        uploaded=len(uploaded_paths) if safe_remote_root is not None else 0,
        deleted=len(dangling) if safe_remote_root is not None else 0,
        skipped=0,
        changed=len(plans),
        metadata_path=metadata_path,
        state_path=state_path,
    )


def prepare_deploy_context(
    *,
    rclone_bin: str,
    remote_root: str,
    input_root: Path,
    staging_root: Path,
    jobs: list[Job],
    prune_categories: Iterable[str],
    force: bool,
    log: DeployLogger | None = None,
) -> DeployContext:
    resolved_staging = staging_root.resolve()
    resolved_staging.mkdir(parents=True, exist_ok=True)
    safe_remote_root = validate_remote_root(remote_root)
    staged_metadata_items = load_cache_items(resolved_staging / METADATA_CACHE_FILENAME)
    staged_state_items = load_cache_deploy_items(resolved_staging / METADATA_CACHE_FILENAME)

    emit(log, f"preparing staging directory: {resolved_staging}")
    emit(log, f"listing remote files: {safe_remote_root}")
    remote_files = list_remote_files(rclone_bin, safe_remote_root)
    emit(log, f"remote has {len(remote_files)} file(s)")
    metadata_path = resolved_staging / METADATA_CACHE_FILENAME
    state_path = metadata_path

    if METADATA_CACHE_FILENAME in remote_files:
        emit(log, f"downloading {METADATA_CACHE_FILENAME}")
        download_remote_file(
            rclone_bin,
            safe_remote_root,
            METADATA_CACHE_FILENAME,
            metadata_path,
        )
    else:
        emit(log, f"initializing missing {METADATA_CACHE_FILENAME}")
        write_cache(metadata_path, {})

    metadata_items = load_cache_items(metadata_path)
    state_items = load_cache_deploy_items(metadata_path)
    plans = build_deploy_plans(
        input_root=input_root,
        staging_root=resolved_staging,
        jobs=jobs,
        metadata_items=metadata_items,
        state_items=state_items,
        staged_metadata_items=staged_metadata_items,
        staged_state_items=staged_state_items,
        remote_files=remote_files,
        force=force,
    )
    changed = sum(1 for plan in plans if plan.changed)
    skipped = len(plans) - changed
    emit(log, f"planned {changed} changed item(s), {skipped} unchanged item(s)")

    return DeployContext(
        remote_root=safe_remote_root,
        staging_root=resolved_staging,
        metadata_path=metadata_path,
        state_path=state_path,
        metadata_items=metadata_items,
        state_items=state_items,
        remote_files=remote_files,
        plans=plans,
        prune_categories=normalize_categories(prune_categories),
    )


def build_deploy_plans(
    *,
    input_root: Path,
    staging_root: Path,
    jobs: list[Job],
    metadata_items: dict[str, dict[str, Any]],
    state_items: dict[str, dict[str, Any]],
    staged_metadata_items: dict[str, dict[str, Any]],
    staged_state_items: dict[str, dict[str, Any]],
    remote_files: set[str],
    force: bool,
) -> list[DeployJobPlan]:
    plans: list[DeployJobPlan] = []
    for job in jobs:
        output_key = relative_output_key(staging_root, job.output_file)
        cover_key = relative_output_key(staging_root, cover_output_path_for_job(job))
        signature = source_signature(input_root, job.source_video)
        existing_state = state_items.get(output_key)
        staged_item = staged_metadata_items.get(output_key)
        staged_state = staged_state_items.get(output_key)
        staged_ready = staged_output_is_ready(job, output_key, cover_key, signature, staged_item, staged_state)

        reason = "remote unchanged"
        changed = False
        needs_conversion = False
        if force:
            changed = True
            needs_conversion = True
            reason = "overwrite requested"
        elif existing_state is None:
            changed = True
            needs_conversion = not staged_ready
            reason = "staged output ready" if staged_ready else "not deployed"
        elif existing_state.get("source") != signature:
            changed = True
            needs_conversion = not staged_ready
            reason = "staged output ready" if staged_ready else "source changed"
        elif existing_state.get("outputPath") != output_key or existing_state.get("coverPath") != cover_key:
            changed = True
            needs_conversion = not staged_ready
            reason = "staged output ready" if staged_ready else "output path changed"
        elif output_key not in metadata_items:
            changed = True
            needs_conversion = not staged_ready
            reason = "staged output ready" if staged_ready else "metadata missing"
        elif output_key not in remote_files or cover_key not in remote_files:
            changed = True
            needs_conversion = not staged_ready
            reason = "staged output ready" if staged_ready else "remote file missing"

        plans.append(
            DeployJobPlan(
                job=job,
                output_key=output_key,
                cover_key=cover_key,
                source_signature=signature,
                changed=changed,
                needs_conversion=needs_conversion,
                reason=reason,
                staged_metadata_item=staged_item if staged_ready else None,
            )
        )
    return plans


def finalize_deploy(
    *,
    rclone_bin: str,
    context: DeployContext,
    ffprobe_bin: str,
    successful_output_keys: set[str],
    dry_run: bool,
    keep_staging: bool,
    input_root: Path,
    log: DeployLogger | None = None,
) -> DeploySummary:
    expected_files = expected_remote_files(context.plans)
    next_metadata_items: dict[str, dict[str, Any]] = {
        key: item
        for key, item in context.metadata_items.items()
        if not path_is_in_categories(key, context.prune_categories)
    }
    next_deploy_items: dict[str, dict[str, Any]] = {}
    uploaded_paths: list[str] = []

    now = datetime.now(tz=UTC).isoformat()
    emit(log, "building next metadata and deploy tracking")
    changed_plans = [plan for plan in context.plans if plan.changed]
    if dry_run:
        dangling = dangling_remote_files(context.remote_files, expected_files, context.prune_categories)
        emit(log, f"dry run: would upload {len(changed_plans) * 2 + 1} file(s)")
        emit(log, f"dry run: would delete {len(dangling)} dangling remote file(s)")
        return DeploySummary(
            uploaded=len(changed_plans) * 2 + 1,
            deleted=len(dangling),
            skipped=sum(1 for plan in context.plans if not plan.changed),
            changed=sum(1 for plan in context.plans if plan.changed),
            metadata_path=context.metadata_path,
            state_path=context.state_path,
        )

    for index, plan in enumerate(context.plans, start=1):
        deploy_item = deploy_tracking_item(plan, deployed_at=now)
        if plan.changed:
            if plan.output_key not in successful_output_keys:
                raise ConversionError(f"deploy output was not converted successfully: {plan.output_key}")
            if plan.needs_conversion:
                emit(log, f"probing changed output {len(uploaded_paths) // 2 + 1}/{len(changed_plans)}: {plan.output_key}")
                item = build_metadata_cache_item(
                    plan.job.output_file,
                    ffprobe_bin=ffprobe_bin,
                    deploy_item=deploy_item,
                )
                if item is None:
                    raise ConversionError(f"failed to probe metadata for staged output: {plan.job.output_file}")
                next_metadata_items[plan.output_key] = item
            else:
                if plan.staged_metadata_item is None:
                    raise ConversionError(f"staged metadata is missing for deploy output: {plan.output_key}")
                emit(log, f"reusing staged metadata {index}/{len(context.plans)}: {plan.output_key}")
                next_metadata_items[plan.output_key] = plan.staged_metadata_item
            uploaded_paths.extend([plan.output_key, plan.cover_key])
        else:
            existing_item = context.metadata_items.get(plan.output_key)
            if existing_item is None:
                raise ConversionError(f"remote metadata is missing unchanged output: {plan.output_key}")
            emit(log, f"reusing metadata {index}/{len(context.plans)}: {plan.output_key}")
            next_metadata_items[plan.output_key] = existing_item

        next_deploy_items[plan.output_key] = deploy_item

    emit(log, f"writing {METADATA_CACHE_FILENAME}")
    write_cache(context.metadata_path, next_metadata_items, deploy_items=next_deploy_items)
    uploaded_paths.append(METADATA_CACHE_FILENAME)

    upload_remote_files(rclone_bin, context.remote_root, context.staging_root, uploaded_paths, log=log)

    dangling = sorted(dangling_remote_files(context.remote_files, expected_files, context.prune_categories))
    delete_remote_files(rclone_bin, context.remote_root, dangling, log=log)

    if not keep_staging:
        emit(log, f"cleaning staging directory: {context.staging_root}")
        clean_staging(context.staging_root, input_root=input_root)
    else:
        emit(log, "keeping staging directory")

    return DeploySummary(
        uploaded=len(uploaded_paths),
        deleted=len(dangling),
        skipped=sum(1 for plan in context.plans if not plan.changed),
        changed=sum(1 for plan in context.plans if plan.changed),
        metadata_path=context.metadata_path,
        state_path=context.state_path,
    )


def expected_remote_files(plans: list[DeployJobPlan]) -> set[str]:
    expected = {METADATA_CACHE_FILENAME}
    for plan in plans:
        expected.add(plan.output_key)
        expected.add(plan.cover_key)
    return expected


def deploy_tracking_item(plan: DeployJobPlan, *, deployed_at: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "source": plan.source_signature,
        "outputPath": plan.output_key,
        "coverPath": plan.cover_key,
    }
    if deployed_at is not None:
        item["deployedAt"] = deployed_at
    return item


def staged_output_is_ready(
    job: Job,
    output_key: str,
    cover_key: str,
    source: dict[str, Any],
    staged_item: dict[str, Any] | None,
    staged_state: dict[str, Any] | None,
) -> bool:
    if staged_item is None or staged_state is None:
        return False
    if staged_state.get("source") != source:
        return False
    if staged_state.get("outputPath") != output_key or staged_state.get("coverPath") != cover_key:
        return False
    if not job.output_file.is_file() or not cover_output_path_for_job(job).is_file():
        return False
    stat = job.output_file.stat()
    return staged_item.get("size") == stat.st_size and staged_item.get("modTimeUnixNano") == stat.st_mtime_ns


def dangling_remote_files(
    remote_files: set[str],
    expected_files: set[str],
    prune_categories: tuple[str, ...],
) -> set[str]:
    if set(prune_categories) == set(CATEGORY_ORDER):
        return remote_files - expected_files
    return {
        relpath
        for relpath in remote_files - expected_files
        if path_is_in_categories(relpath, prune_categories)
    }


def emit(log: DeployLogger | None, message: str) -> None:
    if log is not None:
        log(message)


def relative_output_key(staging_root: Path, output_path: Path) -> str:
    try:
        relpath = output_path.resolve().relative_to(staging_root.resolve()).as_posix()
    except ValueError as exc:
        raise ConversionError(f"deploy output is outside staging directory: {output_path}") from exc
    return validate_remote_relpath(relpath)


def validate_remote_root(remote_root: str) -> str:
    normalized = remote_root.strip().rstrip("/")
    if not normalized:
        raise ConversionError("deploy remote must not be empty")
    if ":" not in normalized:
        raise ConversionError("deploy remote must be an rclone remote path such as remote:folder")
    _, remote_path = normalized.split(":", 1)
    if not remote_path or remote_path in {".", "/"}:
        raise ConversionError("deploy remote must include a non-root folder path")
    if "\\" in remote_path:
        raise ConversionError("deploy remote must use forward slashes")
    for part in PurePosixPath(remote_path).parts:
        if part in {"", ".", ".."}:
            raise ConversionError("deploy remote path must not contain traversal")
    return normalized


def validate_remote_relpath(relpath: str) -> str:
    normalized = relpath.strip()
    if not normalized:
        raise ConversionError("remote relative path must not be empty")
    if normalized.startswith("/") or "\\" in normalized:
        raise ConversionError(f"unsafe remote relative path: {relpath}")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ConversionError(f"unsafe remote relative path: {relpath}")
    return pure.as_posix()


def remote_file_path(remote_root: str, relpath: str) -> str:
    return f"{validate_remote_root(remote_root)}/{validate_remote_relpath(relpath)}"


def run_rclone(rclone_bin: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [rclone_bin, *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ConversionError(f"required tool not found: {rclone_bin}") from exc
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or exc.stdout.strip() or "rclone failed"
        raise ConversionError(message) from exc


def list_remote_files(rclone_bin: str, remote_root: str) -> set[str]:
    completed = run_rclone(
        rclone_bin,
        ["lsf", "--recursive", "--files-only", validate_remote_root(remote_root)],
    )
    files: set[str] = set()
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        files.add(validate_remote_relpath(line.strip()))
    return files


def download_remote_file(rclone_bin: str, remote_root: str, relpath: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_rclone(
        rclone_bin,
        ["copyto", remote_file_path(remote_root, relpath), str(output_path)],
    )


def upload_remote_file(rclone_bin: str, remote_root: str, input_path: Path, relpath: str) -> None:
    if not input_path.is_file():
        raise ConversionError(f"staged deploy file does not exist: {input_path}")
    run_rclone_progress(
        rclone_bin,
        ["copyto", str(input_path), remote_file_path(remote_root, relpath)],
    )


def upload_remote_files(
    rclone_bin: str,
    remote_root: str,
    staging_root: Path,
    relpaths: list[str],
    *,
    log: DeployLogger | None = None,
) -> None:
    for index, relpath in enumerate(relpaths, start=1):
        emit(log, f"uploading {index}/{len(relpaths)}: {relpath}")
        upload_remote_file(rclone_bin, remote_root, staging_root / Path(relpath), relpath)


def delete_remote_file(rclone_bin: str, remote_root: str, relpath: str) -> None:
    run_rclone(
        rclone_bin,
        ["deletefile", remote_file_path(remote_root, relpath)],
    )


def delete_remote_files(
    rclone_bin: str,
    remote_root: str,
    relpaths: list[str],
    *,
    log: DeployLogger | None = None,
) -> None:
    for index, relpath in enumerate(relpaths, start=1):
        emit(log, f"deleting dangling remote file {index}/{len(relpaths)}: {relpath}")
        delete_remote_file(rclone_bin, remote_root, relpath)


def run_rclone_progress(rclone_bin: str, args: list[str]) -> None:
    try:
        subprocess.run(
            [rclone_bin, "-P", *args],
            check=True,
        )
    except FileNotFoundError as exc:
        raise ConversionError(f"required tool not found: {rclone_bin}") from exc
    except subprocess.CalledProcessError as exc:
        raise ConversionError(f"rclone failed with exit code {exc.returncode}") from exc


def clean_staging(staging_root: Path, *, input_root: Path) -> None:
    resolved_staging = staging_root.resolve()
    resolved_input = input_root.resolve()
    if resolved_staging == resolved_input:
        raise ConversionError("refusing to clean staging because it is the input directory")
    if not resolved_staging.exists():
        return
    shutil.rmtree(resolved_staging)
