from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from abridged_convert.app import parse_args, resolve_cli_dirs
from abridged_convert.discovery import discover_jobs, resolve_input_roots


class EntrypointBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="abridged-convert-entrypoint-")
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_parse_args_accepts_required_positional_dirs(self) -> None:
        parser, args = parse_args([".", "Web"])

        input_dir, output_dir = resolve_cli_dirs(parser, args)

        self.assertEqual(input_dir, Path.cwd())
        self.assertEqual(output_dir, Path.cwd() / "Web")

    def test_root_input_processes_all_present_categories(self) -> None:
        shots_entry = self.root / "Shots" / "[Creator] Shot Entry"
        series_entry = self.root / "Series" / "[Creator] Series Entry"
        shots_entry.mkdir(parents=True)
        series_entry.mkdir(parents=True)
        (shots_entry / "1.mkv").write_text("video", encoding="utf-8")
        (series_entry / "Episode 1.mkv").write_text("video", encoding="utf-8")

        category_roots, categories, default_output_root = resolve_input_roots(self.root, ["Series", "Shots", "Shorts"])
        jobs, failures = discover_jobs(self.root, categories, self.root / "Web")

        self.assertEqual(categories, ["Series", "Shots"])
        self.assertEqual(set(category_roots), {"Series", "Shots"})
        self.assertEqual(default_output_root, self.root / "Web")
        self.assertEqual(len(failures), 0)
        self.assertEqual({job.category for job in jobs}, {"Series", "Shots"})
        self.assertIn(self.root / "Web" / "Series" / "[Creator] Series Entry" / "Episode 1.mp4", {job.output_file for job in jobs})
        self.assertIn(self.root / "Web" / "Shots" / "[Creator] Shot Entry" / "1.mp4", {job.output_file for job in jobs})

    def test_category_input_only_processes_that_category(self) -> None:
        series_root = self.root / "Series"
        series_entry = series_root / "[Creator] Series Entry"
        series_entry.mkdir(parents=True)
        (series_entry / "Episode 1.mkv").write_text("video", encoding="utf-8")

        category_roots, categories, default_output_root = resolve_input_roots(series_root, ["Series", "Shots", "Shorts"])
        jobs, failures = discover_jobs(series_root, categories, self.root / "Web")

        self.assertEqual(categories, ["Series"])
        self.assertEqual(category_roots, {"Series": series_root})
        self.assertEqual(default_output_root, self.root / "Web")
        self.assertEqual(len(failures), 0)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].category, "Series")
        self.assertEqual(jobs[0].output_file, self.root / "Web" / "Series" / "[Creator] Series Entry" / "Episode 1.mp4")


if __name__ == "__main__":
    unittest.main()
