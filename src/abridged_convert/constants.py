from __future__ import annotations

VIDEO_EXTENSIONS = {".mkv", ".webm", ".mp4", ".mov", ".m4v", ".avi"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
IMAGE_CODECS = {"png", "mjpeg", "webp", "bmp", "gif"}
# Bitmap subtitle formats cannot become WebVTT without OCR, so they are skipped.
IMAGE_SUBTITLE_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
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
