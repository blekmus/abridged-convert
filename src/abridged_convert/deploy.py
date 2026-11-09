from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from abridged_convert.common import normalize_scope, path_in_scope, source_signature
from abridged_convert.constants import CATEGORY_ORDER
from abridged_convert.media import cover_output_path_for_job, extract_source_subtitles, is_subtitle_sidecar_name
from abridged_convert.metadata import (
    DEPLOY_RUN_SIGNATURE_KEY,
    DEPLOY_RUN_STATUS_IN_PROGRESS,
    DEPLOY_RUN_STATUS_KEY,
    ITEM_UPLOAD_PENDING_KEY,
    METADATA_CACHE_FILENAME,
    STALE_REMOTE_PATHS_KEY,
    SUBTITLES_KEY,
    build_metadata_cache_item,
    item_subtitle_paths,
    subtitle_metadata_entries,
    load_cache_document,
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
    # Remote video is current but its subtitle sidecars were never extracted (one-time backfill).
    subtitles_only: bool = False


@dataclass(frozen=True)
class DeployContext:
    remote_root: str
    staging_root: Path
    metadata_path: Path
    metadata_items: dict[str, dict[str, Any]]
    plans: list[DeployJobPlan]
    prune_scope: tuple[str, ...]
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


def bootstrap_deploy_from_existing_outputs(
    *,
    rclone_bin: str,
    remote_root: str | None,
    input_root: Path,
    source_signature_root: Path | None = None,
    staging_root: Path,
    jobs: list[Job],
    prune_scope: Iterable[str],
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
        for job in (job_with_existing_output(job) for job in jobs)
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

    prune_scope = normalize_scope(prune_scope)
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
        # Subtitles aren't extracted in a dry run, so sidecars beside an expected video may still be valid.
        output_keys = [plan.output_key for plan in plans]
        dangling = [relpath for relpath in dangling if not is_sidecar_of_any(relpath, output_keys)]
        dry_run_uploads = uploaded_paths + ([METADATA_CACHE_FILENAME] if remote_root else [])
        log_dry_run_remote_actions(log, uploads=dry_run_uploads, deletes=dangling)
        return DeploySummary(
            uploaded=len(dry_run_uploads),
            deleted=len(dangling),
            skipped=0,
            changed=len(plans),
            metadata_path=metadata_path,
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
    )


def is_sidecar_of_any(relpath: str, output_keys: Iterable[str]) -> bool:
    path = PurePosixPath(relpath)
    return any(
        path.parent == PurePosixPath(output_key).parent
        and is_subtitle_sidecar_name(PurePosixPath(output_key).stem, path.name)
        for output_key in output_keys
    )


BOOTSTRAP_OUTPUT_SUFFIXES = (".mp4", ".webm")


def job_with_existing_output(job: Job) -> Job:
    """Point a discovered job at the output already on disk.

    Discovery always names outputs `.mp4`; conversion may have written `.webm` instead. Bootstrap uploads
    what exists, so it follows the file rather than re-probing the source.
    """
    existing = [
        candidate
        for candidate in (job.output_file.with_suffix(suffix) for suffix in BOOTSTRAP_OUTPUT_SUFFIXES)
        if candidate.is_file()
    ]
    if len(existing) > 1:
        names = " and ".join(path.name for path in existing)
        raise ConversionError(f"bootstrap found both {names} in {job.output_file.parent}; remove the stale one")
    if not existing:
        # Leave the default name so the missing-output error names the expected file.
        return job
    return replace(job, output_file=existing[0])


def prepare_deploy_context(
    *,
    rclone_bin: str,
    remote_root: str,
    input_root: Path,
    source_signature_root: Path | None = None,
    staging_root: Path,
    jobs: list[Job],
    prune_scope: Iterable[str],
    force: bool,
    dry_run: bool = False,
    log: DeployLogger | None = None,
) -> DeployContext:
    resolved_staging = staging_root.resolve()
    if not dry_run:
        resolved_staging.mkdir(parents=True, exist_ok=True)
    safe_remote_root = validate_remote_root(remote_root)
    emit(log, f"preparing staging directory: {resolved_staging}")
    metadata_path = resolved_staging / METADATA_CACHE_FILENAME
    local_cache = load_cache_document(metadata_path)
    local_signature = local_cache.get(DEPLOY_RUN_SIGNATURE_KEY)
    resumed = (
        local_cache.get(DEPLOY_RUN_STATUS_KEY) == DEPLOY_RUN_STATUS_IN_PROGRESS
        and isinstance(local_signature, str)
        and bool(local_signature.strip())
    )

    if resumed:
        # A dry run may read an interrupted run's journal, but never rewrites it, so the resume point survives.
        deploy_run_signature = local_signature
        emit(log, f"resuming deploy run: {deploy_run_signature}")
        metadata_items = load_cache_items(metadata_path)
        stale_paths = journal_stale_paths(local_cache)
    else:
        stale_paths = []
        deploy_run_signature = uuid.uuid4().hex
        emit(log, f"downloading {METADATA_CACHE_FILENAME}")
        if dry_run:
            # Keep the remote copy out of staging: a journal left there would make the next real run "resume".
            with tempfile.TemporaryDirectory(prefix="abridged-convert-dry-run-") as temp_dir:
                temp_metadata_path = Path(temp_dir) / METADATA_CACHE_FILENAME
                downloaded = download_remote_file_if_exists(
                    rclone_bin,
                    safe_remote_root,
                    METADATA_CACHE_FILENAME,
                    temp_metadata_path,
                )
                metadata_items = load_cache_items(temp_metadata_path) if downloaded else {}
        else:
            downloaded = download_remote_file_if_exists(
                rclone_bin,
                safe_remote_root,
                METADATA_CACHE_FILENAME,
                metadata_path,
            )
            remote_items = load_cache_items(metadata_path) if downloaded else {}
            write_deploy_journal(
                metadata_path,
                remote_items,
                deploy_run_signature=deploy_run_signature,
                metadata_upload_pending=False,
            )
            metadata_items = load_cache_items(metadata_path)
        if not downloaded:
            emit(log, f"initializing missing {METADATA_CACHE_FILENAME}")

    dangling_paths = dangling_paths_from_metadata(
        metadata_items,
        jobs=jobs,
        source_signature_root=source_signature_root or input_root,
        prune_scope=normalize_scope(prune_scope),
    )
    plans = build_deploy_plans(
        source_signature_root=source_signature_root or input_root,
        staging_root=resolved_staging,
        jobs=jobs,
        metadata_items=metadata_items,
        force=force,
    )
    changed = sum(1 for plan in plans if plan.changed)
    skipped = len(plans) - changed
    emit(log, f"planned {changed} changed item(s), {skipped} unchanged item(s)")

    return DeployContext(
        remote_root=safe_remote_root,
        staging_root=resolved_staging,
        metadata_path=metadata_path,
        metadata_items=metadata_items,
        plans=plans,
        prune_scope=normalize_scope(prune_scope),
        deploy_run_signature=deploy_run_signature,
        resumed=resumed,
        dangling_paths=dangling_paths,
        stale_paths=stale_paths,
    )


def journal_stale_paths(journal: dict[str, Any]) -> list[str]:
    paths = journal.get(STALE_REMOTE_PATHS_KEY)
    if not isinstance(paths, list):
        return []
    return [validate_remote_relpath(path) for path in paths if isinstance(path, str)]


def build_deploy_plans(
    *,
    source_signature_root: Path,
    staging_root: Path,
    jobs: list[Job],
    metadata_items: dict[str, dict[str, Any]],
    force: bool,
) -> list[DeployJobPlan]:
    plans: list[DeployJobPlan] = []
    metadata_by_source = metadata_items_by_source(metadata_items)
    for job in jobs:
        signature = source_signature(source_signature_root, job.source_video)
        matched_key, matched_item = matching_metadata_item(metadata_by_source, signature)
        if matched_item is not None:
            output_key = metadata_output_key(matched_key, matched_item)
            cover_key = metadata_cover_key(output_key, matched_item)
            job = replace(job, output_file=staging_root / Path(output_key))
        else:
            output_key = relative_output_key(staging_root, job.output_file)
            cover_key = relative_output_key(staging_root, cover_output_path_for_job(job))

        subtitles_only = False
        if force:
            changed, needs_conversion, reason = True, True, "overwrite requested"
        elif matched_item is None:
            # No record has this source's path and size, so something at the output path came from a different source.
            changed, needs_conversion = True, True
            reason = "source changed" if output_key in metadata_items else "not deployed"
        elif matched_item.get(ITEM_UPLOAD_PENDING_KEY) is False:
            # Converted by an interrupted run; reconvert only if its staged files are gone.
            changed = True
            needs_conversion = not pending_output_files_ready(job, matched_item)
            reason = "pending output missing" if needs_conversion else "pending upload"
        elif SUBTITLES_KEY not in matched_item:
            changed, needs_conversion, subtitles_only = True, False, True
            reason = "subtitles missing"
        else:
            changed, needs_conversion, reason = False, False, "remote metadata match"

        plans.append(
            DeployJobPlan(
                job=job,
                output_key=output_key,
                cover_key=cover_key,
                source_signature=signature,
                changed=changed,
                needs_conversion=needs_conversion,
                reason=reason,
                subtitles_only=subtitles_only,
            )
        )
    return plans


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
    prune_scope: Iterable[str],
) -> tuple[str, ...]:
    local_sources = {
        (signature["path"], signature["size"])
        for signature in (source_signature(source_signature_root, job.source_video) for job in jobs)
    }
    selected_scope = normalize_scope(prune_scope)
    dangling: set[str] = set()
    for key, item in metadata_items.items():
        if not path_in_scope(key, selected_scope):
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
        if not path_in_scope(key, context.prune_scope)
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
        planned_paths = {relpath for plan in context.plans for relpath in (plan.output_key, plan.cover_key)}
        dry_run_deletes = [relpath for relpath in context.dangling_paths if relpath not in planned_paths]
        orphans = find_orphaned_remote_files(
            rclone_bin,
            context,
            expected=referenced_remote_paths(context.metadata_items) | planned_paths | set(dry_run_deletes),
            protect_changed_sidecars=True,
            log=log,
        )
        dry_run_deletes.extend(orphans)
        log_dry_run_remote_actions(log, uploads=upload_paths, deletes=dry_run_deletes)
        return DeploySummary(
            uploaded=len(upload_paths),
            deleted=len(dry_run_deletes),
            skipped=sum(1 for plan in context.plans if not plan.changed),
            changed=sum(1 for plan in context.plans if plan.changed),
            metadata_path=context.metadata_path,
        )

    # Subtitle backfills and items uploaded before an interruption are changed but have nothing pending.
    pending_total = sum(
        1
        for plan in changed_plans
        if (context.metadata_items.get(plan.output_key) or {}).get(ITEM_UPLOAD_PENDING_KEY) is False
    )
    pending_index = 0
    for index, plan in enumerate(context.plans, start=1):
        # Only uploaded items get this run's timestamp; skipped items keep their existing deployedAt.
        video_uploaded = plan.changed and not plan.subtitles_only
        deploy_item = deploy_tracking_item(plan, deployed_at=now if video_uploaded else None)
        if plan.changed:
            item = context.metadata_items.get(plan.output_key)
            if item is None:
                raise ConversionError(f"deploy metadata is missing changed output: {plan.output_key}")
            if item.get(ITEM_UPLOAD_PENDING_KEY) is False:
                pending_index += 1
                emit(log, f"uploading pending item {pending_index}/{pending_total}: {plan.output_key}")
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
                    stale_paths=context.stale_paths,
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
    else:
        emit(log, f"skipping {METADATA_CACHE_FILENAME}: unchanged")

    # Close the local journal (drops the in-progress marker) so a kept staging dir doesn't look interrupted.
    write_cache(
        context.metadata_path,
        next_metadata_items,
        deploy_run_signature=context.deploy_run_signature,
        include_item_upload_state=False,
    )

    expected_after = referenced_remote_paths(next_metadata_items)
    # A replaced source at the same path leaves its old entry dangling, but the new entry reuses the same
    # output/cover/subtitle paths, which were just uploaded and must survive the cleanup.
    dangling = [relpath for relpath in context.dangling_paths if relpath not in expected_after]
    # Files the catalog no longer references, e.g. left by a run that died between the catalog upload and the deletes.
    orphans = find_orphaned_remote_files(rclone_bin, context, expected=expected_after | set(dangling), log=log)
    dangling.extend(orphans)
    delete_remote_files(rclone_bin, context.remote_root, dangling, kept_files=expected_after, log=log)
    # Stale subtitle sidecars sit beside a still-deployed video, so their directories are never removed.
    stale = sorted(set(context.stale_paths) - expected_after - set(dangling))
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
        deleted=len(dangling),
        skipped=sum(1 for plan in context.plans if not plan.changed),
        changed=sum(1 for plan in context.plans if plan.changed),
        metadata_path=context.metadata_path,
    )


ORPHAN_SWEEP_LIMIT = 10


def find_orphaned_remote_files(
    rclone_bin: str,
    context: DeployContext,
    *,
    expected: set[str],
    protect_changed_sidecars: bool = False,
    log: DeployLogger | None = None,
) -> list[str]:
    """Remote files inside the run's scope that nothing in the catalog references.

    A dry run doesn't know the subtitle names of entries it has yet to convert, so it passes
    ``protect_changed_sidecars`` to keep anything sharing a changed output's stem off the list.
    """
    emit(log, f"listing remote files to find orphans: {context.remote_root}")
    remote_files = list_remote_files(rclone_bin, context.remote_root)
    in_scope = {
        relpath
        for relpath in remote_files
        if relpath != METADATA_CACHE_FILENAME and path_in_scope(relpath, context.prune_scope)
    }
    orphans = sorted(
        relpath
        for relpath in in_scope - expected
        if not (protect_changed_sidecars and belongs_to_planned_output(relpath, context.plans))
    )
    if len(orphans) > ORPHAN_SWEEP_LIMIT and len(orphans) * 2 > len(in_scope):
        # Normally only a handful of strays; most of the remote looking orphaned means the catalog is off.
        emit(
            log,
            f"WARNING: {len(orphans)} of {len(in_scope)} remote file(s) look orphaned; not deleting them. "
            "Check the remote catalog, or run rebuild-metadata.",
        )
        return []
    emit(log, f"found {len(orphans)} orphaned remote file(s)")
    return orphans


def belongs_to_planned_output(relpath: str, plans: list[DeployJobPlan]) -> bool:
    # Sidecars (cover, subtitles) of an output this run uploads share its directory and stem.
    path = PurePosixPath(relpath)
    for plan in plans:
        if not plan.changed:
            continue
        output = PurePosixPath(plan.output_key)
        if path.parent == output.parent and (path.stem == output.stem or path.name.startswith(f"{output.stem}.")):
            return True
    return False


def referenced_remote_paths(items: dict[str, dict[str, Any]]) -> set[str]:
    """Every remote file (video, cover, subtitles) that the given metadata items point at."""
    referenced: set[str] = set()
    for key, item in items.items():
        output_key = metadata_output_key(key, item)
        referenced.add(output_key)
        referenced.add(metadata_cover_key(output_key, item))
        referenced.update(subtitle_keys(item))
    return referenced


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
    stale_paths: Iterable[str] = (),
) -> None:
    write_cache(
        metadata_path,
        metadata_items,
        deploy_run_signature=deploy_run_signature,
        deploy_run_status=DEPLOY_RUN_STATUS_IN_PROGRESS,
        metadata_upload_pending=metadata_upload_pending,
        stale_remote_paths=stale_paths,
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
    write_deploy_journal(
        context.metadata_path,
        context.metadata_items,
        deploy_run_signature=context.deploy_run_signature,
        metadata_upload_pending=True,
        stale_paths=context.stale_paths,
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
        stale_paths=context.stale_paths,
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
    prune_scope: tuple[str, ...],
) -> set[str]:
    if set(prune_scope) == set(CATEGORY_ORDER):
        return remote_files - expected_files
    return {
        relpath
        for relpath in remote_files - expected_files
        if path_in_scope(relpath, prune_scope)
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
    kept_files: set[str] | None = None,
    log: DeployLogger | None = None,
) -> None:
    for index, relpath in enumerate(relpaths, start=1):
        emit(log, f"deleting dangling remote file {index}/{len(relpaths)}: {relpath}")
        delete_remote_file(rclone_bin, remote_root, relpath)
    empty_dirs = empty_parent_dirs_for_deleted_files(relpaths, remote_files, kept_files)
    for index, relpath in enumerate(empty_dirs, start=1):
        emit(log, f"removing empty remote directory {index}: {relpath}")
        try:
            remove_empty_remote_dir(rclone_bin, remote_root, relpath)
        except ConversionError as exc:
            if remote_files is not None or not is_remote_dir_not_empty_error(exc):
                raise


def empty_parent_dirs_for_deleted_files(
    relpaths: list[str],
    remote_files: set[str] | None,
    kept_files: set[str] | None = None,
) -> list[str]:
    candidates = {
        PurePosixPath(relpath).parent.as_posix()
        for relpath in relpaths
        if PurePosixPath(relpath).parent.as_posix() != "."
    }
    if kept_files:
        # Directories still holding a known deployed file are not empty; don't try to remove them.
        candidates = {
            dirname
            for dirname in candidates
            if not any(path.startswith(f"{dirname}/") for path in kept_files)
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
