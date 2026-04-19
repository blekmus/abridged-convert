# abridged-convert

`abridged-convert` is a standalone CLI for converting entries from the abridged archive layout into mirrored MP4 outputs.

Install it as a real command:

```bash
uv tool install -e ./abridged-convert
```

That gives you an `abridged-convert` executable you can run from anywhere.

Default behavior:

- if `INPUT_DIR` contains `Shots`, `Shorts`, and/or `Series`, it reads every category it finds there
- if `INPUT_DIR` is one category directory such as `Series`, it only reads that category
- writes to `OUTPUT_DIR/Shots`, `OUTPUT_DIR/Shorts`, and `OUTPUT_DIR/Series`
- preserves metadata
- prefers discovered cover image files first, then embedded cover art, otherwise no cover
- writes the chosen cover beside each output video as a `.webp` sidecar when one is available, copying existing WebP covers directly and converting other formats losslessly when possible
- prefers NVIDIA GPU encoding when `h264_nvenc` is available, otherwise falls back to CPU

Usage:

```bash
abridged-convert . Web
abridged-convert . Web --overwrite
abridged-convert . Web --dry-run
abridged-convert . Web --video-mode nvidia
abridged-convert . Web --video-mode cpu
abridged-convert ./Series ./Web
```
