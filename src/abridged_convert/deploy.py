from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from abridged_convert.common import normalize_categories, path_is_in_categories, source_signature
from abridged_convert.constants import CATEGORY_ORDER
from abridged_convert.media import cover_output_path_for_job, extract_source_subtitles
from abridged_convert.metadata import (
    DEPLOY_RUN_SIGNATURE_KEY,
    DEPLOY_RUN_STATUS_IN_PROGRESS,
    DEPLOY_RUN_STATUS_KEY,
    ITEM_UPLOAD_PENDING_KEY,
    METADATA_CACHE_FILENAME,
    SUBTITLES_KEY,
    build_metadata_cache_item,
    deploy_state_from_metadata_item,
    item_subtitle_paths,
    subtitle_metadata_entries,
    load_cache_document,
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
    # Remote video is current but its subtitle sidecars were never extracted (one-time backfill).
    subtitles_only: bool = False


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
    deploy_run_signature: str
    resumed: bool
    dangling_paths: tuple[str, ...] = ()
    # Remote files replaced by a re-conversion that produced fewer/different subtitle sidecars.
    stale_paths: list[str] = field(default_factory=list)


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
    source_signature_root: Path | None = None,
    staging_root: Path,
    jobs: list[Job],
    prune_categories: Iterable[str],
    ffprobe_bin: str,
    ffmpeg_bin: str = "ffmpeg",
    dry_run: bool,
    keep_staging: bool,
    log: DeployLogger | None = None,
) -> DeploySummary:
    resolved_staging = staging_root.resolve()
    resolved_source_signature_root = (source_signature_root or input_root).resolve()
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
            source_signature=source_signature(resolved_source_signature_root, job.source_video),
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
            emit(log, f"extracting subtitles {index}/{len(plans)}: {plan.output_key}")
            sidecars = extract_source_subtitles(ffmpeg_bin, ffprobe_bin, plan.job.source_video, plan.job.output_file)
            item[SUBTITLES_KEY] = subtitle_metadata_entries(resolved_staging, sidecars)
            metadata_items[plan.output_key] = item
            deploy_items[plan.output_key] = deploy_item
        uploaded_paths.extend([plan.output_key, plan.cover_key])
        if plan.output_key in metadata_items:
            uploaded_paths.extend(subtitle_keys(metadata_items[plan.output_key]))

    prune_scope = normalize_categories(prune_categories)
    expected_files = expected_remote_files(plans) | set(uploaded_paths)
    remote_files: set[str] = set()
    safe_remote_root = None
    if remote_root is not None:
        safe_remote_root = validate_remote_root(remote_root)
        emit(log, f"listing remote files: {safe_remote_root}")
        remote_files = list_remote_files(rclone_bin, safe_remote_root)
        emit(log, f"remote has {len(remote_files)} file(s)")

    dangling = sorted(dangling_remote_files(remote_files, expected_files, prune_scope))
    if dry_run:
        dry_run_uploads = uploaded_paths + ([METADATA_CACHE_FILENAME] if remote_root else [])
        log_dry_run_remote_actions(log, uploads=dry_run_uploads, deletes=dangling)
        return DeploySummary(
            uploaded=len(dry_run_uploads),
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
        delete_remote_files(rclone_bin, safe_remote_root, dangling, remote_files=remote_files, log=log)
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
    source_signature_root: Path | None = None,
    staging_root: Path,
    jobs: list[Job],
    prune_categories: Iterable[str],
    force: bool,
    log: DeployLogger | None = None,
) -> DeployContext:
    resolved_staging = staging_root.resolve()
    resolved_staging.mkdir(parents=True, exist_ok=True)
    safe_remote_root = validate_remote_root(remote_root)
    emit(log, f"preparing staging directory: {resolved_staging}")
    metadata_path = resolved_staging / METADATA_CACHE_FILENAME
    state_path = metadata_path
    local_cache = load_cache_document(metadata_path)
    local_signature = local_cache.get(DEPLOY_RUN_SIGNATURE_KEY)
    resumed = (
        local_cache.get(DEPLOY_RUN_STATUS_KEY) == DEPLOY_RUN_STATUS_IN_PROGRESS
        and isinstance(local_signature, str)
        and bool(local_signature.strip())
    )

    if resumed:
        deploy_run_signature = local_signature
        emit(log, f"resuming deploy run: {deploy_run_signature}")
    else:
        deploy_run_signature = uuid.uuid4().hex
        emit(log, f"downloading {METADATA_CACHE_FILENAME}")
        downloaded = download_remote_file_if_exists(
            rclone_bin,
            safe_remote_root,
            METADATA_CACHE_FILENAME,
            metadata_path,
        )
        if not downloaded:
            emit(log, f"initializing missing {METADATA_CACHE_FILENAME}")
        remote_items = load_cache_items(metadata_path) if downloaded else {}
        write_deploy_journal(
            metadata_path,
            remote_items,
            deploy_run_signature=deploy_run_signature,
            metadata_upload_pending=False,
        )

    metadata_items = load_cache_items(metadata_path)
    state_items = load_cache_deploy_items(metadata_path)
    staged_metadata_items = metadata_items
    staged_state_items = state_items
    dangling_paths = dangling_paths_from_metadata(
        metadata_items,
        jobs=jobs,
        source_signature_root=source_signature_root or input_root,
        prune_categories=normalize_categories(prune_categories),
    )
    plans = build_deploy_plans(
        input_root=input_root,
        source_signature_root=source_signature_root or input_root,
        staging_root=resolved_staging,
        jobs=jobs,
        metadata_items=metadata_items,
        state_items=state_items,
        staged_metadata_items=staged_metadata_items,
        staged_state_items=staged_state_items,
        remote_files=set(),
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
        remote_files=set(),
        plans=plans,
        prune_categories=normalize_categories(prune_categories),
        deploy_run_signature=deploy_run_signature,
        resumed=resumed,
        dangling_paths=dangling_paths,
    )


def build_deploy_plans(
    *,
    input_root: Path,
    source_signature_root: Path | None = None,
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
    signature_root = source_signature_root or input_root
    metadata_by_source = metadata_items_by_source(metadata_items)
    for job in jobs:
        signature = source_signature(signature_root, job.source_video)
        matched_key, matched_item = matching_metadata_item(metadata_by_source, signature)
        if matched_item is not None:
            output_key = metadata_output_key(matched_key, matched_item)
            cover_key = metadata_cover_key(output_key, matched_item)
            job = replace_job_output(job, staging_root / Path(output_key))
        else:
            output_key = relative_output_key(staging_root, job.output_file)
            cover_key = relative_output_key(staging_root, cover_output_path_for_job(job))
        existing_state = state_items.get(output_key)
        staged_item = staged_metadata_items.get(output_key)
        staged_state = staged_state_items.get(output_key)
        staged_ready = staged_output_is_ready(job, output_key, cover_key, signature, staged_item, staged_state)
        pending_upload = matched_item is not None and matched_item.get(ITEM_UPLOAD_PENDING_KEY) is False

        reason = "remote unchanged"
        changed = False
        needs_conversion = False
        subtitles_only = False
        if force:
            changed = True
            needs_conversion = True
            reason = "overwrite requested"
        elif pending_upload:
            changed = True
            needs_conversion = not pending_output_files_ready(job, matched_item)
            reason = "pending upload" if not needs_conversion else "pending output missing"
        elif matched_item is not None and SUBTITLES_KEY not in matched_item:
            changed = True
            needs_conversion = False
            subtitles_only = True
            reason = "subtitles missing"
        elif matched_item is not None:
            changed = False
            needs_conversion = False
            reason = "remote metadata match"
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
                subtitles_only=subtitles_only,
            )
        )
    return plans


def replace_job_output(job: Job, output_file: Path) -> Job:
    return replace(job, output_file=output_file)


def metadata_items_by_source(items: dict[str, dict[str, Any]]) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    indexed: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for key, item in items.items():
        source = item.get("source")
        if not isinstance(source, dict):
            continue
        source_path = source.get("path")
        if isinstance(source_path, str):
            indexed.setdefault(source_path, []).append((key, item))
    return indexed


def matching_metadata_item(
    items_by_source: dict[str, list[tuple[str, dict[str, Any]]]],
    signature: dict[str, Any],
) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    source_path = signature.get("path")
    source_size = signature.get("size")
    if not isinstance(source_path, str):
        return None, None
    for key, item in items_by_source.get(source_path, []):
        source = item.get("source")
        if isinstance(source, dict) and source.get("size") == source_size:
            return key, item
    return None, None


def metadata_output_key(metadata_key: str | None, item: dict[str, Any]) -> str:
    output_path = item.get("outputPath")
    if isinstance(output_path, str):
        return validate_remote_relpath(output_path)
    if metadata_key is None:
        raise ConversionError("metadata item is missing output path")
    return validate_remote_relpath(metadata_key)


def subtitle_keys(item: dict[str, Any]) -> list[str]:
    return [validate_remote_relpath(path) for path in item_subtitle_paths(item)]


def metadata_cover_key(output_key: str, item: dict[str, Any]) -> str:
    cover_path = item.get("coverPath")
    if isinstance(cover_path, str):
        return validate_remote_relpath(cover_path)
    return validate_remote_relpath(Path(output_key).with_suffix(".webp").as_posix())


def dangling_paths_from_metadata(
    metadata_items: dict[str, dict[str, Any]],
    *,
    jobs: list[Job],
    source_signature_root: Path,
    prune_categories: Iterable[str],
) -> tuple[str, ...]:
    local_sources = {
        (signature["path"], signature["size"])
        for signature in (source_signature(source_signature_root, job.source_video) for job in jobs)
    }
    selected_categories = normalize_categories(prune_categories)
    dangling: set[str] = set()
    for key, item in metadata_items.items():
        if not path_is_in_categories(key, selected_categories):
            continue
        source = item.get("source")
        if not isinstance(source, dict):
            continue
        source_path = source.get("path")
        source_size = source.get("size")
        if (source_path, source_size) in local_sources:
            continue
        output_key = metadata_output_key(key, item)
        cover_key = metadata_cover_key(output_key, item)
        dangling.add(output_key)
        dangling.add(cover_key)
        dangling.update(subtitle_keys(item))
    return tuple(sorted(dangling))


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
    next_metadata_items: dict[str, dict[str, Any]] = {
        key: item
        for key, item in context.metadata_items.items()
        if not path_is_in_categories(key, context.prune_categories)
    }
    uploaded_paths: list[str] = []

    now = datetime.now(tz=UTC).isoformat()
    emit(log, "building next metadata and deploy tracking")
    changed_plans = [plan for plan in context.plans if plan.changed]
    metadata_changed = bool(changed_plans or context.dangling_paths or context.resumed)
    if dry_run:
        # Subtitle sidecar names are only known after extraction, so dry runs list video and cover only.
        upload_paths = [
            relpath
            for plan in changed_plans
            if not plan.subtitles_only
            for relpath in (plan.output_key, plan.cover_key)
        ]
        if metadata_changed:
            upload_paths.append(METADATA_CACHE_FILENAME)
        log_dry_run_remote_actions(log, uploads=upload_paths, deletes=list(context.dangling_paths))
        return DeploySummary(
            uploaded=len(upload_paths),
            deleted=len(context.dangling_paths),
            skipped=sum(1 for plan in context.plans if not plan.changed),
            changed=sum(1 for plan in context.plans if plan.changed),
            metadata_path=context.metadata_path,
            state_path=context.state_path,
        )

    for index, plan in enumerate(context.plans, start=1):
        # Only uploaded items get this run's timestamp; skipped items keep their existing deployedAt.
        video_uploaded = plan.changed and not plan.subtitles_only
        deploy_item = deploy_tracking_item(plan, deployed_at=now if video_uploaded else None)
        if plan.changed:
            item = context.metadata_items.get(plan.output_key)
            if item is None:
                raise ConversionError(f"deploy metadata is missing changed output: {plan.output_key}")
            if item.get(ITEM_UPLOAD_PENDING_KEY) is False:
                emit(log, f"uploading pending item {len(uploaded_paths) // 2 + 1}/{len(changed_plans)}: {plan.output_key}")
                item_paths = [plan.output_key, plan.cover_key, *subtitle_keys(item)]
                upload_remote_files(rclone_bin, context.remote_root, context.staging_root, item_paths, log=log)
                uploaded_paths.extend(item_paths)
                item.pop(ITEM_UPLOAD_PENDING_KEY, None)
                context.metadata_items[plan.output_key] = item
                write_deploy_journal(
                    context.metadata_path,
                    context.metadata_items,
                    deploy_run_signature=context.deploy_run_signature,
                    metadata_upload_pending=True,
                )
            item = dict(item)
            item.pop(ITEM_UPLOAD_PENDING_KEY, None)
            next_metadata_items[plan.output_key] = item
        else:
            existing_item = context.metadata_items.get(plan.output_key)
            if existing_item is None:
                raise ConversionError(f"remote metadata is missing unchanged output: {plan.output_key}")
            emit(log, f"reusing metadata {index}/{len(context.plans)}: {plan.output_key}")
            item = dict(existing_item)
            item.pop(ITEM_UPLOAD_PENDING_KEY, None)
            next_metadata_items[plan.output_key] = item

        next_metadata_items[plan.output_key].update(deploy_item)

    if metadata_changed:
        emit(log, f"writing {METADATA_CACHE_FILENAME}")
        remote_metadata_path = context.metadata_path.with_name(f"{context.metadata_path.name}.remote-upload")
        write_cache(
            remote_metadata_path,
            next_metadata_items,
            deploy_run_signature=context.deploy_run_signature,
            include_item_upload_state=False,
        )
        uploaded_paths.append(METADATA_CACHE_FILENAME)

        emit(log, f"uploading {METADATA_CACHE_FILENAME}")
        upload_remote_file(rclone_bin, context.remote_root, remote_metadata_path, METADATA_CACHE_FILENAME)
        remote_metadata_path.unlink(missing_ok=True)

        write_cache(
            context.metadata_path,
            next_metadata_items,
            deploy_run_signature=context.deploy_run_signature,
            include_item_upload_state=False,
        )
    else:
        emit(log, f"skipping {METADATA_CACHE_FILENAME}: unchanged")

    expected_after = {
        path
        for key, item in next_metadata_items.items()
        for path in (metadata_output_key(key, item), *subtitle_keys(item))
    }
    delete_remote_files(rclone_bin, context.remote_root, list(context.dangling_paths), log=log)
    # Stale subtitle sidecars sit beside a still-deployed video, so their directories are never removed.
    stale = sorted(set(context.stale_paths) - expected_after - set(context.dangling_paths))
    for index, relpath in enumerate(stale, start=1):
        emit(log, f"deleting stale subtitle {index}/{len(stale)}: {relpath}")
        delete_remote_file(rclone_bin, context.remote_root, relpath)

    if not keep_staging:
        emit(log, f"cleaning staging directory: {context.staging_root}")
        clean_staging(context.staging_root, input_root=input_root)
    else:
        emit(log, "keeping staging directory")

    return DeploySummary(
        uploaded=len(uploaded_paths),
        deleted=len(context.dangling_paths),
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
        if plan.staged_metadata_item is not None:
            expected.update(subtitle_keys(plan.staged_metadata_item))
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


def pending_deploy_tracking_item(plan: DeployJobPlan) -> dict[str, Any]:
    item = deploy_tracking_item(plan)
    item[ITEM_UPLOAD_PENDING_KEY] = False
    return item


def write_deploy_journal(
    metadata_path: Path,
    metadata_items: dict[str, dict[str, Any]],
    *,
    deploy_run_signature: str,
    metadata_upload_pending: bool,
) -> None:
    write_cache(
        metadata_path,
        metadata_items,
        deploy_run_signature=deploy_run_signature,
        deploy_run_status=DEPLOY_RUN_STATUS_IN_PROGRESS,
        metadata_upload_pending=metadata_upload_pending,
        include_item_upload_state=True,
    )


def record_pending_deploy_item(
    context: DeployContext,
    plan: DeployJobPlan,
    *,
    ffprobe_bin: str,
    subtitles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    previous_item = context.metadata_items.get(plan.output_key) or {}
    deploy_item = pending_deploy_tracking_item(plan)
    item = build_metadata_cache_item(
        plan.job.output_file,
        ffprobe_bin=ffprobe_bin,
        deploy_item=deploy_item,
    )
    if item is None:
        raise ConversionError(f"failed to probe metadata for staged output: {plan.job.output_file}")
    item.update(deploy_item)
    item[SUBTITLES_KEY] = subtitle_metadata_entries(context.staging_root, subtitles or [])
    context.stale_paths.extend(set(subtitle_keys(previous_item)) - set(subtitle_keys(item)))
    context.metadata_items[plan.output_key] = item
    deploy_state = deploy_state_from_metadata_item(item)
    if deploy_state is not None:
        context.state_items[plan.output_key] = deploy_state
    write_deploy_journal(
        context.metadata_path,
        context.metadata_items,
        deploy_run_signature=context.deploy_run_signature,
        metadata_upload_pending=True,
    )
    return item


def backfill_subtitles(
    rclone_bin: str,
    context: DeployContext,
    plan: DeployJobPlan,
    *,
    ffmpeg_bin: str,
    ffprobe_bin: str,
    log: DeployLogger | None = None,
) -> list[str]:
    """Extract and upload subtitle sidecars for an already-deployed video without touching the video itself."""
    item = context.metadata_items.get(plan.output_key)
    if item is None:
        raise ConversionError(f"deploy metadata is missing backfill output: {plan.output_key}")
    sidecars = extract_source_subtitles(ffmpeg_bin, ffprobe_bin, plan.job.source_video, plan.job.output_file)
    entries = subtitle_metadata_entries(context.staging_root, sidecars)
    next_item = {**item, SUBTITLES_KEY: entries}
    keys = subtitle_keys(next_item)
    if keys:
        upload_remote_files(rclone_bin, context.remote_root, context.staging_root, keys, log=log)
    context.metadata_items[plan.output_key] = next_item
    write_deploy_journal(
        context.metadata_path,
        context.metadata_items,
        deploy_run_signature=context.deploy_run_signature,
        metadata_upload_pending=True,
    )
    return keys


def log_dry_run_remote_actions(
    log: DeployLogger | None,
    *,
    uploads: list[str],
    deletes: list[str],
) -> None:
    emit(log, f"dry run: would upload {len(uploads)} file(s)")
    for index, relpath in enumerate(uploads, start=1):
        emit(log, f"dry run: would upload {index}/{len(uploads)}: {relpath}")
    emit(log, f"dry run: would delete {len(deletes)} dangling remote file(s)")
    for index, relpath in enumerate(deletes, start=1):
        emit(log, f"dry run: would delete {index}/{len(deletes)}: {relpath}")


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
    if not pending_output_files_ready(job, staged_item):
        return False
    stat = job.output_file.stat()
    return staged_item.get("size") == stat.st_size and staged_item.get("modTimeUnixNano") == stat.st_mtime_ns


def pending_output_files_ready(job: Job, item: dict[str, Any] | None = None) -> bool:
    if not job.output_file.is_file() or not cover_output_path_for_job(job).is_file():
        return False
    if item is None or not isinstance(item.get("outputPath"), str):
        return True
    # Subtitle paths are relative to the staging root; walk up from the output by its key depth.
    output_depth = len(PurePosixPath(item["outputPath"]).parts)
    staging_root = job.output_file.parents[output_depth - 1]
    return all((staging_root / Path(path)).is_file() for path in item_subtitle_paths(item))


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


def download_remote_file_if_exists(rclone_bin: str, remote_root: str, relpath: str, output_path: Path) -> bool:
    try:
        download_remote_file(rclone_bin, remote_root, relpath, output_path)
    except ConversionError as exc:
        message = str(exc).lower()
        if "not found" in message or "doesn't exist" in message or "does not exist" in message:
            return False
        raise
    return True


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


def remove_empty_remote_dir(rclone_bin: str, remote_root: str, relpath: str) -> None:
    run_rclone(
        rclone_bin,
        ["rmdir", remote_file_path(remote_root, relpath)],
    )


def delete_remote_files(
    rclone_bin: str,
    remote_root: str,
    relpaths: list[str],
    *,
    remote_files: set[str] | None = None,
    log: DeployLogger | None = None,
) -> None:
    for index, relpath in enumerate(relpaths, start=1):
        emit(log, f"deleting dangling remote file {index}/{len(relpaths)}: {relpath}")
        delete_remote_file(rclone_bin, remote_root, relpath)
    for index, relpath in enumerate(empty_parent_dirs_for_deleted_files(relpaths, remote_files), start=1):
        emit(log, f"removing empty remote directory {index}: {relpath}")
        try:
            remove_empty_remote_dir(rclone_bin, remote_root, relpath)
        except ConversionError as exc:
            if remote_files is not None or not is_remote_dir_not_empty_error(exc):
                raise


def empty_parent_dirs_for_deleted_files(relpaths: list[str], remote_files: set[str] | None) -> list[str]:
    candidates = {
        PurePosixPath(relpath).parent.as_posix()
        for relpath in relpaths
        if PurePosixPath(relpath).parent.as_posix() != "."
    }
    if remote_files is None:
        return sorted(candidates, key=lambda dirname: (dirname.count("/"), dirname), reverse=True)
    deleted = set(relpaths)
    remaining = remote_files - deleted
    empty_dirs = [
        dirname
        for dirname in candidates
        if not any(path == dirname or path.startswith(f"{dirname}/") for path in remaining)
    ]
    return sorted(empty_dirs, key=lambda dirname: (dirname.count("/"), dirname), reverse=True)


def is_remote_dir_not_empty_error(exc: ConversionError) -> bool:
    message = str(exc).lower()
    return "not empty" in message or "directory not empty" in message


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
