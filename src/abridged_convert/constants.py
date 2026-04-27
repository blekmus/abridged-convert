from __future__ import annotations

VIDEO_EXTENSIONS = {".mkv", ".webm", ".mp4", ".mov", ".m4v", ".avi"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
IMAGE_CODECS = {"png", "mjpeg", "webp", "bmp", "gif"}
PREFERRED_TAG_ORDER = (
    "title",
    "artist",
    "album",
    "date",
    "description",
    "comment",
    "synopsis",
)
CATEGORY_ORDER = ("Series", "Shots", "Shorts", "Songs")
GPU_ERROR_MARKERS = (
    "cannot load libcuda",
    "cannot init cuda",
    "cuda error",
    "cuinit",
    "nvenc",
    "no capable devices found",
    "operation not permitted",
    "permission denied",
)
