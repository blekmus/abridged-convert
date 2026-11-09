from __future__ import annotations
import sys
import tempfile
from pathlib import Path
from unittest import mock
import pytest
from abridged_convert.app import main, parse_args
from abridged_convert.common import path_in_scope
import json
from abridged_convert.models import ConversionError, ConversionResult
from abridged_convert.discovery import discover_jobs, resolve_input_roots

class EntrypointBehaviorTests:

    def setup_method(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix='abridged-convert-entrypoint-')
        self.root = Path(self.temp_dir.name)

    def teardown_method(self) -> None:
        self.temp_dir.cleanup()

    def test_parse_args_accepts_required_positional_dirs(self) -> None:
        options = parse_args(['convert', '.', 'Web'])
        assert options.archive == Path.cwd()
        assert options.output == Path.cwd() / 'Web'

    def test_empty_argv_prints_help_and_ignores_process_argv(self, capsys) -> None:
        # An explicit empty list means "no arguments", so main must not fall back to sys.argv.
        with mock.patch.object(sys, 'argv', ['abridged-convert', 'convert', str(self.root), str(self.root / 'Web'), '--dry-run']):
            with pytest.raises(SystemExit) as exc_info:
                main([])
        assert exc_info.value.code == 2
        assert 'examples:' in capsys.readouterr().err

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

    def test_category_folder_is_not_accepted_as_archive(self) -> None:
        series_entry = self.root / 'Series' / '[Creator] Series Entry'
        series_entry.mkdir(parents=True)
        (series_entry / 'Episode 1.mkv').write_text('video', encoding='utf-8')
        with pytest.raises(ConversionError, match='ARCHIVE must be the archive root'):
            resolve_input_roots(self.root / 'Series', ['Series'])

    def test_songs_input_discovers_regular_and_amv_entries(self) -> None:
        songs_root = self.root / 'Songs'
        regular_entry = songs_root / '[Creator] Song Entry'
        amv_root = songs_root / '[~] AMVs'
        regular_entry.mkdir(parents=True)
        (amv_root / 'covers').mkdir(parents=True)
        (regular_entry / '1.mkv').write_text('video', encoding='utf-8')
        (amv_root / '[AMVCreator] Dance Song.mkv').write_text('video', encoding='utf-8')
        (amv_root / 'covers' / 'Dance Song AMV.png').write_text('cover', encoding='utf-8')
        category_roots, categories, default_output_root = resolve_input_roots(self.root, ['Series', 'Shots', 'Shorts', 'Songs'])
        jobs, failures = discover_jobs(self.root, categories, self.root / 'Web')
        assert categories == ['Songs']
        assert category_roots == {'Songs': songs_root}
        assert default_output_root == self.root / 'Web'
        assert len(failures) == 0
        assert {job.output_file for job in jobs} == {self.root / 'Web' / 'Songs' / '[Creator] Song Entry' / '1.mp4', self.root / 'Web' / 'Songs' / 'AMV' / '[AMVCreator] Dance Song' / '1.mp4'}
        amv_job = next((job for job in jobs if job.source_video.name == '[AMVCreator] Dance Song.mkv'))
        assert amv_job.entry_dir.name == '[AMVCreator] Dance Song'
        assert amv_job.cover_search_dir == amv_root / 'covers'
        assert amv_job.cover_match_source == amv_root / 'Dance Song.mkv'


class CliSurfaceTests:

    def setup_method(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix='abridged-convert-cli-')
        self.root = Path(self.temp_dir.name)
        self.archive = self.root / 'abridged'
        for name in ('[A] Show', '[A] Show 2'):
            (self.archive / 'Series' / name).mkdir(parents=True)
            (self.archive / 'Series' / name / 'Episode 1.mkv').write_text('video', encoding='utf-8')
        (self.archive / 'Songs' / '[~] AMVs').mkdir(parents=True)
        (self.archive / 'Songs' / '[~] AMVs' / '[B] Song.mkv').write_text('video', encoding='utf-8')

    def teardown_method(self) -> None:
        self.temp_dir.cleanup()

    def _converted(self, *extra: str) -> tuple[int, list[str]]:
        converted: list[str] = []

        def fake_convert(job, **kwargs):
            converted.append(job.source_video.relative_to(self.archive).as_posix())
            return ConversionResult(category=job.category, entry_dir=job.entry_dir, source_video=job.source_video, output_file=job.output_file, success=True, skipped=True, message='fake')

        with mock.patch('abridged_convert.app.validate_tools'), \
             mock.patch('abridged_convert.app.resolve_video_handling_plan', side_effect=lambda *a, **k: ('.mp4', None)), \
             mock.patch('abridged_convert.app.convert_job', side_effect=fake_convert), \
             mock.patch('abridged_convert.app.update_metadata_cache_item_with_tracking', side_effect=lambda *a, **k: mock.Mock(reused=True, probed=False, failed=False, output_path=self.root / 'Web' / '.abridged-video-metadata.json')):
            code = main(['convert', str(self.archive), str(self.root / 'Web'), *extra])
        return code, converted

    def test_deploy_requires_remote(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(['deploy', str(self.archive)])
        assert exc_info.value.code == 2

    def test_deploy_help_shows_archive_remote_only(self, capsys) -> None:
        with pytest.raises(SystemExit):
            parse_args(['deploy', '--help'])
        out = capsys.readouterr().out
        assert 'ARCHIVE REMOTE' in out
        assert 'STAGING' not in out and '--keep-staging' not in out

    def test_category_is_case_insensitive(self) -> None:
        assert parse_args(['convert', 'a', 'b', '--category', 'series', '--category', 'SONGS']).categories == ('Series', 'Songs')

    def test_unknown_category_lists_choices(self, capsys) -> None:
        with pytest.raises(SystemExit) as exc_info:
            parse_args(['convert', 'a', 'b', '--category', 'Seris'])
        assert exc_info.value.code == 2
        assert "choose from 'Series', 'Shots', 'Shorts', 'Songs'" in capsys.readouterr().err

    def test_category_and_entry_are_exclusive(self) -> None:
        with pytest.raises(SystemExit):
            parse_args(['convert', 'a', 'b', '--category', 'Series', '--entry', 'x'])

    def test_convert_help_shows_only_convert_options(self, capsys) -> None:
        with pytest.raises(SystemExit):
            parse_args(['convert', '--help'])
        out = capsys.readouterr().out
        assert '--keep-staging' not in out and '--upload' not in out and '--rclone' not in out
        assert '{Series,Shots,Shorts,Songs}' in out

    def test_category_folder_archive_suggests_fix(self, capsys) -> None:
        assert main(['convert', str(self.archive / 'Series'), str(self.root / 'Web')]) == 2
        err = capsys.readouterr().err
        assert f'Try: abridged-convert convert {self.archive.as_posix()} {(self.root / "Web").as_posix()} --category Series' in err

    def test_category_folder_output_suggests_fix(self, capsys) -> None:
        assert main(['convert', str(self.archive), str(self.root / 'Web' / 'Series'), '--dry-run']) == 2
        err = capsys.readouterr().err
        assert 'OUTPUT must be the web root' in err
        assert f'{(self.root / "Web").as_posix()} --dry-run --category Series' in err

    def test_entry_dir_converts_only_that_entry(self) -> None:
        code, converted = self._converted('--entry', str(self.archive / 'Series' / '[A] Show'))
        assert code == 0
        assert converted == ['Series/[A] Show/Episode 1.mkv']

    def test_entry_amv_file_converts_only_that_amv(self) -> None:
        code, converted = self._converted('--entry', str(self.archive / 'Songs' / '[~] AMVs' / '[B] Song.mkv'))
        assert code == 0
        assert converted == ['Songs/[~] AMVs/[B] Song.mkv']

    def test_entry_must_be_an_entry(self, capsys) -> None:
        code, converted = self._converted('--entry', str(self.archive / 'Series' / '[A] Show' / 'Episode 1.mkv'))
        assert code == 2
        assert converted == []
        assert 'not a convertible entry' in capsys.readouterr().err

    def test_entry_convert_does_not_prune_siblings(self) -> None:
        web = self.root / 'Web'
        sibling = web / 'Series' / '[A] Show 2'
        sibling.mkdir(parents=True)
        (sibling / 'Episode 1.mp4').write_text('out', encoding='utf-8')
        (web / '.abridged-video-metadata.json').write_text(json.dumps({'version': 2, 'items': {'Series/[A] Show 2/Episode 1.mp4': {'size': 3}, 'Series/[A] Show/Old.mp4': {'size': 3}}}), encoding='utf-8')
        code, _ = self._converted('--entry', str(self.archive / 'Series' / '[A] Show'))
        assert code == 0
        items = json.loads((web / '.abridged-video-metadata.json').read_text(encoding='utf-8'))['items']
        assert 'Series/[A] Show 2/Episode 1.mp4' in items
        assert 'Series/[A] Show/Old.mp4' not in items
        assert (sibling / 'Episode 1.mp4').exists()

    def test_path_in_scope_matches_whole_segments(self) -> None:
        assert path_in_scope('Series/[A] Show/1.mp4', ('Series/[A] Show',))
        assert not path_in_scope('Series/[A] Show 2/1.mp4', ('Series/[A] Show',))
        assert path_in_scope('Series/x', ('Series',))
        assert not path_in_scope('SeriesX/x', ('Series',))


class HelpColorTests:

    def test_help_is_plain_when_not_a_terminal(self, capsys, monkeypatch) -> None:
        monkeypatch.delenv('FORCE_COLOR', raising=False)
        with pytest.raises(SystemExit):
            parse_args(['deploy', '--help'])
        assert '\x1b[' not in capsys.readouterr().out

    def test_force_color_colors_headings_flags_and_metavars(self, capsys, monkeypatch) -> None:
        monkeypatch.setenv('FORCE_COLOR', '1')
        monkeypatch.delenv('NO_COLOR', raising=False)
        with pytest.raises(SystemExit):
            parse_args(['deploy', '--help'])
        out = capsys.readouterr().out
        assert '\x1b[1m\x1b[33musage:\x1b[0m' in out
        assert '\x1b[36m--dry-run\x1b[0m' in out
        assert '\x1b[32mREMOTE\x1b[0m' in out
        assert '\x1b[1m\x1b[36mdeploy\x1b[0m' in out

    def test_no_color_wins_over_force_color(self, capsys, monkeypatch) -> None:
        monkeypatch.setenv('FORCE_COLOR', '1')
        monkeypatch.setenv('NO_COLOR', '1')
        with pytest.raises(SystemExit):
            parse_args(['convert', 'a', 'b', '--category', 'nope'])
        assert '\x1b[' not in capsys.readouterr().err

    def test_colorize_leaves_hyphenated_words_alone(self) -> None:
        from abridged_convert.help_format import colorize
        assert colorize('  Only process this category (case-insensitive).') == '  Only process this category (case-insensitive).'
