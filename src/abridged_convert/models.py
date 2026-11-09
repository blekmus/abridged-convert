from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class ConversionError(RuntimeError):
    """Raised when conversion setup or an entry is invalid."""


@dataclass(frozen=True)
class SubtitleStream:
    index: int
    codec: str | None
    language: str | None = None
    title: str | None = None
    default: bool = False
    forced: bool = False


@dataclass
class EntryMetadata:
    tags: dict[str, str] = field(default_factory=dict)
    video_index: int | None = None
    audio_index: int | None = None
    attached_cover_index: int | None = None
    attached_cover_codec: str | None = None
    video_language: str | None = None
    audio_language: str | None = None
    subtitle_streams: list[SubtitleStream] = field(default_factory=list)


@dataclass
class Job:
    category: str
    entry_dir: Path
    source_video: Path
    output_file: Path
    external_cover: Path | None = None
    cover_search_dir: Path | None = None
    cover_match_source: Path | None = None


@dataclass
class ConversionResult:
    category: str
    entry_dir: Path
    source_video: Path
    output_file: Path
    success: bool = False
    skipped: bool = False
    cover_source: str | None = None
    # Subtitle sidecars written for this output: {path (absolute), language, label, default, forced}.
    # None means subtitles were not processed (e.g. dry run).
    subtitles: list[dict] | None = None
    mapped_tags: list[str] = field(default_factory=list)
    message: str = ""

    @classmethod
    def for_job(cls, job: Job, **fields: object) -> ConversionResult:
        return cls(
            category=job.category,
            entry_dir=job.entry_dir,
            source_video=job.source_video,
            output_file=job.output_file,
            **fields,
        )


@dataclass(frozen=True)
class VideoEncodingConfig:
    mode: str
    codec: str
    extra_args: tuple[str, ...]


@dataclass
class JobState:
    label: str
    detail: str
    status: str = "pending"
    message: str = ""
