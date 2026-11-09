from __future__ import annotations
import io
import json
import shutil
import subprocess
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
import abridged_convert.conversion as conversion
from abridged_convert.app import main
from abridged_convert.conversion import tracking_file_for_job
from abridged_convert.discovery import compact_label, discover_jobs
from abridged_convert.media import build_ffmpeg_command, extract_embedded_cover, resolve_remux_output_suffix, write_cover_sidecar
from abridged_convert.models import VideoEncodingConfig
from abridged_convert.ui import TerminalUI
FFMPEG = shutil.which('ffmpeg') or 'ffmpeg'
FFPROBE = shutil.which('ffprobe') or 'ffprobe'

def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, capture_output=True, text=True)

def ffprobe_json(path: Path) -> dict:
    completed = run([FFPROBE, '-v', 'error', '-show_entries', 'format=format_name:format_tags:stream=index,codec_type,codec_name,disposition:stream_tags', '-of', 'json', str(path)])
    return json.loads(completed.stdout)

def normalize_probe_tags(probe: dict) -> dict[str, str]:
    return {key.lower(): value for key, value in probe.get('format', {}).get('tags', {}).items()}

class AbridgedConvertTests:

    @classmethod
    def setup_class(cls) -> None:
        for tool in (FFMPEG, FFPROBE):
            subprocess.run([tool, '-version'], check=True, capture_output=True, text=True)

    def setup_method(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix='abridged-convert-test-')
        self.repo_root = Path(self.temp_dir.name)
        for category in ('Shots', 'Shorts', 'Series', 'Songs'):
            (self.repo_root / category).mkdir(parents=True)

    def teardown_method(self) -> None:
        self.temp_dir.cleanup()

    def test_default_run_processes_all_roots(self) -> None:
        shots_entry = self.repo_root / 'Shots' / '[Creator] Shot Entry'
        shots_entry.mkdir()
        self._make_cover_image(shots_entry / 'cover.png', 'red')
        self._make_source_video(shots_entry / '1.mkv', title='Shot Title', description='Shot Description', comment='Shot Comment')
        shorts_entry = self.repo_root / 'Shorts' / '[Creator] Short Entry'
        shorts_entry.mkdir()
        self._make_cover_image(shorts_entry / 'cover.jpg', 'yellow')
        self._make_source_video(shorts_entry / '1.webm', title='Short Title', description='Short Description', comment='Short Comment')
        series_entry = self.repo_root / 'Series' / '[Creator] Series Entry'
        (series_entry / 'covers').mkdir(parents=True)
        self._make_cover_image(series_entry / 'covers' / '1.png', 'green')
        self._make_source_video(series_entry / 'Episode 1 - Pilot.mkv', title='Episode 1 - Pilot', description='Series Description', comment='Series Comment')
        exit_code = main(['convert', str(self.repo_root), str(self.repo_root / 'Web')])
        assert exit_code == 0
        assert (self.repo_root / 'Web' / 'Shots' / '[Creator] Shot Entry' / '1.mp4').exists()
        assert (self.repo_root / 'Web' / 'Shots' / '[Creator] Shot Entry' / '1.webp').exists()
        assert (self.repo_root / 'Web' / 'Shorts' / '[Creator] Short Entry' / '1.webm').exists()
        assert (self.repo_root / 'Web' / 'Shorts' / '[Creator] Short Entry' / '1.webp').exists()
        assert (self.repo_root / 'Web' / 'Series' / '[Creator] Series Entry' / 'Episode 1 - Pilot.mp4').exists()
        assert (self.repo_root / 'Web' / 'Series' / '[Creator] Series Entry' / 'Episode 1 - Pilot.webp').exists()
        metadata_cache = json.loads((self.repo_root / 'Web' / '.abridged-video-metadata.json').read_text(encoding='utf-8'))
        assert metadata_cache['version'] == 2
        assert set(metadata_cache['items']) == {'Series/[Creator] Series Entry/Episode 1 - Pilot.mp4', 'Shots/[Creator] Shot Entry/1.mp4', 'Shorts/[Creator] Short Entry/1.webm'}
        shot_item = metadata_cache['items']['Shots/[Creator] Shot Entry/1.mp4']
        assert shot_item['description'] == 'Shot Description'
        assert shot_item['date'] == '20240202'
        assert shot_item['durationSeconds'] == 1
        assert shot_item['source']['path'] == 'Shots/[Creator] Shot Entry/1.mkv'
        assert shot_item['outputPath'] == 'Shots/[Creator] Shot Entry/1.mp4'
        assert shot_item['coverPath'] == 'Shots/[Creator] Shot Entry/1.webp'

    def test_external_cover_wins_over_embedded_cover(self) -> None:
        entry = self.repo_root / 'Shots' / '[Creator] Embedded First'
        entry.mkdir()
        external_cover = entry / 'cover.webp'
        embedded_cover = self.repo_root / 'embedded-cover.png'
        self._make_cover_image(external_cover, 'red')
        self._make_cover_image(embedded_cover, 'blue')
        self._make_source_video(entry / '1.mkv', title='Embedded Cover', description='desc', comment='comment', add_cover_stream=embedded_cover)
        exit_code = main(['convert', str(self.repo_root), str(self.repo_root / 'Web'), '--category', 'Shots'])
        assert exit_code == 0
        output = self.repo_root / 'Web' / 'Shots' / '[Creator] Embedded First' / '1.mp4'
        cover_output = self.repo_root / 'Web' / 'Shots' / '[Creator] Embedded First' / '1.webp'
        probe = ffprobe_json(output)
        assert len(probe['streams']) == 2
        assert cover_output.exists()
        assert ffprobe_json(cover_output)['streams'][0]['codec_name'] == 'webp'
        assert cover_output.read_bytes() == external_cover.read_bytes()

    def test_extract_embedded_webp_cover_preserves_original_bytes(self) -> None:
        embedded_cover = self.repo_root / 'embedded-cover.webp'
        self._make_cover_image(embedded_cover, 'blue')
        temp_dir = self.repo_root / 'extract-temp'
        temp_dir.mkdir()
        extracted_cover = extract_embedded_cover(FFMPEG, embedded_cover, 0, 'webp', temp_dir)
        assert extracted_cover.suffix.lower() == '.webp'
        assert extracted_cover.read_bytes() == embedded_cover.read_bytes()

    def test_shot_output_title_and_artist_are_derived_from_entry_name(self) -> None:
        entry = self.repo_root / 'Shots' / '[MyToasterIsMoist] Interspecies Reviewers Abridged'
        entry.mkdir()
        self._make_source_video(entry / '1.mkv', title='Wrong Source Title', artist='Wrong Source Artist', description='desc', comment='comment')
        exit_code = main(['convert', str(self.repo_root), str(self.repo_root / 'Web'), '--category', 'Shots'])
        assert exit_code == 0
        output = self.repo_root / 'Web' / 'Shots' / '[MyToasterIsMoist] Interspecies Reviewers Abridged' / '1.mp4'
        tags = normalize_probe_tags(ffprobe_json(output))
        assert tags['title'] == 'Interspecies Reviewers Abridged'
        assert tags['artist'] == 'MyToasterIsMoist'

    def test_song_uses_single_item_layout_and_external_cover(self) -> None:
        entry = self.repo_root / 'Songs' / "[Nantoka Narusou] Gendo's Paradise"
        entry.mkdir()
        external_cover = entry / 'cover.png'
        self._make_cover_image(external_cover, 'red')
        self._make_source_video(entry / '1.mkv', title='Wrong Song Title', artist='Wrong Song Artist', description='desc', comment='comment')
        exit_code = main(['convert', str(self.repo_root), str(self.repo_root / 'Web'), '--category', 'Songs'])
        assert exit_code == 0
        output = self.repo_root / 'Web' / 'Songs' / "[Nantoka Narusou] Gendo's Paradise" / '1.mp4'
        cover_output = self.repo_root / 'Web' / 'Songs' / "[Nantoka Narusou] Gendo's Paradise" / '1.webp'
        tags = normalize_probe_tags(ffprobe_json(output))
        assert tags['title'] == "Gendo's Paradise"
        assert tags['artist'] == 'Nantoka Narusou'
        assert cover_output.exists()
        assert ffprobe_json(cover_output)['streams'][0]['codec_name'] == 'webp'

    def test_amv_uses_special_output_layout_matching_cover_and_entry_metadata(self) -> None:
        amv_root = self.repo_root / 'Songs' / '[~] AMVs'
        covers_dir = amv_root / 'covers'
        covers_dir.mkdir(parents=True)
        matched_cover = covers_dir / 'Uptown Funk [ AMV - Mix ] Anime Mix [ej7E89xq1v8].png'
        self._make_cover_image(matched_cover, 'green')
        self._make_cover_image(covers_dir / 'Burn The House Down [ AMV - Mix ] Anime Mix [94hviaWK8UE].png', 'red')
        self._make_source_video(amv_root / '[monomogu] Uptown Funk.mkv', title='Wrong AMV Title', artist='Wrong AMV Artist', description='desc', comment='comment')
        exit_code = main(['convert', str(self.repo_root), str(self.repo_root / 'Web'), '--category', 'Songs'])
        assert exit_code == 0
        output = self.repo_root / 'Web' / 'Songs' / 'AMV' / '[monomogu] Uptown Funk' / '1.mp4'
        cover_output = self.repo_root / 'Web' / 'Songs' / 'AMV' / '[monomogu] Uptown Funk' / '1.webp'
        tags = normalize_probe_tags(ffprobe_json(output))
        assert tags['title'] == 'Uptown Funk'
        assert tags['artist'] == 'monomogu'
        assert cover_output.exists()
        assert ffprobe_json(cover_output)['streams'][0]['codec_name'] == 'webp'

    def test_amv_cover_matching_handles_stylized_spaced_title(self) -> None:
        amv_root = self.repo_root / 'Songs' / '[~] AMVs'
        covers_dir = amv_root / 'covers'
        covers_dir.mkdir(parents=True)
        matched_cover = covers_dir / 'Gintama AMV - ＤＲＯＰＰＥＤ [JiSIVsKy-N8].png'
        self._make_cover_image(matched_cover, 'blue')
        self._make_source_video(amv_root / '[YushiieChii] D R O P P E D.mkv', title='Wrong AMV Title', artist='Wrong AMV Artist', description='desc', comment='comment')
        exit_code = main(['convert', str(self.repo_root), str(self.repo_root / 'Web'), '--category', 'Songs'])
        assert exit_code == 0
        cover_output = self.repo_root / 'Web' / 'Songs' / 'AMV' / '[YushiieChii] D R O P P E D' / '1.webp'
        assert cover_output.exists()
        assert ffprobe_json(cover_output)['streams'][0]['codec_name'] == 'webp'

    def test_series_output_title_and_artist_are_derived_from_entry_and_episode_names(self) -> None:
        entry = self.repo_root / 'Series' / '[2GS BootLegged] Fire Hoez'
        entry.mkdir()
        self._make_source_video(entry / 'Episode 1 - something.mkv', title='Wrong Episode Title', artist='Wrong Source Artist', description='desc', comment='comment')
        exit_code = main(['convert', str(self.repo_root), str(self.repo_root / 'Web'), '--category', 'Series'])
        assert exit_code == 0
        output = self.repo_root / 'Web' / 'Series' / '[2GS BootLegged] Fire Hoez' / 'Episode 1 - something.mp4'
        tags = normalize_probe_tags(ffprobe_json(output))
        assert tags['title'] == 'Fire Hoez - Episode 1 - something'
        assert tags['artist'] == '2GS BootLegged'

    def test_series_uses_best_matching_external_cover_when_embedded_missing(self) -> None:
        entry = self.repo_root / 'Series' / '[Creator] Matching Covers'
        (entry / 'covers').mkdir(parents=True)
        self._make_cover_image(entry / 'covers' / '1.png', 'green')
        self._make_cover_image(entry / 'covers' / '2.png', 'red')
        self._make_source_video(entry / 'Episode 1 - Pilot.mkv', title='Episode 1 - Pilot', description='desc', comment='comment')
        exit_code = main(['convert', str(self.repo_root), str(self.repo_root / 'Web'), '--category', 'Series'])
        assert exit_code == 0
        output = self.repo_root / 'Web' / 'Series' / '[Creator] Matching Covers' / 'Episode 1 - Pilot.mp4'
        cover_output = self.repo_root / 'Web' / 'Series' / '[Creator] Matching Covers' / 'Episode 1 - Pilot.webp'
        probe = ffprobe_json(output)
        assert len(probe['streams']) == 2
        assert cover_output.exists()
        assert ffprobe_json(cover_output)['streams'][0]['codec_name'] == 'webp'

    def test_series_generates_cover_when_no_external_or_embedded_cover_exists(self) -> None:
        entry = self.repo_root / 'Series' / '[Creator] No Matching Covers'
        (entry / 'covers').mkdir(parents=True)
        self._make_cover_image(entry / 'covers' / 'art-a.png', 'green')
        self._make_cover_image(entry / 'covers' / 'bonus-image.png', 'red')
        self._make_source_video(entry / 'Episode 1 - Pilot.mkv', title='Pilot', description='desc', comment='comment', duration=21)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = main(['convert', str(self.repo_root), str(self.repo_root / 'Web'), '--category', 'Series'])
        assert exit_code == 0
        output = self.repo_root / 'Web' / 'Series' / '[Creator] No Matching Covers' / 'Episode 1 - Pilot.mp4'
        cover_output = self.repo_root / 'Web' / 'Series' / '[Creator] No Matching Covers' / 'Episode 1 - Pilot.webp'
        probe = ffprobe_json(output)
        assert len(probe['streams']) == 2
        assert cover_output.exists()
        assert ffprobe_json(cover_output)['streams'][0]['codec_name'] == 'webp'
        assert 'cover=generated:20s' in stdout.getvalue()

    def test_series_prefers_exact_titled_cover_over_numeric_episode_art(self) -> None:
        entry = self.repo_root / 'Series' / '[Creator] Mixed Cover Signals'
        covers_dir = entry / 'covers'
        covers_dir.mkdir(parents=True)
        titled_cover = covers_dir / 'This is a College Class on Rap.webp'
        numeric_cover = covers_dir / '0.5.png'
        self._make_cover_image(titled_cover, 'green')
        self._make_cover_image(numeric_cover, 'red')
        self._make_source_video(entry / 'Episode 0.5 - This is a College Class on Rap.mkv', title='Episode 0.5 - This is a College Class on Rap', description='desc', comment='comment')
        exit_code = main(['convert', str(self.repo_root), str(self.repo_root / 'Web'), '--category', 'Series'])
        assert exit_code == 0
        cover_output = self.repo_root / 'Web' / 'Series' / '[Creator] Mixed Cover Signals' / 'Episode 0.5 - This is a College Class on Rap.webp'
        assert cover_output.exists()
        assert cover_output.read_bytes() == titled_cover.read_bytes()

    def test_series_uses_embedded_cover_when_hybrid_filesystem_match_returns_none(self) -> None:
        entry = self.repo_root / 'Series' / '[Creator] Embedded Series Cover'
        covers_dir = entry / 'covers'
        covers_dir.mkdir(parents=True)
        self._make_cover_image(covers_dir / '0.1.png', 'green')
        self._make_cover_image(covers_dir / 'bonus.png', 'yellow')
        embedded_cover = self.repo_root / 'embedded-series-cover.png'
        self._make_cover_image(embedded_cover, 'blue')
        self._make_source_video(entry / 'Episode 14 - Kurosai Cinematic Universe.mkv', title='Episode 14 - Kurosai Cinematic Universe', description='desc', comment='comment', add_cover_stream=embedded_cover)
        exit_code = main(['convert', str(self.repo_root), str(self.repo_root / 'Web'), '--category', 'Series'])
        assert exit_code == 0
        output = self.repo_root / 'Web' / 'Series' / '[Creator] Embedded Series Cover' / 'Episode 14 - Kurosai Cinematic Universe.mp4'
        cover_output = self.repo_root / 'Web' / 'Series' / '[Creator] Embedded Series Cover' / 'Episode 14 - Kurosai Cinematic Universe.webp'
        probe = ffprobe_json(output)
        assert len(probe['streams']) == 2
        assert cover_output.exists()
        expected_cover_output = self.repo_root / 'expected-embedded-series-cover.webp'
        write_cover_sidecar(FFMPEG, embedded_cover, expected_cover_output)
        assert cover_output.read_bytes() == expected_cover_output.read_bytes()

    def test_builds_nvenc_command_when_requested(self) -> None:
        metadata = type('Metadata', (), {'video_index': 0, 'audio_index': 1, 'video_language': None, 'audio_language': None, 'tags': {}})()
        cmd = build_ffmpeg_command('ffmpeg', Path('input.mkv'), Path('output.mp4'), metadata, None, VideoEncodingConfig(mode='nvidia', codec='h264_nvenc', extra_args=('-preset', 'p5', '-cq', '23')))
        assert 'h264_nvenc' in cmd

    def test_builds_stream_copy_command_when_requested(self) -> None:
        metadata = type('Metadata', (), {'video_index': 0, 'audio_index': 1, 'video_language': None, 'audio_language': None, 'tags': {}})()
        cmd = build_ffmpeg_command('ffmpeg', Path('input.mkv'), Path('output.webm'), metadata, Path('cover.webp'), VideoEncodingConfig(mode='copy', codec='copy', extra_args=()))
        assert '-c:v:0' in cmd
        assert 'copy' in cmd
        assert '-c:a:0' in cmd
        assert 'cover.webp' not in cmd
        assert 'attached_pic' not in cmd
        assert 'libx264' not in cmd
        assert 'h264_nvenc' not in cmd
        assert '-pix_fmt' not in cmd
        assert '-b:a:0' not in cmd
        assert '-movflags' not in cmd

    def test_default_keeps_webm_container_and_writes_cover_sidecar(self) -> None:
        entry = self.repo_root / 'Shorts' / '[Creator] Webm Source'
        entry.mkdir()
        external_cover = entry / 'cover.png'
        self._make_cover_image(external_cover, 'red')
        self._make_source_video(entry / '1.webm', title='Wrong Source Title', artist='Wrong Source Artist', description='desc', comment='comment')
        exit_code = main(['convert', str(self.repo_root), str(self.repo_root / 'Web'), '--category', 'Shorts'])
        assert exit_code == 0
        output = self.repo_root / 'Web' / 'Shorts' / '[Creator] Webm Source' / '1.webm'
        cover_output = self.repo_root / 'Web' / 'Shorts' / '[Creator] Webm Source' / '1.webp'
        assert output.exists()
        assert not (output.parent / '1.mp4').exists()
        assert cover_output.exists()
        probe = ffprobe_json(output)
        assert len(probe['streams']) == 2

    def test_default_uses_webm_for_webm_compatible_mkv(self) -> None:
        entry = self.repo_root / 'Shorts' / '[Creator] Webm Compatible MKV'
        entry.mkdir()
        self._make_source_video(entry / '1.mkv', title='Wrong Source Title', artist='Wrong Source Artist', description='desc', comment='comment', audio_codec='libopus')
        exit_code = main(['convert', str(self.repo_root), str(self.repo_root / 'Web'), '--category', 'Shorts'])
        assert exit_code == 0
        output = self.repo_root / 'Web' / 'Shorts' / '[Creator] Webm Compatible MKV' / '1.webm'
        assert output.exists()
        assert not (output.parent / '1.mkv').exists()
        assert not (output.parent / '1.mp4').exists()

    def test_default_uses_mp4_for_mp4_compatible_mkv(self) -> None:
        entry = self.repo_root / 'Shorts' / '[Creator] Mp4 Compatible MKV'
        entry.mkdir()
        self._make_source_video(entry / '1.mkv', title='Wrong Source Title', artist='Wrong Source Artist', description='desc', comment='comment')
        assert resolve_remux_output_suffix(FFPROBE, entry / '1.mkv') == '.mp4'

    def test_default_encodes_when_source_streams_do_not_fit_supported_containers(self) -> None:
        entry = self.repo_root / 'Shorts' / '[Creator] Needs Encoding'
        entry.mkdir()
        self._make_source_video(entry / '1.mkv', title='Wrong Source Title', artist='Wrong Source Artist', description='desc', comment='comment', video_codec='ffv1', audio_codec='flac')
        assert resolve_remux_output_suffix(FFPROBE, entry / '1.mkv') is None
        exit_code = main(['convert', str(self.repo_root), str(self.repo_root / 'Web'), '--category', 'Shorts'])
        assert exit_code == 0
        output = self.repo_root / 'Web' / 'Shorts' / '[Creator] Needs Encoding' / '1.mp4'
        assert output.exists()
        probe = ffprobe_json(output)
        codecs = {stream['codec_type']: stream['codec_name'] for stream in probe['streams']}
        assert codecs['video'] == 'h264'
        assert codecs['audio'] == 'aac'

    def test_compact_label_uses_title_creator_and_episode(self) -> None:
        job = type('JobLike', (), {'category': 'Series', 'entry_dir': Path('[Creator] Great Show'), 'source_video': Path('Episode 2 - Return.mkv'), 'output_file': Path('ignored')})()
        assert compact_label(job) == 'Series  [Creator] Great Show  Episode 2 - Return'

    def test_tui_queue_uses_status_symbols_without_pending_rows(self) -> None:
        ui = TerminalUI(repo_root=self.repo_root, categories=['Shots'], jobs=[], discovery_failures=[], dry_run=False, stream=self._fake_stream(isatty=True))
        ui.states = [type('State', (), {'status': 'pending', 'label': 'one', 'detail': 'queued', 'message': ''})(), type('State', (), {'status': 'running', 'label': 'two', 'detail': 'working', 'message': ''})(), type('State', (), {'status': 'done', 'label': 'three', 'detail': 'done', 'message': 'embedded'})(), type('State', (), {'status': 'failed', 'label': 'four', 'detail': 'boom', 'message': ''})()]
        lines = ui._queue_lines()
        assert any(('▶ working' in line for line in lines))
        assert any(('✓ done' in line for line in lines))
        assert any(('✗ failed' in line for line in lines))
        assert not any(('pending' in line for line in lines))

    def test_tui_highlights_creator_brackets_in_purple(self) -> None:
        ui = TerminalUI(repo_root=self.repo_root, categories=['Shots'], jobs=[], discovery_failures=[], dry_run=False, stream=self._fake_stream(isatty=True))
        line = ui._format_state_line(type('State', (), {'status': 'done', 'label': 'Shots  [Creator] Great Show', 'detail': 'done', 'message': ''})())
        assert f'{ui.ANSI_BRIGHT_MAGENTA}[Creator]{ui.ANSI_RESET}' in line

    def test_tui_finish_prints_completed_and_failed_rows_without_skips(self) -> None:

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
        ui = TerminalUI(repo_root=self.repo_root, categories=['Series'], jobs=[], discovery_failures=[], dry_run=False, stream=stream)
        ui.states = [type('State', (), {'status': 'done', 'label': 'Series  [Creator] Show  Episode 1', 'detail': 'done', 'message': 'embedded'})(), type('State', (), {'status': 'skipped', 'label': 'Series  [Creator] Show  Episode 2', 'detail': 'already exists', 'message': ''})(), type('State', (), {'status': 'failed', 'label': 'Series  [Creator] Show  Episode 3', 'detail': 'bad match', 'message': ''})()]
        ui.enabled = True
        ui._active = True
        ui.finish(1)
        output = ''.join(stream.parts).split(ui.ANSI_EXIT_ALT_SCREEN, 1)[1]
        assert 'Processed' in output
        assert '✓ done' in output
        assert 'Show  Episode 1' in output
        assert '✗ failed' in output
        assert 'Show  Episode 3' in output
        assert '↷ skipped' not in output
        assert 'Show  Episode 2' not in output

    def test_tui_dry_run_prints_one_coloured_report_with_deleted_files(self) -> None:

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
        ui = TerminalUI(repo_root=self.repo_root, categories=['Series'], jobs=[], discovery_failures=[], dry_run=True, stream=stream)
        ui.states = [type('State', (), {'status': 'done', 'label': 'Series  [Creator] Show  Episode 1', 'detail': 'deploy dry run (not deployed)', 'message': 'deploy dry run (not deployed)'})()]
        ui.enabled = True
        ui._active = True
        ui.add_deleted_items([('Series/[Gone] Show/1.mp4', 'removed entry'), ('Series/[Stray] Show/1.mp4', 'orphan'), ('Series/[Creator] Show/Episode 1.1.es.vtt', 'stale subtitle')])
        ui.finish(0)
        output = ''.join(stream.parts).split(ui.ANSI_EXIT_ALT_SCREEN, 1)[1]
        # One list only: no plain duplicate of the rows, so each message appears once.
        assert 'Dry-run queue summary' not in output
        assert '[OK]' not in output
        assert output.count('deploy dry run (not deployed)') == 1
        # Styled like the done rows: red badge, white path, dim note.
        assert f'{ui.ANSI_RED}✓ removing{ui.ANSI_RESET} {ui.ANSI_WHITE}Series/[Gone] Show/1.mp4{ui.ANSI_RESET} {ui.ANSI_DIM}deploy dry run (removed entry){ui.ANSI_RESET}' in output
        assert f'{ui.ANSI_WHITE}Series/[Stray] Show/1.mp4{ui.ANSI_RESET} {ui.ANSI_DIM}deploy dry run (orphan){ui.ANSI_RESET}' in output
        assert 'deploy dry run (stale subtitle)' in output
        assert 'orphan = not in the catalog' in output

    def test_existing_tracking_file_deletes_partial_output_and_restarts(self) -> None:
        entry = self.repo_root / 'Shots' / '[Creator] Resume Me'
        entry.mkdir()
        self._make_source_video(entry / '1.mkv', title='Resume Me', description='desc', comment='comment')
        jobs, _ = discover_jobs(self.repo_root, ['Shots'])
        job = jobs[0]
        job.output_file.parent.mkdir(parents=True, exist_ok=True)
        job.output_file.write_text('partial output', encoding='utf-8')
        tracking_file = tracking_file_for_job(job)
        assert tracking_file.parent == job.output_file.parent
        assert tracking_file.parent != job.entry_dir
        tracking_file.write_text(f'{job.output_file}\n', encoding='utf-8')
        exit_code = main(['convert', str(self.repo_root), str(self.repo_root / 'Web'), '--category', 'Shots'])
        assert exit_code == 0
        assert not tracking_file.exists()
        probe = ffprobe_json(job.output_file)
        assert probe['format']['format_name'] == 'mov,mp4,m4a,3gp,3g2,mj2'

    def test_keyboard_interrupt_keeps_tracking_file_and_next_run_resumes(self) -> None:
        entry = self.repo_root / 'Shots' / '[Creator] Interrupted'
        entry.mkdir()
        self._make_source_video(entry / '1.mkv', title='Interrupted', description='desc', comment='comment')
        jobs, _ = discover_jobs(self.repo_root, ['Shots'])
        job = jobs[0]
        tracking_file = tracking_file_for_job(job)
        original_run_command = conversion.run_command

        def interrupting_run_command(cmd: list[str], *, check: bool=True) -> subprocess.CompletedProcess[str]:
            if cmd[0] == FFMPEG and cmd[-1] == str(job.output_file):
                job.output_file.parent.mkdir(parents=True, exist_ok=True)
                job.output_file.write_text('partial output', encoding='utf-8')
                raise KeyboardInterrupt
            return original_run_command(cmd, check=check)
        interrupted_stdout = io.StringIO()
        interrupted_stderr = io.StringIO()
        with mock.patch('abridged_convert.conversion.run_command', side_effect=interrupting_run_command):
            with redirect_stdout(interrupted_stdout), redirect_stderr(interrupted_stderr):
                exit_code = main(['convert', str(self.repo_root), str(self.repo_root / 'Web'), '--category', 'Shots'])
        assert exit_code == 130
        assert 'Interrupted.' in interrupted_stderr.getvalue()
        assert 'Traceback' not in interrupted_stderr.getvalue()
        assert tracking_file.exists()
        assert not job.output_file.exists()
        resumed_stdout = io.StringIO()
        resumed_stderr = io.StringIO()
        with redirect_stdout(resumed_stdout), redirect_stderr(resumed_stderr):
            resumed_exit_code = main(['convert', str(self.repo_root), str(self.repo_root / 'Web'), '--category', 'Shots'])
        assert resumed_exit_code == 0
        assert not tracking_file.exists()
        probe = ffprobe_json(job.output_file)
        assert probe['format']['format_name'] == 'mov,mp4,m4a,3gp,3g2,mj2'

    def test_summary_prints_pruned_output_paths(self) -> None:
        entry = self.repo_root / 'Shots' / '[Creator] Renamed'
        entry.mkdir()
        self._make_source_video(entry / '1.mkv', title='Renamed', description='desc', comment='comment')
        old_output = self.repo_root / 'Web' / 'Shots' / '[Creator] Old Name' / '1.mp4'
        old_cover = old_output.with_suffix('.webp')
        old_output.parent.mkdir(parents=True, exist_ok=True)
        old_output.write_text('old output', encoding='utf-8')
        old_cover.write_text('old cover', encoding='utf-8')
        metadata_path = self.repo_root / 'Web' / '.abridged-video-metadata.json'
        metadata_path.write_text(json.dumps({'version': 2, 'items': {'Shots/[Creator] Old Name/1.mp4': {'size': 10, 'modTimeUnixNano': 20, 'durationSeconds': 1, 'source': {'path': 'Shots/[Creator] Old Name/1.mkv', 'size': 30, 'modTimeUnixNano': 40}, 'outputPath': 'Shots/[Creator] Old Name/1.mp4', 'coverPath': 'Shots/[Creator] Old Name/1.webp'}}}), encoding='utf-8')
        stdout = io.StringIO()
        original_set_final_summary = TerminalUI.set_final_summary
        with redirect_stdout(stdout), mock.patch.object(TerminalUI, 'set_final_summary', autospec=True, side_effect=original_set_final_summary) as set_final_summary:
            exit_code = main(['convert', str(self.repo_root), str(self.repo_root / 'Web'), '--category', 'Shots'])
        output = stdout.getvalue()
        assert exit_code == 0
        assert 'Summary: converted=1 skipped=0 failed=0 pruned=1' in output
        assert '✓ pruned Shots/[Creator] Old Name/1.mp4' in output
        ui = set_final_summary.call_args.args[0]
        assert ui.final_message == 'converted=1 skipped=0 failed=0 pruned=1'
        assert not old_output.exists()
        assert not old_cover.exists()

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
        run([FFMPEG, '-y', '-v', 'error', '-f', 'lavfi', '-i', f'color=c={color}:s=160x90:d=1', '-frames:v', '1', str(output_path)])

    def test_subtitle_streams_become_vtt_sidecars_listed_in_metadata(self) -> None:
        entry = self.repo_root / 'Shots' / '[Creator] Subbed'
        entry.mkdir()
        self._make_cover_image(entry / 'cover.png', 'red')
        source = entry / '1.mkv'
        self._make_source_video(source, title='Subbed', description='desc', comment='comment')
        self._add_subtitle_streams(source, [('WEBVTT\n\n00:00.000 --> 00:00.900 align:start position:0%\nhello<00:00:00.400><c> there</c>\n', '.vtt', 'en', None, True), ('1\n00:00:00,000 --> 00:00:00,900\nHola\n', '.srt', 'es', 'Spanish (manual)', False)])
        exit_code = main(['convert', str(self.repo_root), str(self.repo_root / 'Web'), '--category', 'Shots'])
        assert exit_code == 0
        web_entry = self.repo_root / 'Web' / 'Shots' / '[Creator] Subbed'
        english = web_entry / '1.0.en.vtt'
        spanish = web_entry / '1.1.es.vtt'
        assert english.read_text(encoding='utf-8').startswith('WEBVTT')
        assert '<c> there</c>' in english.read_text(encoding='utf-8')
        assert spanish.read_text(encoding='utf-8').startswith('WEBVTT')
        assert 'Hola' in spanish.read_text(encoding='utf-8')
        output_streams = ffprobe_json(web_entry / '1.mp4')['streams']
        assert not any(stream['codec_type'] == 'subtitle' for stream in output_streams)
        metadata_cache = json.loads((self.repo_root / 'Web' / '.abridged-video-metadata.json').read_text(encoding='utf-8'))
        item = metadata_cache['items']['Shots/[Creator] Subbed/1.mp4']
        assert item['subtitles'] == [{'path': 'Shots/[Creator] Subbed/1.0.en.vtt', 'language': 'en', 'label': 'en', 'default': True, 'forced': False}, {'path': 'Shots/[Creator] Subbed/1.1.es.vtt', 'language': 'es', 'label': 'Spanish (manual)', 'default': False, 'forced': False}]
        spanish.unlink()
        assert main(['convert', str(self.repo_root), str(self.repo_root / 'Web'), '--category', 'Shots']) == 0
        assert spanish.is_file()

    def test_source_without_subtitles_records_empty_list(self) -> None:
        entry = self.repo_root / 'Shots' / '[Creator] Unsubbed'
        entry.mkdir()
        self._make_cover_image(entry / 'cover.png', 'red')
        self._make_source_video(entry / '1.mkv', title='Unsubbed', description='desc', comment='comment')
        assert main(['convert', str(self.repo_root), str(self.repo_root / 'Web'), '--category', 'Shots']) == 0
        metadata_cache = json.loads((self.repo_root / 'Web' / '.abridged-video-metadata.json').read_text(encoding='utf-8'))
        assert metadata_cache['items']['Shots/[Creator] Unsubbed/1.mp4']['subtitles'] == []

    def _add_subtitle_streams(self, source: Path, subtitles: list[tuple[str, str, str | None, str | None, bool]]) -> None:
        temp_output = source.with_name(f'{source.stem}.with-subs{source.suffix}')
        cmd = [FFMPEG, '-y', '-v', 'error', '-i', str(source)]
        for position, (text, suffix, _language, _title, _default) in enumerate(subtitles):
            sub_path = source.parent / f'fixture-sub-{position}{suffix}'
            sub_path.write_text(text, encoding='utf-8')
            cmd.extend(['-i', str(sub_path)])
        cmd.extend(['-map', '0'])
        for position in range(len(subtitles)):
            cmd.extend(['-map', f'{position + 1}:0'])
        cmd.extend(['-c', 'copy'])
        for position, (_text, _suffix, language, title, default) in enumerate(subtitles):
            if language:
                cmd.extend([f'-metadata:s:s:{position}', f'language={language}'])
            if title:
                cmd.extend([f'-metadata:s:s:{position}', f'title={title}'])
            cmd.extend([f'-disposition:s:{position}', 'default' if default else '0'])
        cmd.append(str(temp_output))
        run(cmd)
        temp_output.replace(source)
        for position, (_text, suffix, *_rest) in enumerate(subtitles):
            (source.parent / f'fixture-sub-{position}{suffix}').unlink()

    def _make_source_video(self, output_path: Path, *, title: str, description: str, comment: str, artist: str='Artist', album: str='Album', date: str='20240202', synopsis: str='Synopsis', attach_json: bool=False, add_cover_stream: Path | None=None, add_cover_codec: str | None=None, add_cover_attached_pic: bool=True, video_codec: str='libvpx-vp9', audio_codec: str | None=None, duration: float=1) -> None:
        cmd = [FFMPEG, '-y', '-v', 'error', '-f', 'lavfi', '-i', f'testsrc=size=160x90:rate=12:duration={duration}', '-f', 'lavfi', '-i', f'sine=frequency=1000:duration={duration}']
        if add_cover_stream is not None:
            cmd.extend(['-i', str(add_cover_stream)])
        cmd.extend(['-map', '0:v:0', '-map', '1:a:0'])
        if add_cover_stream is not None:
            cmd.extend(['-map', '2:v:0'])
        cmd.extend(['-c:v:0', video_codec, '-pix_fmt', 'yuv420p'])
        if audio_codec is not None:
            cmd.extend(['-c:a:0', audio_codec])
        elif output_path.suffix.lower() == '.webm':
            cmd.extend(['-c:a:0', 'libopus'])
        else:
            cmd.extend(['-c:a:0', 'aac'])
        if add_cover_stream is not None:
            cmd.extend(['-c:v:1', add_cover_codec or 'png'])
            if add_cover_attached_pic:
                cmd.extend(['-disposition:v:1', 'attached_pic'])
        cmd.extend(['-metadata', f'title={title}', '-metadata', f'artist={artist}', '-metadata', f'album={album}', '-metadata', f'date={date}', '-metadata', f'description={description}', '-metadata', f'comment={comment}', '-metadata', f'synopsis={synopsis}'])
        json_path: Path | None = None
        if attach_json:
            json_path = output_path.parent / 'temp.info.json'
            json_path.write_text('{"source":"fixture"}\n', encoding='utf-8')
            cmd.extend(['-attach', str(json_path), '-metadata:s:t', 'mimetype=application/json'])
        cmd.append(str(output_path))
        run(cmd)
        if json_path is not None and json_path.exists():
            json_path.unlink()
