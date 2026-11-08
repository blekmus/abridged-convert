from __future__ import annotations
import sys
import tempfile
from pathlib import Path
from unittest import mock
import pytest
from abridged_convert.app import main, parse_args, resolve_cli_dirs
from abridged_convert.discovery import discover_jobs, resolve_input_roots

class EntrypointBehaviorTests:

    def setup_method(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix='abridged-convert-entrypoint-')
        self.root = Path(self.temp_dir.name)

    def teardown_method(self) -> None:
        self.temp_dir.cleanup()

    def test_parse_args_accepts_required_positional_dirs(self) -> None:
        parser, args = parse_args(['.', 'Web'])
        input_dir, output_dir = resolve_cli_dirs(parser, args)
        assert input_dir == Path.cwd()
        assert output_dir == Path.cwd() / 'Web'

    def test_main_with_empty_argv_does_not_read_process_argv(self) -> None:
        # An explicit empty list means "no arguments", so the parser must reject it rather than fall back to sys.argv.
        with mock.patch.object(sys, 'argv', ['abridged-convert', str(self.root), str(self.root / 'Web'), '--dry-run']):
            with pytest.raises(SystemExit) as exc_info:
                main([])
        assert exc_info.value.code == 2

    def test_root_input_processes_all_present_categories(self) -> None:
        shots_entry = self.root / 'Shots' / '[Creator] Shot Entry'
        series_entry = self.root / 'Series' / '[Creator] Series Entry'
        shots_entry.mkdir(parents=True)
        series_entry.mkdir(parents=True)
        (shots_entry / '1.mkv').write_text('video', encoding='utf-8')
        (series_entry / 'Episode 1.mkv').write_text('video', encoding='utf-8')
        songs_entry = self.root / 'Songs' / '[Creator] Song Entry'
        songs_entry.mkdir(parents=True)
        (songs_entry / '1.mkv').write_text('video', encoding='utf-8')
        category_roots, categories, default_output_root = resolve_input_roots(self.root, ['Series', 'Shots', 'Shorts', 'Songs'])
        jobs, failures = discover_jobs(self.root, categories, self.root / 'Web')
        assert categories == ['Series', 'Shots', 'Songs']
        assert set(category_roots) == {'Series', 'Shots', 'Songs'}
        assert default_output_root == self.root / 'Web'
        assert len(failures) == 0
        assert {job.category for job in jobs} == {'Series', 'Shots', 'Songs'}
        assert self.root / 'Web' / 'Series' / '[Creator] Series Entry' / 'Episode 1.mp4' in {job.output_file for job in jobs}
        assert self.root / 'Web' / 'Shots' / '[Creator] Shot Entry' / '1.mp4' in {job.output_file for job in jobs}
        assert self.root / 'Web' / 'Songs' / '[Creator] Song Entry' / '1.mp4' in {job.output_file for job in jobs}

    def test_category_input_only_processes_that_category(self) -> None:
        series_root = self.root / 'Series'
        series_entry = series_root / '[Creator] Series Entry'
        series_entry.mkdir(parents=True)
        (series_entry / 'Episode 1.mkv').write_text('video', encoding='utf-8')
        category_roots, categories, default_output_root = resolve_input_roots(series_root, ['Series', 'Shots', 'Shorts'])
        jobs, failures = discover_jobs(series_root, categories, self.root / 'Web')
        assert categories == ['Series']
        assert category_roots == {'Series': series_root}
        assert default_output_root == self.root / 'Web'
        assert len(failures) == 0
        assert len(jobs) == 1
        assert jobs[0].category == 'Series'
        assert jobs[0].output_file == self.root / 'Web' / 'Series' / '[Creator] Series Entry' / 'Episode 1.mp4'

    def test_direct_songs_input_discovers_regular_and_amv_entries(self) -> None:
        songs_root = self.root / 'Songs'
        regular_entry = songs_root / '[Creator] Song Entry'
        amv_root = songs_root / '[~] AMVs'
        regular_entry.mkdir(parents=True)
        (amv_root / 'covers').mkdir(parents=True)
        (regular_entry / '1.mkv').write_text('video', encoding='utf-8')
        (amv_root / '[AMVCreator] Dance Song.mkv').write_text('video', encoding='utf-8')
        (amv_root / 'covers' / 'Dance Song AMV.png').write_text('cover', encoding='utf-8')
        category_roots, categories, default_output_root = resolve_input_roots(songs_root, ['Series', 'Shots', 'Shorts', 'Songs'])
        jobs, failures = discover_jobs(songs_root, categories, self.root / 'Web')
        assert categories == ['Songs']
        assert category_roots == {'Songs': songs_root}
        assert default_output_root == self.root / 'Web'
        assert len(failures) == 0
        assert {job.output_file for job in jobs} == {self.root / 'Web' / 'Songs' / '[Creator] Song Entry' / '1.mp4', self.root / 'Web' / 'Songs' / 'AMV' / '[AMVCreator] Dance Song' / '1.mp4'}
        amv_job = next((job for job in jobs if job.source_video.name == '[AMVCreator] Dance Song.mkv'))
        assert amv_job.entry_dir.name == '[AMVCreator] Dance Song'
        assert amv_job.cover_search_dir == amv_root / 'covers'
        assert amv_job.cover_match_source == amv_root / 'Dance Song.mkv'
