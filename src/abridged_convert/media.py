from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from abridged_convert.constants import GPU_ERROR_MARKERS, IMAGE_CODECS, PREFERRED_TAG_ORDER
from abridged_convert.cover_finder import find_best_cover
from abridged_convert.discovery import derived_output_title, parse_entry_name
from abridged_convert.models import ConversionError, ConversionResult, EntryMetadata, Job, VideoEncodingConfig


def run_command(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def ffprobe_json(ffprobe_bin: str, media_path: Path) -> dict:
    completed = run_command(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            (
                "format_tags:stream=index,codec_type,codec_name,disposition:stream_tags="
                "language,title,filename,mimetype,handler_name,vendor_id"
            ),
            "-of",
            "json",
            str(media_path),
        ]
    )
    return json.loads(completed.stdout)


def normalize_tags(tags: dict[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in tags.items():
        if isinstance(value, str):
            normalized[key.lower()] = value
    return normalized


def map_output_tags(format_tags: dict[str, str]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for key in PREFERRED_TAG_ORDER:
        value = format_tags.get(key)
        if value:
            mapped[key] = value
    return mapped


def collect_metadata(ffprobe_bin: str, media_path: Path) -> EntryMetadata:
    probe = ffprobe_json(ffprobe_bin, media_path)
    streams = probe.get("streams", [])
    format_tags = normalize_tags(probe.get("format", {}).get("tags", {}))
    metadata = EntryMetadata(tags=map_output_tags(format_tags))

    for stream in streams:
        stream_type = stream.get("codec_type")
        disposition = stream.get("disposition") or {}
        tags = normalize_tags(stream.get("tags", {}))

        if stream_type == "video":
            is_attached_pic = bool(disposition.get("attached_pic"))
            looks_like_cover = stream.get("codec_name") in IMAGE_CODECS and metadata.video_index is not None
            if is_attached_pic or looks_like_cover:
                if metadata.attached_cover_index is None:
                    metadata.attached_cover_index = stream["index"]
                    metadata.attached_cover_codec = stream.get("codec_name")
                continue
            if metadata.video_index is None:
                metadata.video_index = stream["index"]
                metadata.video_language = tags.get("language")
        elif stream_type == "audio" and metadata.audio_index is None:
            metadata.audio_index = stream["index"]
            metadata.audio_language = tags.get("language")

    if metadata.video_index is None:
        raise ConversionError("no primary video stream found")

    if "description" not in metadata.tags and "synopsis" in metadata.tags:
        metadata.tags["description"] = metadata.tags["synopsis"]
    if "comment" not in metadata.tags:
        purl = format_tags.get("purl")
        if purl:
            metadata.tags["comment"] = purl

    return metadata


def apply_output_metadata_overrides(job: Job, metadata: EntryMetadata) -> None:
    creator, _ = parse_entry_name(job.entry_dir.name)
    metadata.tags["title"] = derived_output_title(job)
    metadata.tags["artist"] = creator


def embedded_cover_suffix(codec_name: str | None) -> str:
    if codec_name == "webp":
        return ".webp"
    return ".png"


def extract_embedded_cover(
    ffmpeg_bin: str,
    media_path: Path,
    stream_index: int,
    codec_name: str | None,
    temp_dir: Path,
) -> Path:
    output_path = temp_dir / f"embedded-cover{embedded_cover_suffix(codec_name)}"
    cmd = [
        ffmpeg_bin,
        "-y",
        "-v",
        "error",
        "-i",
        str(media_path),
        "-map",
        f"0:{stream_index}",
        "-frames:v",
        "1",
    ]
    if codec_name == "webp":
        cmd.extend(["-c:v", "copy"])
    else:
        cmd.extend(["-c:v", "png"])
    cmd.append(str(output_path))
    run_command(cmd)
    return output_path


def ffmpeg_supports_encoder(ffmpeg_bin: str, encoder_name: str) -> bool:
    completed = run_command([ffmpeg_bin, "-hide_banner", "-encoders"], check=False)
    if completed.returncode != 0:
        return False
    return encoder_name in completed.stdout


def resolve_video_encoding_config(ffmpeg_bin: str, mode: str) -> VideoEncodingConfig:
    if mode == "cpu":
        return VideoEncodingConfig(mode="cpu", codec="libx264", extra_args=("-preset", "medium", "-crf", "23"))

    if mode == "nvidia":
        if not ffmpeg_supports_encoder(ffmpeg_bin, "h264_nvenc"):
            raise ConversionError("ffmpeg does not support h264_nvenc on this system")
        return VideoEncodingConfig(
            mode="nvidia",
            codec="h264_nvenc",
            extra_args=("-preset", "p5", "-cq", "23", "-rc", "vbr"),
        )

    if ffmpeg_supports_encoder(ffmpeg_bin, "h264_nvenc"):
        return VideoEncodingConfig(
            mode="nvidia",
            codec="h264_nvenc",
            extra_args=("-preset", "p5", "-cq", "23", "-rc", "vbr"),
        )
    return VideoEncodingConfig(mode="cpu", codec="libx264", extra_args=("-preset", "medium", "-crf", "23"))


def build_ffmpeg_command(
    ffmpeg_bin: str,
    source_video: Path,
    output_path: Path,
    metadata: EntryMetadata,
    cover_path: Path | None,
    encoding: VideoEncodingConfig,
) -> list[str]:
    cmd = [ffmpeg_bin, "-y", "-v", "error", "-i", str(source_video)]
    if cover_path is not None:
        cmd.extend(["-i", str(cover_path)])

    cmd.extend(["-map", f"0:{metadata.video_index}"])
    if metadata.audio_index is not None:
        cmd.extend(["-map", f"0:{metadata.audio_index}"])
    if cover_path is not None:
        cmd.extend(["-map", "1:v:0"])

    cmd.extend(
        [
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-sn",
            "-dn",
            "-c:v:0",
            encoding.codec,
            "-pix_fmt",
            "yuv420p",
        ]
    )
    cmd.extend(encoding.extra_args)

    if metadata.audio_index is not None:
        cmd.extend(["-c:a:0", "aac", "-b:a:0", "160k"])

    if cover_path is not None:
        cmd.extend(
            [
                "-c:v:1",
                "png",
                "-disposition:v:1",
                "attached_pic",
                "-metadata:s:v:1",
                "title=Cover",
                "-metadata:s:v:1",
                "comment=Cover (front)",
            ]
        )

    if metadata.video_language:
        cmd.extend(["-metadata:s:v:0", f"language={metadata.video_language}"])
    if metadata.audio_language and metadata.audio_index is not None:
        cmd.extend(["-metadata:s:a:0", f"language={metadata.audio_language}"])

    for key, value in metadata.tags.items():
        if value:
            cmd.extend(["-metadata", f"{key}={value}"])

    cmd.extend(["-movflags", "+faststart", str(output_path)])
    return cmd


def choose_cover(
    job: Job,
    metadata: EntryMetadata,
    ffmpeg_bin: str,
    temp_dir: Path,
) -> tuple[Path | None, str]:
    if job.external_cover is not None:
        return job.external_cover, f"external:{job.external_cover.name}"

    if job.category == "Series":
        cover_match = find_best_cover(
            job.source_video,
            job.entry_dir / "covers",
            metadata_title=metadata.tags.get("title"),
        )
        if cover_match is not None:
            return cover_match.path, f"external:{cover_match.path.name}"

    if metadata.attached_cover_index is not None:
        return (
            extract_embedded_cover(
                ffmpeg_bin,
                job.source_video,
                metadata.attached_cover_index,
                metadata.attached_cover_codec,
                temp_dir,
            ),
            "embedded",
        )
    return None, "none"


def cover_output_path_for_job(job: Job) -> Path:
    return job.output_file.with_suffix(".webp")


def write_cover_sidecar(
    ffmpeg_bin: str,
    cover_path: Path,
    output_path: Path,
) -> None:
    if cover_path.suffix.lower() == ".webp":
        shutil.copyfile(cover_path, output_path)
        return

    run_command(
        [
            ffmpeg_bin,
            "-y",
            "-v",
            "error",
            "-i",
            str(cover_path),
            "-an",
            "-sn",
            "-dn",
            "-frames:v",
            "1",
            "-c:v",
            "libwebp",
            "-lossless",
            "1",
            str(output_path),
        ]
    )


def validate_tools(ffmpeg_bin: str, ffprobe_bin: str) -> None:
    for tool in (ffmpeg_bin, ffprobe_bin):
        try:
            run_command([tool, "-version"])
        except FileNotFoundError as exc:
            raise ConversionError(f"required tool not found: {tool}") from exc
