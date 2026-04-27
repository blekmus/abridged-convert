from __future__ import annotations
import json
import tempfile
from pathlib import Path
from unittest import mock
from abridged_convert.metadata import METADATA_CACHE_FILENAME, generate_metadata_cache, prune_missing_metadata_cache_items, prune_unexpected_metadata_cache_items, update_metadata_cache_item, update_metadata_cache_item_with_tracking

class MetadataCacheTests:

    def setup_method(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix='abridged-convert-metadata-')
        self.archive_root = Path(self.temp_dir.name)

    def teardown_method(self) -> None:
        self.temp_dir.cleanup()

    def test_generate_metadata_cache_scans_reuses_and_prunes_archive_outputs(self) -> None:
        series_video = self._write_video('Series/[Creator] Show/Episode 1 - Pilot.mp4')
        series_webm = self._write_video('Series/[Creator] Show/Episode 2 - Webm.webm')
        self._write_video('Series/[Creator] Show/extras/Ignored.mp4')
        shot_video = self._write_video('Shots/[Creator] Shot/1.mp4')
        short_webm = self._write_video('Shorts/[Creator] Short/1.webm')
        amv_video = self._write_video('Songs/AMV/[Creator] Song/1.mp4')
        song_mkv = self._write_video('Songs/[Creator] Song Copy/1.mkv')
        self._write_video('Songs/[Creator] Missing Canonical/2.mp4')
        cache_path = self.archive_root / METADATA_CACHE_FILENAME
        cache_path.write_text(json.dumps({'version': 2, 'items': {'Songs/Old/1.mp4': {'size': 1, 'modTimeUnixNano': 2, 'durationSeconds': 3}}}), encoding='utf-8')
        with mock.patch('abridged_convert.metadata.probe_video_metadata', return_value={'durationSeconds': 12, 'description': 'desc', 'date': '2024-01-01'}) as probe:
            summary = generate_metadata_cache(self.archive_root, ffprobe_bin='ffprobe')
        cache = json.loads(cache_path.read_text(encoding='utf-8'))
        assert summary.discovered == 6
        assert summary.reused == 0
        assert summary.probed == 6
        assert summary.pruned == 1
        assert summary.written
        assert probe.call_count == 6
        assert set(cache['items']) == {'Series/[Creator] Show/Episode 1 - Pilot.mp4', 'Series/[Creator] Show/Episode 2 - Webm.webm', 'Shots/[Creator] Shot/1.mp4', 'Shorts/[Creator] Short/1.webm', 'Songs/AMV/[Creator] Song/1.mp4', 'Songs/[Creator] Song Copy/1.mkv'}
        for key, video_path in {'Series/[Creator] Show/Episode 1 - Pilot.mp4': series_video, 'Series/[Creator] Show/Episode 2 - Webm.webm': series_webm, 'Shots/[Creator] Shot/1.mp4': shot_video, 'Shorts/[Creator] Short/1.webm': short_webm, 'Songs/AMV/[Creator] Song/1.mp4': amv_video, 'Songs/[Creator] Song Copy/1.mkv': song_mkv}.items():
            item = cache['items'][key]
            assert item['size'] == video_path.stat().st_size
            assert item['modTimeUnixNano'] == video_path.stat().st_mtime_ns
            assert item['durationSeconds'] == 12
            assert item['description'] == 'desc'
            assert item['date'] == '2024-01-01'
        with mock.patch('abridged_convert.metadata.probe_video_metadata') as probe_again:
            reuse_summary = generate_metadata_cache(self.archive_root, ffprobe_bin='ffprobe')
        assert reuse_summary.discovered == 6
        assert reuse_summary.reused == 6
        assert reuse_summary.probed == 0
        assert not reuse_summary.written
        probe_again.assert_not_called()

    def test_failed_probe_keeps_existing_stale_cache_item(self) -> None:
        video_path = self._write_video('Shots/[Creator] Shot/1.mp4')
        cache_path = self.archive_root / METADATA_CACHE_FILENAME
        cache_path.write_text(json.dumps({'version': 2, 'items': {'Shots/[Creator] Shot/1.mp4': {'size': video_path.stat().st_size + 1, 'modTimeUnixNano': video_path.stat().st_mtime_ns, 'durationSeconds': 9}}}), encoding='utf-8')
        with mock.patch('abridged_convert.metadata.probe_video_metadata', return_value=None):
            summary = generate_metadata_cache(self.archive_root, ffprobe_bin='ffprobe')
        cache = json.loads(cache_path.read_text(encoding='utf-8'))
        assert summary.failed == 1
        assert summary.pruned == 0
        assert cache['items']['Shots/[Creator] Shot/1.mp4']['durationSeconds'] == 9

    def test_category_scoped_generation_preserves_other_categories(self) -> None:
        series_video = self._write_video('Series/[Creator] Show/Episode 1 - Pilot.mp4')
        song_video = self._write_video('Songs/[Creator] Song/1.webm')
        stale_song_video = self._write_video('Songs/[Creator] Stale/1.webm')
        stale_song_video.unlink()
        cache_path = self.archive_root / METADATA_CACHE_FILENAME
        cache_path.write_text(json.dumps({'version': 2, 'items': {'Series/[Creator] Show/Episode 1 - Pilot.mp4': {'size': series_video.stat().st_size + 1, 'modTimeUnixNano': series_video.stat().st_mtime_ns + 1, 'durationSeconds': 99}, 'Songs/[Creator] Stale/1.webm': {'size': 1, 'modTimeUnixNano': 2, 'durationSeconds': 3}}}), encoding='utf-8')
        with mock.patch('abridged_convert.metadata.probe_video_metadata', return_value={'durationSeconds': 12}) as probe:
            summary = generate_metadata_cache(self.archive_root, ffprobe_bin='ffprobe', categories=['Songs'])
        cache = json.loads(cache_path.read_text(encoding='utf-8'))
        assert summary.discovered == 1
        assert summary.probed == 1
        assert summary.pruned == 1
        probe.assert_called_once_with('ffprobe', song_video)
        assert set(cache['items']) == {'Series/[Creator] Show/Episode 1 - Pilot.mp4', 'Songs/[Creator] Song/1.webm'}
        assert cache['items']['Series/[Creator] Show/Episode 1 - Pilot.mp4']['durationSeconds'] == 99

    def test_generation_preserves_item_level_deploy_tracking(self) -> None:
        video_path = self._write_video('Shots/[Creator] Shot/1.mp4')
        cache_path = self.archive_root / METADATA_CACHE_FILENAME
        cache_path.write_text(json.dumps({'version': 2, 'items': {'Shots/[Creator] Shot/1.mp4': {'size': video_path.stat().st_size + 1, 'modTimeUnixNano': video_path.stat().st_mtime_ns, 'durationSeconds': 9, 'source': {'path': 'Shots/[Creator] Shot/1.mkv'}, 'outputPath': 'Shots/[Creator] Shot/1.mp4', 'coverPath': 'Shots/[Creator] Shot/1.webp', 'deployedAt': '2024-01-01T00:00:00+00:00'}}}), encoding='utf-8')
        with mock.patch('abridged_convert.metadata.probe_video_metadata', return_value={'durationSeconds': 12}):
            generate_metadata_cache(self.archive_root, ffprobe_bin='ffprobe')
        cache = json.loads(cache_path.read_text(encoding='utf-8'))
        item = cache['items']['Shots/[Creator] Shot/1.mp4']
        assert item['durationSeconds'] == 12
        assert item['source'] == {'path': 'Shots/[Creator] Shot/1.mkv'}
        assert item['outputPath'] == 'Shots/[Creator] Shot/1.mp4'
        assert item['coverPath'] == 'Shots/[Creator] Shot/1.webp'
        assert item['deployedAt'] == '2024-01-01T00:00:00+00:00'
        assert 'deploy' not in cache

    def test_generation_ignores_top_level_deploy_tracking(self) -> None:
        video_path = self._write_video('Shots/[Creator] Shot/1.mp4')
        cache_path = self.archive_root / METADATA_CACHE_FILENAME
        cache_path.write_text(json.dumps({'version': 2, 'items': {'Shots/[Creator] Shot/1.mp4': {'size': video_path.stat().st_size + 1, 'modTimeUnixNano': video_path.stat().st_mtime_ns, 'durationSeconds': 9}}, 'deploy': {'version': 1, 'items': {'Shots/[Creator] Shot/1.mp4': {'source': {'path': 'Shots/[Creator] Shot/1.mkv'}, 'outputPath': 'Shots/[Creator] Shot/1.mp4', 'coverPath': 'Shots/[Creator] Shot/1.webp', 'deployedAt': '2024-01-01T00:00:00+00:00'}}}}), encoding='utf-8')
        with mock.patch('abridged_convert.metadata.probe_video_metadata', return_value={'durationSeconds': 12}):
            generate_metadata_cache(self.archive_root, ffprobe_bin='ffprobe')
        cache = json.loads(cache_path.read_text(encoding='utf-8'))
        item = cache['items']['Shots/[Creator] Shot/1.mp4']
        assert item['durationSeconds'] == 12
        assert 'source' not in item
        assert 'outputPath' not in item
        assert 'coverPath' not in item
        assert 'deployedAt' not in item
        assert 'deploy' not in cache

    def test_update_metadata_cache_item_upserts_one_finished_output(self) -> None:
        old_video = self._write_video('Shots/[Creator] Existing/1.mp4')
        new_video = self._write_video('Shots/[Creator] New/1.mp4')
        cache_path = self.archive_root / METADATA_CACHE_FILENAME
        cache_path.write_text(json.dumps({'version': 2, 'items': {'Shots/[Creator] Existing/1.mp4': {'size': old_video.stat().st_size, 'modTimeUnixNano': old_video.stat().st_mtime_ns, 'durationSeconds': 9}}}), encoding='utf-8')
        with mock.patch('abridged_convert.metadata.probe_video_metadata', return_value={'durationSeconds': 12, 'description': 'new'}) as probe:
            summary = update_metadata_cache_item(self.archive_root, new_video, ffprobe_bin='ffprobe')
        cache = json.loads(cache_path.read_text(encoding='utf-8'))
        assert summary.probed
        assert summary.written
        probe.assert_called_once_with('ffprobe', new_video)
        assert cache['items']['Shots/[Creator] Existing/1.mp4']['durationSeconds'] == 9
        assert cache['items']['Shots/[Creator] New/1.mp4']['durationSeconds'] == 12
        assert cache['items']['Shots/[Creator] New/1.mp4']['description'] == 'new'

    def test_update_metadata_cache_item_writes_tracking_fields(self) -> None:
        video = self._write_video('Shots/[Creator] New/1.mp4')
        cache_path = self.archive_root / METADATA_CACHE_FILENAME
        deploy_item = {'source': {'path': 'Shots/[Creator] New/1.mkv', 'size': 100, 'modTimeUnixNano': 200}, 'outputPath': 'Shots/[Creator] New/1.mp4', 'coverPath': 'Shots/[Creator] New/1.webp'}
        with mock.patch('abridged_convert.metadata.probe_video_metadata', return_value={'durationSeconds': 12}):
            update_metadata_cache_item_with_tracking(self.archive_root, video, ffprobe_bin='ffprobe', deploy_item=deploy_item)
        cache = json.loads(cache_path.read_text(encoding='utf-8'))
        item = cache['items']['Shots/[Creator] New/1.mp4']
        assert item['source'] == deploy_item['source']
        assert item['outputPath'] == deploy_item['outputPath']
        assert item['coverPath'] == deploy_item['coverPath']

    def test_prune_missing_metadata_cache_items_removes_deleted_outputs_without_probe(self) -> None:
        existing_video = self._write_video('Shots/[Creator] Existing/1.mp4')
        cache_path = self.archive_root / METADATA_CACHE_FILENAME
        cache_path.write_text(json.dumps({'version': 2, 'items': {'Shots/[Creator] Existing/1.mp4': {'size': existing_video.stat().st_size, 'modTimeUnixNano': existing_video.stat().st_mtime_ns, 'durationSeconds': 9}, 'Shots/[Creator] Missing/1.mp4': {'size': 1, 'modTimeUnixNano': 2, 'durationSeconds': 3}}}), encoding='utf-8')
        with mock.patch('abridged_convert.metadata.probe_video_metadata') as probe:
            summary = prune_missing_metadata_cache_items(self.archive_root, categories=['Shots'])
        cache = json.loads(cache_path.read_text(encoding='utf-8'))
        assert summary.pruned == 1
        assert summary.pruned_keys == ('Shots/[Creator] Missing/1.mp4',)
        assert summary.written
        probe.assert_not_called()
        assert set(cache['items']) == {'Shots/[Creator] Existing/1.mp4'}

    def test_prune_unexpected_metadata_cache_items_deletes_stale_output_folder(self) -> None:
        old_video = self._write_video('Songs/AMV/[Fain] More/1.mp4')
        old_cover = self._write_video('Songs/AMV/[Fain] More/1.webp')
        new_video = self._write_video('Songs/AMV/[Fain] More oooooooooooof/1.mp4')
        cache_path = self.archive_root / METADATA_CACHE_FILENAME
        cache_path.write_text(json.dumps({'version': 2, 'items': {'Songs/AMV/[Fain] More/1.mp4': {'size': old_video.stat().st_size, 'modTimeUnixNano': old_video.stat().st_mtime_ns, 'durationSeconds': 225, 'source': {'path': '[~] AMVs/[Fain] More.mkv', 'size': 100, 'modTimeUnixNano': 200}, 'outputPath': 'Songs/AMV/[Fain] More/1.mp4', 'coverPath': 'Songs/AMV/[Fain] More/1.webp'}, 'Songs/AMV/[Fain] More oooooooooooof/1.mp4': {'size': new_video.stat().st_size, 'modTimeUnixNano': new_video.stat().st_mtime_ns, 'durationSeconds': 225}}}), encoding='utf-8')
        summary = prune_unexpected_metadata_cache_items(self.archive_root, {'Songs/AMV/[Fain] More oooooooooooof/1.mp4'}, categories=['Songs'], delete_files=True)
        cache = json.loads(cache_path.read_text(encoding='utf-8'))
        assert summary.pruned == 1
        assert summary.pruned_keys == ('Songs/AMV/[Fain] More/1.mp4',)
        assert summary.written
        assert not old_video.exists()
        assert not old_cover.exists()
        assert not (self.archive_root / 'Songs/AMV/[Fain] More').exists()
        assert set(cache['items']) == {'Songs/AMV/[Fain] More oooooooooooof/1.mp4'}

    def _write_video(self, relative_path: str) -> Path:
        path = self.archive_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('video', encoding='utf-8')
        return path
