# abridged-convert

`abridged-convert` is a standalone CLI for preparing entries from the abridged archive layout into mirrored web outputs.

Install it as a real command:

```bash
uv tool install -e ./abridged-convert
```

That gives you an `abridged-convert` executable you can run from anywhere.

Run `abridged-convert --help` (or `abridged-convert <command> --help`) for the same examples shown here. Help is coloured in a terminal; set `NO_COLOR=1` to turn that off, or `FORCE_COLOR=1` to keep colour when piping (e.g. into `less -R`).

## Commands

```bash
# Convert the whole archive into Web/
abridged-convert convert abridged Web

# Only one category (repeat --category for more; any case works)
abridged-convert convert abridged Web --category Series

# Only one entry: tab-complete the real path (repeat --entry for more)
abridged-convert convert abridged Web --entry "abridged/Series/[Creator] Show"
abridged-convert convert abridged Web --entry "abridged/Songs/[~] AMVs/[Creator] Song.mkv"

# Preview a deploy, then run it
abridged-convert deploy abridged bitpile:Abridged --dry-run
abridged-convert deploy abridged bitpile:Abridged
# Interrupted? Run the same command again to resume.

# Recovery only: Web/ has videos but .abridged-video-metadata.json is gone
abridged-convert rebuild-metadata abridged Web --upload bitpile:Abridged
```

Path rules. There is exactly one accepted form:

- `ARCHIVE` is always the folder that contains `Series/`, `Shots/`, `Shorts/` and `Songs/`, never a category folder such as `abridged/Series`.
- `OUTPUT` (for `convert` and `rebuild-metadata`) is always the web root, never `Web/Series`. Category folders are created inside it.
- Narrow a run with `--category Series|Shots|Shorts|Songs` or `--entry PATH`, never with a deeper path. A category-level path is rejected with the corrected command.
- `--entry` takes an entry folder `ARCHIVE/<Category>/<entry>` or an AMV file `ARCHIVE/Songs/[~] AMVs/<file>`.
- A run owns its scope: inside the selected categories or entries, anything that no longer exists in the archive is pruned. Nothing outside the scope is touched.

The ffmpeg, ffprobe and rclone paths can be overridden with `--ffmpeg`, `--ffprobe` and `--rclone`, listed under "advanced" in each command's help.

## What `convert` produces

- writes to `OUTPUT/Series`, `OUTPUT/Shots`, `OUTPUT/Shorts`, and `OUTPUT/Songs`
- maps `Songs/[~] AMVs/*.mkv` to `OUTPUT/Songs/AMV/<entry>/1.mp4` or `1.webm` with matched covers from `Songs/[~] AMVs/covers`
- preserves metadata
- writes `OUTPUT/.abridged-video-metadata.json` after conversion for the web catalog
- prefers discovered cover image files first, then embedded cover art, otherwise generates a thumbnail from the video at the 20 second mark, falling back to the first frame for shorter videos
- writes the chosen or generated cover beside each output video as a `.webp` sidecar, copying existing WebP covers directly and converting other formats losslessly when possible
- copies/remuxes existing video and audio streams by default without re-encoding
- writes each embedded text subtitle track as a `.vtt` sidecar named `<stem>.<n>.<language>.vtt` (WebVTT tracks, as embedded by yt-dlp from YouTube, are copied verbatim; other text formats are converted; image-based tracks such as PGS are skipped) and lists them under `subtitles` in the metadata:

  ```json
  "subtitles": [
    {"path": "Shots/[Creator] Shot/1.0.en.vtt", "language": "en", "label": "en", "default": true, "forced": false}
  ]
  ```

  `label` is the track title when the source has one, otherwise the language. An empty list means the source has no subtitles. A missing `subtitles` key means the entry was converted before subtitle support existed: local runs and deploys backfill it once from the source, extracting and uploading only the `.vtt` files without re-converting or re-uploading the video.

By default, compatible video/audio streams are copied without re-encoding. Outputs are
always `.mp4` or `.webm`, never `.mkv`, and covers are written as sibling `.webp` files
rather than embedded into the video. If a source's streams do not fit either supported
container, `abridged-convert` transcodes to MP4, preferring NVIDIA NVENC when available
and falling back to CPU/libx264.

## How `deploy` works

`deploy ARCHIVE REMOTE` stages converted files in a fresh temp dir
(`/tmp/abridged-deploy-*`; set `TMPDIR` to move it) and leaves nothing behind
after a successful run.

While a run is unfinished, `ARCHIVE/.abridged-deploy.json` records the remote
and the temp dir. Running the same command again resumes from there. If the temp
dir is gone (e.g. after a reboot), or the file names a different remote, the run
starts again from the remote metadata. Anything uploaded but not yet listed in
the remote catalog is re-converted and re-uploaded to the same paths. A failed
or interrupted run keeps both so it can resume; a successful run removes both.
Dry runs never write the file.

Deploy downloads the remote `.abridged-video-metadata.json` and treats it as
the source of truth for already-uploaded files. Unchanged entries are matched by the recorded
`source.path` and `source.size`, so deploy startup does not `ffprobe` every file
in the selected archive tree. `ffprobe` is only run for entries that are new,
changed, forced with `--overwrite`, or missing staged output during resume.

The resume journal is `.abridged-video-metadata.json` inside the temp staging dir.
A clean deploy creates a `deployRunSignature`; if the run is interrupted, the
next run sees the in-progress signature and resumes from the local journal
instead of downloading the remote metadata again. Successfully converted entries
are written to the journal immediately with a local `uploaded: false` marker.
During upload, that marker is cleared only after both the video and cover are
uploaded. Local-only journal fields are stripped from the remote metadata, while
`deployRunSignature` is kept as the run identifier.

Every deploy also lists the remote and deletes files inside its scope that the
catalog no longer references, such as leftovers from a run that died between
uploading the catalog and deleting old files. Only files inside category folders
are considered, never other files at the remote root. If most of the remote
looks orphaned, deploy deletes nothing and prints a warning instead, because
that points to a broken catalog rather than stray files.

The end-of-run report lists every remote file deploy (or
`rebuild-metadata --upload`) deleted as `✓ removed <path> deleted (<reason>)`,
or with `--dry-run` as `✓ removing <path> deploy dry run (<reason>)`. The reason
is `removed entry` (the entry's source is gone or was replaced), `orphan` (no
catalog entry references it) or `stale subtitle` (a re-conversion replaced the
sidecar). Locally pruned outputs from `convert` are listed as `✓ pruned <path>`.
On a terminal the verb is red, the path white and the note dim; `NO_COLOR` /
`FORCE_COLOR` override that.

When there are no changed entries and no dangling remote entries to prune,
deploy skips uploading the metadata file because the remote metadata would be
unchanged. When entries are pruned, deploy deletes the recorded remote
`outputPath` and `coverPath`, then removes the now-empty entry directory. The
rclone remote must point at the dedicated catalog folder, not a shared parent
directory.

## Recovery: `rebuild-metadata`

Use `rebuild-metadata ARCHIVE OUTPUT` only when `OUTPUT` already contains the
converted videos and WebP covers but the root `.abridged-video-metadata.json`
is missing, e.g. after losing the remote catalog's metadata. It rebuilds the
file from the existing outputs without re-encoding. With `--upload REMOTE` it
also uploads the outputs and the rebuilt metadata. `OUTPUT` is always kept,
since in a recovery it is the only copy of the converted files.
