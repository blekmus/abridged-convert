from __future__ import annotations

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
    remove_subtitle_sidecars,
    run_command,
    write_cover_sidecar,
)
from abridged_convert.models import ConversionError, ConversionResult, EntryMetadata, Job, VideoEncodingConfig


def tracking_file_for_job(job: Job) -> Path:
    token = re.sub(r"[^A-Za-z0-9._-]+", "_", job.output_file.stem).strip("._") or "output"
    return job.output_file.parent / f".abridged-convert.{token}.incomplete"


def remove_incomplete_output(output_file: Path) -> None:
    if output_file.exists():
        output_file.unlink()


def resume_interrupted_job(job: Job, *, dry_run: bool) -> bool:
    tracking_file = tracking_file_for_job(job)
    if not tracking_file.exists():
        return False
    if not dry_run:
        remove_incomplete_output(job.output_file)
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
    result = ConversionResult(
        category=job.category,
        entry_dir=job.entry_dir,
        source_video=job.source_video,
        output_file=job.output_file,
    )
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
                result.message = exc.stderr.strip() or exc.stdout.strip() or "subtitle extraction failed"
        return result

    with tempfile.TemporaryDirectory(prefix="abridged-convert-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        try:
            cover_path, cover_source = choose_cover(job, metadata, ffmpeg_bin, temp_dir)
        except ConversionError as exc:
            result.message = str(exc)
            return result
        except subprocess.CalledProcessError as exc:
            result.message = exc.stderr.strip() or exc.stdout.strip() or "ffmpeg failed"
            return result
        result.cover_source = cover_source
        cover_output_path = cover_output_path_for_job(job)

        if dry_run:
            result.success = True
            result.message = "dry run (would resume interrupted conversion)" if resumed_from_incomplete else "dry run"
            return result

        job.output_file.parent.mkdir(parents=True, exist_ok=True)
        tracking_file.write_text(f"{job.output_file}\n", encoding="utf-8")
        ffmpeg_cmd = build_ffmpeg_command(
            ffmpeg_bin,
            job.source_video,
            job.output_file,
            metadata,
            cover_path,
            encoding,
        )

        keep_tracking_file = False
        try:
            run_command(ffmpeg_cmd)
            result.subtitles = write_sidecars(ffmpeg_bin, job, metadata, cover_path, cover_output_path)
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() or exc.stdout.strip() or "ffmpeg failed"
            if encoding.mode == "nvidia" and any(marker in stderr.lower() for marker in GPU_ERROR_MARKERS):
                fallback_encoding = cpu_video_encoding_config()
                ffmpeg_cmd = build_ffmpeg_command(
                    ffmpeg_bin,
                    job.source_video,
                    job.output_file,
                    metadata,
                    cover_path,
                    fallback_encoding,
                )
                try:
                    run_command(ffmpeg_cmd)
                    result.subtitles = write_sidecars(ffmpeg_bin, job, metadata, cover_path, cover_output_path)
                except subprocess.CalledProcessError:
                    remove_incomplete_output(job.output_file)
                    cover_output_path.unlink(missing_ok=True)
                    remove_subtitle_sidecars(job.output_file)
                    result.message = stderr
                    return result
                except KeyboardInterrupt:
                    keep_tracking_file = True
                    remove_incomplete_output(job.output_file)
                    cover_output_path.unlink(missing_ok=True)
                    remove_subtitle_sidecars(job.output_file)
                    raise
                result.success = True
                result.message = (
                    "resumed after interruption; fell back to cpu after gpu failure"
                    if resumed_from_incomplete
                    else "converted (fell back to cpu after gpu failure)"
                )
                return result
            remove_incomplete_output(job.output_file)
            cover_output_path.unlink(missing_ok=True)
            remove_subtitle_sidecars(job.output_file)
            result.message = stderr
            return result
        except KeyboardInterrupt:
            keep_tracking_file = True
            remove_incomplete_output(job.output_file)
            cover_output_path.unlink(missing_ok=True)
            remove_subtitle_sidecars(job.output_file)
            raise
        finally:
            if not keep_tracking_file:
                tracking_file.unlink(missing_ok=True)

    result.success = True
    action = "remuxed" if encoding.mode == "copy" else "converted"
    result.message = f"resumed after interruption; {action}" if resumed_from_incomplete else action
    return result
