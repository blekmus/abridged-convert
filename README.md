# abridged-convert

`abridged-convert` is a standalone CLI for preparing entries from the abridged archive layout into mirrored web outputs.

Install it as a real command:

```bash
uv tool install -e ./abridged-convert
```

That gives you an `abridged-convert` executable you can run from anywhere.

Default behavior:

- if `INPUT_DIR` contains `Series`, `Shots`, `Shorts`, and/or `Songs`, it reads every category it finds there
- if `INPUT_DIR` is one category directory such as `Series`, it only reads that category
- writes to `OUTPUT_DIR/Series`, `OUTPUT_DIR/Shots`, `OUTPUT_DIR/Shorts`, and `OUTPUT_DIR/Songs`
- maps `Songs/[~] AMVs/*.mkv` to `OUTPUT_DIR/Songs/AMV/<entry>/1.mp4` or `1.webm` with matched covers from `Songs/[~] AMVs/covers`
- preserves metadata
- writes `OUTPUT_DIR/.abridged-video-metadata.json` after conversion for the web catalog
- prefers discovered cover image files first, then embedded cover art, otherwise generates a thumbnail from the video at the 20 second mark, falling back to the first frame for shorter videos
- writes the chosen or generated cover beside each output video as a `.webp` sidecar, copying existing WebP covers directly and converting other formats losslessly when possible
- copies/remuxes existing video and audio streams by default without re-encoding

Usage:

```bash
abridged-convert . Web
abridged-convert . Web --overwrite
abridged-convert . Web --dry-run
abridged-convert ./Series ./Web
```

By default, compatible video/audio streams are copied without re-encoding. Outputs are
always `.mp4` or `.webm`, never `.mkv`, and covers are written as sibling `.webp` files
rather than embedded into the video. If a source's streams do not fit either supported
container, `abridged-convert` transcodes to MP4, preferring NVIDIA NVENC when available
and falling back to CPU/libx264.

Deploy mode:

```bash
abridged-convert Archive Web --deploy bitpile:Abridged
abridged-convert Archive Web --deploy bitpile:Abridged --deploy-dry-run
abridged-convert Archive Web --deploy bitpile:Abridged --keep-staging
abridged-convert Archive Web --bootstrap-deploy
abridged-convert Archive Web --deploy bitpile:Abridged --bootstrap-deploy
```

Deploy mode treats `OUTPUT_DIR` as temporary staging. It downloads the remote
`.abridged-video-metadata.json`, uses each item's deploy tracking fields to
convert only new or changed files, uploads staged media and the updated metadata
file with `rclone`, deletes remote files that are no longer expected, then
removes the local staging directory unless `--keep-staging` is used. The rclone
remote must point at the dedicated catalog folder, not a shared parent
directory.

Use `--bootstrap-deploy` when `OUTPUT_DIR` already contains the converted MP4
and WebP files but the root `.abridged-video-metadata.json` file does not exist
yet. With `--deploy`, the bootstrap run uploads the existing outputs and
generated metadata without re-encoding.
