from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import abridged_convert.conversion as conversion
from abridged_convert.app import main
from abridged_convert.conversion import tracking_file_for_job
from abridged_convert.discovery import compact_label, discover_jobs
from abridged_convert.media import build_ffmpeg_command, extract_embedded_cover, write_cover_sidecar
from abridged_convert.models import VideoEncodingConfig
from abridged_convert.ui import TerminalUI


FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def ffprobe_json(path: Path) -> dict:
    completed = run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=format_name:format_tags:stream=index,codec_type,codec_name,disposition:stream_tags",
            "-of",
            "json",
            str(path),
        ]
    )
    return json.loads(completed.stdout)


def normalize_probe_tags(probe: dict) -> dict[str, str]:
    return {key.lower(): value for key, value in probe.get("format", {}).get("tags", {}).items()}


class AbridgedConvertTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        for tool in (FFMPEG, FFPROBE):
            subprocess.run([tool, "-version"], check=True, capture_output=True, text=True)

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="abridged-convert-test-")
        self.repo_root = Path(self.temp_dir.name)
        for category in ("Shots", "Shorts", "Series"):
            (self.repo_root / category).mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_default_run_processes_all_roots(self) -> None:
        shots_entry = self.repo_root / "Shots" / "[Creator] Shot Entry"
        shots_entry.mkdir()
        self._make_cover_image(shots_entry / "cover.png", "red")
        self._make_source_video(
            shots_entry / "1.mkv",
            title="Shot Title",
            description="Shot Description",
            comment="Shot Comment",
        )

        shorts_entry = self.repo_root / "Shorts" / "[Creator] Short Entry"
        shorts_entry.mkdir()
        self._make_cover_image(shorts_entry / "cover.jpg", "yellow")
        self._make_source_video(
            shorts_entry / "1.webm",
            title="Short Title",
            description="Short Description",
            comment="Short Comment",
        )

        series_entry = self.repo_root / "Series" / "[Creator] Series Entry"
        (series_entry / "covers").mkdir(parents=True)
        self._make_cover_image(series_entry / "covers" / "1.png", "green")
        self._make_source_video(
            series_entry / "Episode 1 - Pilot.mkv",
            title="Episode 1 - Pilot",
            description="Series Description",
            comment="Series Comment",
        )

        exit_code = main(["--repo-root", str(self.repo_root)])

        self.assertEqual(exit_code, 0)
        self.assertTrue((self.repo_root / "Web" / "Shots" / "[Creator] Shot Entry" / "1.mp4").exists())
        self.assertTrue((self.repo_root / "Web" / "Shots" / "[Creator] Shot Entry" / "1.webp").exists())
        self.assertTrue((self.repo_root / "Web" / "Shorts" / "[Creator] Short Entry" / "1.mp4").exists())
        self.assertTrue((self.repo_root / "Web" / "Shorts" / "[Creator] Short Entry" / "1.webp").exists())
        self.assertTrue(
            (self.repo_root / "Web" / "Series" / "[Creator] Series Entry" / "Episode 1 - Pilot.mp4").exists()
        )
        self.assertTrue(
            (self.repo_root / "Web" / "Series" / "[Creator] Series Entry" / "Episode 1 - Pilot.webp").exists()
        )

    def test_external_cover_wins_over_embedded_cover(self) -> None:
        entry = self.repo_root / "Shots" / "[Creator] Embedded First"
        entry.mkdir()
        external_cover = entry / "cover.webp"
        embedded_cover = self.repo_root / "embedded-cover.png"
        self._make_cover_image(external_cover, "red")
        self._make_cover_image(embedded_cover, "blue")
        self._make_source_video(
            entry / "1.mkv",
            title="Embedded Cover",
            description="desc",
            comment="comment",
            add_cover_stream=embedded_cover,
        )

        exit_code = main(["--repo-root", str(self.repo_root), "--category", "Shots"])

        self.assertEqual(exit_code, 0)
        output = self.repo_root / "Web" / "Shots" / "[Creator] Embedded First" / "1.mp4"
        cover_output = self.repo_root / "Web" / "Shots" / "[Creator] Embedded First" / "1.webp"
        probe = ffprobe_json(output)
        self.assertEqual(len(probe["streams"]), 3)
        self.assertEqual(probe["streams"][2]["codec_name"], "png")
        self.assertTrue(cover_output.exists())
        self.assertEqual(ffprobe_json(cover_output)["streams"][0]["codec_name"], "webp")
        self.assertEqual(cover_output.read_bytes(), external_cover.read_bytes())

    def test_extract_embedded_webp_cover_preserves_original_bytes(self) -> None:
        embedded_cover = self.repo_root / "embedded-cover.webp"
        self._make_cover_image(embedded_cover, "blue")
        temp_dir = self.repo_root / "extract-temp"
        temp_dir.mkdir()

        extracted_cover = extract_embedded_cover(
            FFMPEG,
            embedded_cover,
            0,
            "webp",
            temp_dir,
        )

        self.assertEqual(extracted_cover.suffix.lower(), ".webp")
        self.assertEqual(extracted_cover.read_bytes(), embedded_cover.read_bytes())

    def test_shot_output_title_and_artist_are_derived_from_entry_name(self) -> None:
        entry = self.repo_root / "Shots" / "[MyToasterIsMoist] Interspecies Reviewers Abridged"
        entry.mkdir()
        self._make_source_video(
            entry / "1.mkv",
            title="Wrong Source Title",
            artist="Wrong Source Artist",
            description="desc",
            comment="comment",
        )

        exit_code = main(["--repo-root", str(self.repo_root), "--category", "Shots"])

        self.assertEqual(exit_code, 0)
        output = self.repo_root / "Web" / "Shots" / "[MyToasterIsMoist] Interspecies Reviewers Abridged" / "1.mp4"
        tags = normalize_probe_tags(ffprobe_json(output))
        self.assertEqual(tags["title"], "Interspecies Reviewers Abridged")
        self.assertEqual(tags["artist"], "MyToasterIsMoist")

    def test_series_output_title_and_artist_are_derived_from_entry_and_episode_names(self) -> None:
        entry = self.repo_root / "Series" / "[2GS BootLegged] Fire Hoez"
        entry.mkdir()
        self._make_source_video(
            entry / "Episode 1 - something.mkv",
            title="Wrong Episode Title",
            artist="Wrong Source Artist",
            description="desc",
            comment="comment",
        )

        exit_code = main(["--repo-root", str(self.repo_root), "--category", "Series"])

        self.assertEqual(exit_code, 0)
        output = self.repo_root / "Web" / "Series" / "[2GS BootLegged] Fire Hoez" / "Episode 1 - something.mp4"
        tags = normalize_probe_tags(ffprobe_json(output))
        self.assertEqual(tags["title"], "Fire Hoez - Episode 1 - something")
        self.assertEqual(tags["artist"], "2GS BootLegged")

    def test_series_uses_best_matching_external_cover_when_embedded_missing(self) -> None:
        entry = self.repo_root / "Series" / "[Creator] Matching Covers"
        (entry / "covers").mkdir(parents=True)
        self._make_cover_image(entry / "covers" / "1.png", "green")
        self._make_cover_image(entry / "covers" / "2.png", "red")
        self._make_source_video(
            entry / "Episode 1 - Pilot.mkv",
            title="Episode 1 - Pilot",
            description="desc",
            comment="comment",
        )

        exit_code = main(["--repo-root", str(self.repo_root), "--category", "Series"])

        self.assertEqual(exit_code, 0)
        output = self.repo_root / "Web" / "Series" / "[Creator] Matching Covers" / "Episode 1 - Pilot.mp4"
        cover_output = self.repo_root / "Web" / "Series" / "[Creator] Matching Covers" / "Episode 1 - Pilot.webp"
        probe = ffprobe_json(output)
        self.assertEqual(len(probe["streams"]), 3)
        self.assertEqual(probe["streams"][2]["codec_name"], "png")
        self.assertTrue(cover_output.exists())
        self.assertEqual(ffprobe_json(cover_output)["streams"][0]["codec_name"], "webp")

    def test_series_generates_cover_when_no_external_or_embedded_cover_exists(self) -> None:
        entry = self.repo_root / "Series" / "[Creator] No Matching Covers"
        (entry / "covers").mkdir(parents=True)
        self._make_cover_image(entry / "covers" / "art-a.png", "green")
        self._make_cover_image(entry / "covers" / "bonus-image.png", "red")
        self._make_source_video(
            entry / "Episode 1 - Pilot.mkv",
            title="Pilot",
            description="desc",
            comment="comment",
            duration=21,
        )

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = main(["--repo-root", str(self.repo_root), "--category", "Series"])

        self.assertEqual(exit_code, 0)
        output = self.repo_root / "Web" / "Series" / "[Creator] No Matching Covers" / "Episode 1 - Pilot.mp4"
        cover_output = self.repo_root / "Web" / "Series" / "[Creator] No Matching Covers" / "Episode 1 - Pilot.webp"
        probe = ffprobe_json(output)
        self.assertEqual(len(probe["streams"]), 3)
        self.assertEqual(probe["streams"][2]["codec_name"], "png")
        self.assertTrue(cover_output.exists())
        self.assertEqual(ffprobe_json(cover_output)["streams"][0]["codec_name"], "webp")
        self.assertIn("cover=generated:20s", stdout.getvalue())

    def test_series_prefers_exact_titled_cover_over_numeric_episode_art(self) -> None:
        entry = self.repo_root / "Series" / "[Creator] Mixed Cover Signals"
        covers_dir = entry / "covers"
        covers_dir.mkdir(parents=True)
        titled_cover = covers_dir / "This is a College Class on Rap.webp"
        numeric_cover = covers_dir / "0.5.png"
        self._make_cover_image(titled_cover, "green")
        self._make_cover_image(numeric_cover, "red")
        self._make_source_video(
            entry / "Episode 0.5 - This is a College Class on Rap.mkv",
            title="Episode 0.5 - This is a College Class on Rap",
            description="desc",
            comment="comment",
        )

        exit_code = main(["--repo-root", str(self.repo_root), "--category", "Series"])

        self.assertEqual(exit_code, 0)
        cover_output = (
            self.repo_root / "Web" / "Series" / "[Creator] Mixed Cover Signals" / "Episode 0.5 - This is a College Class on Rap.webp"
        )
        self.assertTrue(cover_output.exists())
        self.assertEqual(cover_output.read_bytes(), titled_cover.read_bytes())

    def test_series_uses_embedded_cover_when_hybrid_filesystem_match_returns_none(self) -> None:
        entry = self.repo_root / "Series" / "[Creator] Embedded Series Cover"
        covers_dir = entry / "covers"
        covers_dir.mkdir(parents=True)
        self._make_cover_image(covers_dir / "0.1.png", "green")
        self._make_cover_image(covers_dir / "bonus.png", "yellow")
        embedded_cover = self.repo_root / "embedded-series-cover.png"
        self._make_cover_image(embedded_cover, "blue")
        self._make_source_video(
            entry / "Episode 14 - Kurosai Cinematic Universe.mkv",
            title="Episode 14 - Kurosai Cinematic Universe",
            description="desc",
            comment="comment",
            add_cover_stream=embedded_cover,
        )

        exit_code = main(["--repo-root", str(self.repo_root), "--category", "Series"])

        self.assertEqual(exit_code, 0)
        output = self.repo_root / "Web" / "Series" / "[Creator] Embedded Series Cover" / "Episode 14 - Kurosai Cinematic Universe.mp4"
        cover_output = (
            self.repo_root / "Web" / "Series" / "[Creator] Embedded Series Cover" / "Episode 14 - Kurosai Cinematic Universe.webp"
        )
        probe = ffprobe_json(output)
        self.assertEqual(len(probe["streams"]), 3)
        self.assertEqual(probe["streams"][2]["codec_name"], "png")
        self.assertTrue(cover_output.exists())
        expected_cover_output = self.repo_root / "expected-embedded-series-cover.webp"
        write_cover_sidecar(FFMPEG, embedded_cover, expected_cover_output)
        self.assertEqual(cover_output.read_bytes(), expected_cover_output.read_bytes())

    def test_builds_nvenc_command_when_requested(self) -> None:
        metadata = type(
            "Metadata",
            (),
            {
                "video_index": 0,
                "audio_index": 1,
                "video_language": None,
                "audio_language": None,
                "tags": {},
            },
        )()
        cmd = build_ffmpeg_command(
            "ffmpeg",
            Path("input.mkv"),
            Path("output.mp4"),
            metadata,
            None,
            VideoEncodingConfig(mode="nvidia", codec="h264_nvenc", extra_args=("-preset", "p5", "-cq", "23")),
        )

        self.assertIn("h264_nvenc", cmd)

    def test_compact_label_uses_title_creator_and_episode(self) -> None:
        job = type(
            "JobLike",
            (),
            {
                "category": "Series",
                "entry_dir": Path("[Creator] Great Show"),
                "source_video": Path("Episode 2 - Return.mkv"),
                "output_file": Path("ignored"),
            },
        )()

        self.assertEqual(compact_label(job), "Series  [Creator] Great Show  Episode 2 - Return")

    def test_tui_queue_uses_status_symbols_without_pending_rows(self) -> None:
        ui = TerminalUI(
            repo_root=self.repo_root,
            categories=["Shots"],
            encoding=VideoEncodingConfig(mode="cpu", codec="libx264", extra_args=()),
            jobs=[],
            discovery_failures=[],
            persist_final_report=False,
            stream=self._fake_stream(isatty=True),
        )
        ui.states = [
            type("State", (), {"status": "pending", "label": "one", "detail": "queued", "message": ""})(),
            type("State", (), {"status": "running", "label": "two", "detail": "working", "message": ""})(),
            type("State", (), {"status": "done", "label": "three", "detail": "done", "message": "embedded"})(),
            type("State", (), {"status": "failed", "label": "four", "detail": "boom", "message": ""})(),
        ]

        lines = ui._queue_lines()

        self.assertTrue(any("▶ working" in line for line in lines))
        self.assertTrue(any("✓ done" in line for line in lines))
        self.assertTrue(any("✗ failed" in line for line in lines))
        self.assertFalse(any("pending" in line for line in lines))

    def test_tui_highlights_creator_brackets_in_purple(self) -> None:
        ui = TerminalUI(
            repo_root=self.repo_root,
            categories=["Shots"],
            encoding=VideoEncodingConfig(mode="cpu", codec="libx264", extra_args=()),
            jobs=[],
            discovery_failures=[],
            persist_final_report=False,
            stream=self._fake_stream(isatty=True),
        )

        line = ui._format_state_line(
            type("State", (), {"status": "done", "label": "Shots  [Creator] Great Show", "detail": "done", "message": ""})()
        )

        self.assertIn(f"{ui.ANSI_BRIGHT_MAGENTA}[Creator]{ui.ANSI_RESET}", line)

    def test_tui_prints_persistent_plain_report_for_dry_run(self) -> None:
        class Stream:
            def __init__(self) -> None:
                self.parts: list[str] = []

            def write(self, value: str) -> None:
                self.parts.append(value)

            def flush(self) -> None:
                return None

            def isatty(self) -> bool:
                return True

        stream = Stream()
        ui = TerminalUI(
            repo_root=self.repo_root,
            categories=["Series"],
            encoding=VideoEncodingConfig(mode="cpu", codec="libx264", extra_args=()),
            jobs=[],
            discovery_failures=[],
            persist_final_report=True,
            stream=stream,
        )
        ui.states = [
            type("State", (), {"status": "done", "label": "Series  [Creator] Show  Episode 1", "detail": "dry run", "message": "external:cover.png"})(),
            type("State", (), {"status": "failed", "label": "Series  [Creator] Show  Episode 2", "detail": "bad match", "message": ""})(),
        ]
        ui.enabled = True
        ui._active = True
        ui.final_message = "converted=1 skipped=0 failed=1"

        ui.finish(1)

        output = "".join(stream.parts)
        self.assertIn("Dry-run queue summary", output)
        self.assertIn("Summary: converted=1 skipped=0 failed=1", output)
        self.assertIn("[OK] Series  [Creator] Show  Episode 1 | dry run | external:cover.png", output)
        self.assertIn("[FAIL] Series  [Creator] Show  Episode 2 | bad match", output)

    def test_existing_tracking_file_deletes_partial_output_and_restarts(self) -> None:
        entry = self.repo_root / "Shots" / "[Creator] Resume Me"
        entry.mkdir()
        self._make_source_video(
            entry / "1.mkv",
            title="Resume Me",
            description="desc",
            comment="comment",
        )

        jobs, _ = discover_jobs(self.repo_root, ["Shots"])
        job = jobs[0]
        job.output_file.parent.mkdir(parents=True, exist_ok=True)
        job.output_file.write_text("partial output", encoding="utf-8")
        tracking_file = tracking_file_for_job(job)
        self.assertEqual(tracking_file.parent, job.output_file.parent)
        self.assertNotEqual(tracking_file.parent, job.entry_dir)
        tracking_file.write_text(f"{job.output_file}\n", encoding="utf-8")

        exit_code = main(["--repo-root", str(self.repo_root), "--category", "Shots"])

        self.assertEqual(exit_code, 0)
        self.assertFalse(tracking_file.exists())
        probe = ffprobe_json(job.output_file)
        self.assertEqual(probe["format"]["format_name"], "mov,mp4,m4a,3gp,3g2,mj2")

    def test_keyboard_interrupt_keeps_tracking_file_and_next_run_resumes(self) -> None:
        entry = self.repo_root / "Shots" / "[Creator] Interrupted"
        entry.mkdir()
        self._make_source_video(
            entry / "1.mkv",
            title="Interrupted",
            description="desc",
            comment="comment",
        )

        jobs, _ = discover_jobs(self.repo_root, ["Shots"])
        job = jobs[0]
        tracking_file = tracking_file_for_job(job)
        original_run_command = conversion.run_command

        def interrupting_run_command(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
            if cmd[0] == FFMPEG and cmd[-1] == str(job.output_file):
                job.output_file.parent.mkdir(parents=True, exist_ok=True)
                job.output_file.write_text("partial output", encoding="utf-8")
                raise KeyboardInterrupt
            return original_run_command(cmd, check=check)

        interrupted_stdout = io.StringIO()
        interrupted_stderr = io.StringIO()
        with mock.patch("abridged_convert.conversion.run_command", side_effect=interrupting_run_command):
            with redirect_stdout(interrupted_stdout), redirect_stderr(interrupted_stderr):
                exit_code = main(["--repo-root", str(self.repo_root), "--category", "Shots"])

        self.assertEqual(exit_code, 130)
        self.assertIn("Interrupted.", interrupted_stderr.getvalue())
        self.assertNotIn("Traceback", interrupted_stderr.getvalue())
        self.assertTrue(tracking_file.exists())
        self.assertFalse(job.output_file.exists())

        resumed_stdout = io.StringIO()
        resumed_stderr = io.StringIO()
        with redirect_stdout(resumed_stdout), redirect_stderr(resumed_stderr):
            resumed_exit_code = main(["--repo-root", str(self.repo_root), "--category", "Shots"])

        self.assertEqual(resumed_exit_code, 0)
        self.assertFalse(tracking_file.exists())
        probe = ffprobe_json(job.output_file)
        self.assertEqual(probe["format"]["format_name"], "mov,mp4,m4a,3gp,3g2,mj2")

    def _fake_stream(self, *, isatty: bool):
        class Stream:
            def write(self, _: str) -> None:
                return None

            def flush(self) -> None:
                return None

            def isatty(self) -> bool:
                return isatty

        return Stream()

    def _make_cover_image(self, output_path: Path, color: str) -> None:
        run(
            [
                FFMPEG,
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=160x90:d=1",
                "-frames:v",
                "1",
                str(output_path),
            ]
        )

    def _make_source_video(
        self,
        output_path: Path,
        *,
        title: str,
        description: str,
        comment: str,
        artist: str = "Artist",
        album: str = "Album",
        date: str = "20240202",
        synopsis: str = "Synopsis",
        attach_json: bool = False,
        add_cover_stream: Path | None = None,
        add_cover_codec: str | None = None,
        add_cover_attached_pic: bool = True,
        duration: float = 1,
    ) -> None:
        cmd = [
            FFMPEG,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=160x90:rate=12:duration={duration}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=1000:duration={duration}",
        ]

        if add_cover_stream is not None:
            cmd.extend(["-i", str(add_cover_stream)])

        cmd.extend(["-map", "0:v:0", "-map", "1:a:0"])

        if add_cover_stream is not None:
            cmd.extend(["-map", "2:v:0"])

        cmd.extend(["-c:v:0", "libvpx-vp9", "-pix_fmt", "yuv420p"])

        if output_path.suffix.lower() == ".webm":
            cmd.extend(["-c:a:0", "libopus"])
        else:
            cmd.extend(["-c:a:0", "aac"])

        if add_cover_stream is not None:
            cmd.extend(["-c:v:1", add_cover_codec or "png"])
            if add_cover_attached_pic:
                cmd.extend(["-disposition:v:1", "attached_pic"])

        cmd.extend(
            [
                "-metadata",
                f"title={title}",
                "-metadata",
                f"artist={artist}",
                "-metadata",
                f"album={album}",
                "-metadata",
                f"date={date}",
                "-metadata",
                f"description={description}",
                "-metadata",
                f"comment={comment}",
                "-metadata",
                f"synopsis={synopsis}",
            ]
        )

        json_path: Path | None = None
        if attach_json:
            json_path = output_path.parent / "temp.info.json"
            json_path.write_text('{"source":"fixture"}\n', encoding="utf-8")
            cmd.extend(["-attach", str(json_path), "-metadata:s:t", "mimetype=application/json"])

        cmd.append(str(output_path))
        run(cmd)

        if json_path is not None and json_path.exists():
            json_path.unlink()


if __name__ == "__main__":
    unittest.main()
