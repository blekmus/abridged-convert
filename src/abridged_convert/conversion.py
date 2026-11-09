from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
from pathlib import Path

from abridged_convert.constants import GPU_ERROR_MARKERS, PREFERRED_TAG_ORDER
from abridged_convert.media import (
    apply_output_metadata_overrides,
    build_ffmpeg_command,
    choose_cover,
    collect_metadata,
    cover_output_path_for_job,
    cpu_video_encoding_config,
    expected_subtitle_sidecars,
    extract_subtitle_sidecars,
    process_error_message,
    remove_subtitle_sidecars,
    run_command,
    write_cover_sidecar,
)
from abridged_convert.models import ConversionError, ConversionResult, EntryMetadata, Job, VideoEncodingConfig


def tracking_file_for_job(job: Job) -> Path:
    token = hashlib.sha256(job.output_file.name.encode("utf-8")).hexdigest()
    return job.output_file.parent / f".abridged-convert.{token}.incomplete"


def discard_partial_outputs(job: Job, cover_output_path: Path) -> None:
    job.output_file.unlink(missing_ok=True)
    cover_output_path.unlink(missing_ok=True)
    remove_subtitle_sidecars(job.output_file)


def resume_interrupted_job(job: Job, *, dry_run: bool) -> bool:
    tracking_file = tracking_file_for_job(job)
    if not tracking_file.exists():
        # Accept old markers only when their contents identify this exact output.
        token = re.sub(r"[^A-Za-z0-9._-]+", "_", job.output_file.stem).strip("._") or "output"
        legacy = job.output_file.parent / f".abridged-convert.{token}.incomplete"
        if not legacy.is_file() or legacy.read_text(encoding="utf-8").strip() != str(job.output_file):
            return False
        tracking_file = legacy
    if not dry_run:
        job.output_file.unlink(missing_ok=True)
        tracking_file.unlink(missing_ok=True)
    return True


def write_sidecars(
    ffmpeg_bin: str,
    job: Job,
    metadata: EntryMetadata,
    cover_path: Path | None,
    cover_output_path: Path,
) -> list[dict]:
    if cover_path is not None:
        write_cover_sidecar(ffmpeg_bin, cover_path, cover_output_path)
    return extract_subtitle_sidecars(ffmpeg_bin, job.source_video, metadata.subtitle_streams, job.output_file)


def ensure_subtitle_sidecars(ffmpeg_bin: str, job: Job, metadata: EntryMetadata) -> list[dict]:
    """For an output that already exists, (re)write its subtitle sidecars only if any are missing."""
    expected = expected_subtitle_sidecars(job.output_file, metadata.subtitle_streams)
    if all(sidecar["path"].is_file() for sidecar in expected):
        return expected
    return extract_subtitle_sidecars(ffmpeg_bin, job.source_video, metadata.subtitle_streams, job.output_file)


def convert_job(
    job: Job,
    *,
    ffmpeg_bin: str,
    ffprobe_bin: str,
    encoding: VideoEncodingConfig,
    overwrite: bool,
    dry_run: bool,
) -> ConversionResult:
    result = ConversionResult.for_job(job)
    tracking_file = tracking_file_for_job(job)
    resumed_from_incomplete = resume_interrupted_job(job, dry_run=dry_run)

    try:
        metadata = collect_metadata(ffprobe_bin, job.source_video)
    except ConversionError as exc:
        result.message = str(exc)
        return result
    apply_output_metadata_overrides(job, metadata)

    result.mapped_tags = [tag for tag in PREFERRED_TAG_ORDER if metadata.tags.get(tag)]

    if job.output_file.exists() and not overwrite:
        result.success = True
        result.skipped = True
        result.message = "already exists"
        if not dry_run:
            try:
                result.subtitles = ensure_subtitle_sidecars(ffmpeg_bin, job, metadata)
            except subprocess.CalledProcessError as exc:
                result.success = False
                result.skipped = False
                result.message = process_error_message(exc, "subtitle extraction failed")
        return result

    with tempfile.TemporaryDirectory(prefix="abridged-convert-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        try:
            cover_path, cover_source = choose_cover(job, metadata, ffmpeg_bin, temp_dir)
        except ConversionError as exc:
            result.message = str(exc)
            return result
        except subprocess.CalledProcessError as exc:
            result.message = process_error_message(exc, "ffmpeg failed")
            return result
        result.cover_source = cover_source
        cover_output_path = cover_output_path_for_job(job)

        if dry_run:
            result.success = True
            result.message = "dry run (would resume interrupted conversion)" if resumed_from_incomplete else "dry run"
            return result

        job.output_file.parent.mkdir(parents=True, exist_ok=True)
        tracking_file.write_text(f"{job.output_file}\n", encoding="utf-8")
        fell_back_to_cpu = False
        ffmpeg_cmd = build_ffmpeg_command(
            ffmpeg_bin,
            job.source_video,
            job.output_file,
            metadata,
            cover_path,
            encoding,
        )

        try:
            try:
                run_command(ffmpeg_cmd)
            except subprocess.CalledProcessError as exc:
                stderr = process_error_message(exc, "ffmpeg failed")
                if encoding.mode != "nvidia" or not any(marker in stderr.lower() for marker in GPU_ERROR_MARKERS):
                    raise
                fell_back_to_cpu = True
                run_command(
                    build_ffmpeg_command(
                        ffmpeg_bin,
                        job.source_video,
                        job.output_file,
                        metadata,
                        cover_path,
                        cpu_video_encoding_config(),
                    )
                )
            result.subtitles = write_sidecars(ffmpeg_bin, job, metadata, cover_path, cover_output_path)
        except subprocess.CalledProcessError as exc:
            discard_partial_outputs(job, cover_output_path)
            tracking_file.unlink(missing_ok=True)
            result.message = process_error_message(exc, "ffmpeg failed")
            return result
        except KeyboardInterrupt:
            # The tracking file stays so the next run knows this output is incomplete.
            discard_partial_outputs(job, cover_output_path)
            raise
        tracking_file.unlink(missing_ok=True)

    result.success = True
    if fell_back_to_cpu:
        result.message = (
            "resumed after interruption; fell back to cpu after gpu failure"
            if resumed_from_incomplete
            else "converted (fell back to cpu after gpu failure)"
        )
        return result
    action = "remuxed" if encoding.mode == "copy" else "converted"
    result.message = f"resumed after interruption; {action}" if resumed_from_incomplete else action
    return result
