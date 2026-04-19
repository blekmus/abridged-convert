from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class ConversionError(RuntimeError):
    """Raised when conversion setup or an entry is invalid."""


@dataclass
class EntryMetadata:
    tags: dict[str, str] = field(default_factory=dict)
    video_index: int | None = None
    audio_index: int | None = None
    attached_cover_index: int | None = None
    attached_cover_codec: str | None = None
    video_language: str | None = None
    audio_language: str | None = None


@dataclass
class Job:
    category: str
    entry_dir: Path
    source_video: Path
    output_file: Path
    external_cover: Path | None = None


@dataclass
class ConversionResult:
    category: str
    entry_dir: Path
    source_video: Path
    output_file: Path
    success: bool = False
    skipped: bool = False
    cover_source: str | None = None
    mapped_tags: list[str] = field(default_factory=list)
    message: str = ""


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
