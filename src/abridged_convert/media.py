from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from abridged_convert.constants import GPU_ERROR_MARKERS, IMAGE_CODECS, IMAGE_SUBTITLE_CODECS, PREFERRED_TAG_ORDER
from abridged_convert.cover_finder import find_best_cover
from abridged_convert.discovery import derived_output_title, parse_entry_name
from abridged_convert.models import (
    ConversionError,
    ConversionResult,
    EntryMetadata,
    Job,
    SubtitleStream,
    VideoEncodingConfig,
)

GENERATED_THUMBNAIL_TIMESTAMP = "20"
WEBM_AUDIO_CODECS = {"opus", "vorbis"}
WEBM_VIDEO_CODECS = {"av1", "vp8", "vp9"}
MP4_AUDIO_CODECS = {"aac", "ac3", "alac", "eac3", "mp3"}
MP4_VIDEO_CODECS = {"av1", "h264", "hevc", "mpeg4", "vp9"}


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
                # Dispositions must be requested as their own section; "stream=disposition" returns nothing.
                "format_tags:stream=index,codec_type,codec_name"
                ":stream_disposition=default,forced,attached_pic:stream_tags="
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
        elif stream_type == "subtitle":
            codec_name = stream.get("codec_name")
            if codec_name in IMAGE_SUBTITLE_CODECS:
                continue
            metadata.subtitle_streams.append(
                SubtitleStream(
                    index=stream["index"],
                    codec=codec_name,
                    language=tags.get("language") or None,
                    title=tags.get("title") or None,
                    default=bool(disposition.get("default")),
                    forced=bool(disposition.get("forced")),
                )
            )

    if metadata.video_index is None:
        raise ConversionError("no primary video stream found")

    if "description" not in metadata.tags and "synopsis" in metadata.tags:
        metadata.tags["description"] = metadata.tags["synopsis"]
    if "comment" not in metadata.tags:
        purl = format_tags.get("purl")
        if purl:
            metadata.tags["comment"] = purl

    return metadata


def primary_av_codecs(probe: dict) -> tuple[str | None, str | None]:
    video_codec: str | None = None
    audio_codec: str | None = None

    for stream in probe.get("streams", []):
        stream_type = stream.get("codec_type")
        codec_name = stream.get("codec_name")
        disposition = stream.get("disposition") or {}

        if stream_type == "video":
            is_attached_pic = bool(disposition.get("attached_pic"))
            looks_like_cover = codec_name in IMAGE_CODECS and video_codec is not None
            if is_attached_pic or looks_like_cover:
                continue
            if video_codec is None:
                video_codec = codec_name
        elif stream_type == "audio" and audio_codec is None:
            audio_codec = codec_name

    return video_codec, audio_codec


def is_webm_compatible(video_codec: str | None, audio_codec: str | None) -> bool:
    return video_codec in WEBM_VIDEO_CODECS and (audio_codec is None or audio_codec in WEBM_AUDIO_CODECS)


def is_mp4_compatible(video_codec: str | None, audio_codec: str | None) -> bool:
    return video_codec in MP4_VIDEO_CODECS and (audio_codec is None or audio_codec in MP4_AUDIO_CODECS)


def resolve_remux_output_suffix(ffprobe_bin: str, media_path: Path) -> str | None:
    video_codec, audio_codec = primary_av_codecs(ffprobe_json(ffprobe_bin, media_path))

    if is_webm_compatible(video_codec, audio_codec):
        return ".webm"
    if is_mp4_compatible(video_codec, audio_codec):
        return ".mp4"
    return None


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


def _capture_video_frame(
    ffmpeg_bin: str,
    media_path: Path,
    video_index: int,
    timestamp: str,
    output_path: Path,
) -> subprocess.CompletedProcess[str]:
    output_path.unlink(missing_ok=True)
    return run_command(
        [
            ffmpeg_bin,
            "-y",
            "-v",
            "error",
            "-i",
            str(media_path),
            "-map",
            f"0:{video_index}",
            "-ss",
            timestamp,
            "-frames:v",
            "1",
            "-an",
            "-sn",
            "-dn",
            "-c:v",
            "libwebp",
            "-lossless",
            "1",
            str(output_path),
        ],
        check=False,
    )


def _is_decodable_image(ffmpeg_bin: str, image_path: Path) -> bool:
    if not image_path.exists() or image_path.stat().st_size == 0:
        return False
    completed = run_command(
        [
            ffmpeg_bin,
            "-v",
            "error",
            "-i",
            str(image_path),
            "-f",
            "null",
            "-",
        ],
        check=False,
    )
    return completed.returncode == 0


def generate_video_thumbnail(
    ffmpeg_bin: str,
    media_path: Path,
    video_index: int,
    temp_dir: Path,
) -> tuple[Path, str]:
    output_path = temp_dir / "generated-thumbnail.webp"
    attempts = [(GENERATED_THUMBNAIL_TIMESTAMP, "generated:20s"), ("0", "generated:first-frame")]
    last_attempt: subprocess.CompletedProcess[str] | None = None

    for timestamp, source_label in attempts:
        last_attempt = _capture_video_frame(ffmpeg_bin, media_path, video_index, timestamp, output_path)
        if last_attempt.returncode == 0 and _is_decodable_image(ffmpeg_bin, output_path):
            return output_path, source_label

    if last_attempt is not None and last_attempt.returncode != 0:
        last_attempt.check_returncode()
    raise ConversionError("ffmpeg did not produce a thumbnail frame")


def ffmpeg_supports_encoder(ffmpeg_bin: str, encoder_name: str) -> bool:
    completed = run_command([ffmpeg_bin, "-hide_banner", "-encoders"], check=False)
    if completed.returncode != 0:
        return False
    return encoder_name in completed.stdout


def copy_video_encoding_config() -> VideoEncodingConfig:
    return VideoEncodingConfig(mode="copy", codec="copy", extra_args=())


def cpu_video_encoding_config() -> VideoEncodingConfig:
    return VideoEncodingConfig(mode="cpu", codec="libx264", extra_args=("-preset", "medium", "-crf", "23"))


def nvidia_video_encoding_config() -> VideoEncodingConfig:
    return VideoEncodingConfig(
        mode="nvidia",
        codec="h264_nvenc",
        extra_args=("-preset", "p5", "-cq", "23", "-rc", "vbr"),
    )


def preferred_video_encoding_config(ffmpeg_bin: str) -> VideoEncodingConfig:
    if ffmpeg_supports_encoder(ffmpeg_bin, "h264_nvenc"):
        return nvidia_video_encoding_config()
    return cpu_video_encoding_config()


def resolve_video_handling_plan(ffprobe_bin: str, ffmpeg_bin: str, media_path: Path) -> tuple[str, VideoEncodingConfig]:
    remux_suffix = resolve_remux_output_suffix(ffprobe_bin, media_path)
    if remux_suffix is not None:
        return remux_suffix, copy_video_encoding_config()
    return ".mp4", preferred_video_encoding_config(ffmpeg_bin)


def build_ffmpeg_command(
    ffmpeg_bin: str,
    source_video: Path,
    output_path: Path,
    metadata: EntryMetadata,
    cover_path: Path | None,
    encoding: VideoEncodingConfig,
) -> list[str]:
    cmd = [ffmpeg_bin, "-y", "-v", "error", "-i", str(source_video)]

    cmd.extend(["-map", f"0:{metadata.video_index}"])
    if metadata.audio_index is not None:
        cmd.extend(["-map", f"0:{metadata.audio_index}"])

    cmd.extend(["-map_metadata", "-1", "-map_chapters", "-1", "-sn", "-dn", "-c:v:0", encoding.codec])
    if encoding.mode != "copy":
        cmd.extend(["-pix_fmt", "yuv420p"])
        cmd.extend(encoding.extra_args)

    if metadata.audio_index is not None:
        if encoding.mode == "copy":
            cmd.extend(["-c:a:0", "copy"])
        else:
            cmd.extend(["-c:a:0", "aac", "-b:a:0", "160k"])

    if metadata.video_language:
        cmd.extend(["-metadata:s:v:0", f"language={metadata.video_language}"])
    if metadata.audio_language and metadata.audio_index is not None:
        cmd.extend(["-metadata:s:a:0", f"language={metadata.audio_language}"])

    for key, value in metadata.tags.items():
        if value:
            cmd.extend(["-metadata", f"{key}={value}"])

    if output_path.suffix.lower() in {".mp4", ".m4v", ".mov"}:
        cmd.extend(["-movflags", "+faststart"])
    cmd.append(str(output_path))
    return cmd


def choose_cover(
    job: Job,
    metadata: EntryMetadata,
    ffmpeg_bin: str,
    temp_dir: Path,
) -> tuple[Path | None, str]:
    if job.external_cover is not None:
        return job.external_cover, f"external:{job.external_cover.name}"

    if job.cover_search_dir is not None:
        cover_match = find_best_cover(
            job.cover_match_source or job.source_video,
            job.cover_search_dir,
            metadata_title=metadata.tags.get("title"),
            minimum_score=0.5,
            allow_title_containment=True,
        )
        if cover_match is not None:
            return cover_match.path, f"external:{cover_match.path.name}"

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

    if metadata.video_index is None:
        raise ConversionError("no primary video stream found")
    return generate_video_thumbnail(ffmpeg_bin, job.source_video, metadata.video_index, temp_dir)


def cover_output_path_for_job(job: Job) -> Path:
    return job.output_file.with_suffix(".webp")


def subtitle_language_token(language: str | None) -> str:
    token = re.sub(r"[^A-Za-z0-9-]+", "", language or "")
    return token or "und"


def subtitle_output_path(output_file: Path, position: int, language: str | None) -> Path:
    return output_file.with_name(f"{output_file.stem}.{position}.{subtitle_language_token(language)}.vtt")


def existing_subtitle_sidecars(output_file: Path) -> list[Path]:
    if not output_file.parent.is_dir():
        return []
    pattern = re.compile(rf"^{re.escape(output_file.stem)}\.\d+\.[A-Za-z0-9-]+\.vtt$")
    return sorted(path for path in output_file.parent.iterdir() if path.is_file() and pattern.match(path.name))


def remove_subtitle_sidecars(output_file: Path) -> None:
    for path in existing_subtitle_sidecars(output_file):
        path.unlink(missing_ok=True)


def subtitle_label(stream: SubtitleStream, position: int) -> str:
    if stream.title:
        return stream.title
    if stream.language and stream.language != "und":
        return stream.language
    return f"Subtitle {position + 1}"


def expected_subtitle_sidecars(output_file: Path, streams: list[SubtitleStream]) -> list[dict[str, Any]]:
    """Describe the .vtt sidecars for an output; `path` is absolute and must be made relative by the caller."""
    return [
        {
            "path": subtitle_output_path(output_file, position, stream.language),
            "language": subtitle_language_token(stream.language),
            "label": subtitle_label(stream, position),
            "default": stream.default,
            "forced": stream.forced,
        }
        for position, stream in enumerate(streams)
    ]


def extract_subtitle_sidecars(
    ffmpeg_bin: str,
    source_video: Path,
    streams: list[SubtitleStream],
    output_file: Path,
) -> list[dict[str, Any]]:
    """Write every text subtitle stream as a WebVTT sidecar next to `output_file`.

    WebVTT streams (what yt-dlp embeds from YouTube) are copied verbatim so cue
    positioning survives; other text formats are converted to WebVTT.
    Stale sidecars from an earlier run are removed first.
    """
    remove_subtitle_sidecars(output_file)
    sidecars = expected_subtitle_sidecars(output_file, streams)
    if not sidecars:
        return []

    output_file.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg_bin, "-y", "-v", "error", "-i", str(source_video)]
    for stream, sidecar in zip(streams, sidecars):
        codec = "copy" if stream.codec == "webvtt" else "webvtt"
        cmd.extend(["-map", f"0:{stream.index}", "-c:s", codec, "-f", "webvtt", str(sidecar["path"])])
    try:
        run_command(cmd)
    except BaseException:
        remove_subtitle_sidecars(output_file)
        raise
    return sidecars


def extract_source_subtitles(
    ffmpeg_bin: str,
    ffprobe_bin: str,
    source_video: Path,
    output_file: Path,
) -> list[dict[str, Any]]:
    """Probe the source and write its subtitle sidecars (used to backfill already-converted outputs)."""
    try:
        metadata = collect_metadata(ffprobe_bin, source_video)
        return extract_subtitle_sidecars(ffmpeg_bin, source_video, metadata.subtitle_streams, output_file)
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or exc.stdout.strip() or "ffmpeg failed"
        raise ConversionError(f"subtitle extraction failed for {source_video}: {message}") from exc


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
