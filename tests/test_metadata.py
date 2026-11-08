from __future__ import annotations
import json
import tempfile
from pathlib import Path
from unittest import mock
from abridged_convert.metadata import METADATA_CACHE_FILENAME, prune_missing_metadata_cache_items, prune_unexpected_metadata_cache_items, update_metadata_cache_item_with_tracking

class MetadataCacheTests:

    def setup_method(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix='abridged-convert-metadata-')
        self.archive_root = Path(self.temp_dir.name)

    def teardown_method(self) -> None:
        self.temp_dir.cleanup()

    def test_update_metadata_cache_item_upserts_one_finished_output(self) -> None:
        old_video = self._write_video('Shots/[Creator] Existing/1.mp4')
        new_video = self._write_video('Shots/[Creator] New/1.mp4')
        cache_path = self.archive_root / METADATA_CACHE_FILENAME
        cache_path.write_text(json.dumps({'version': 2, 'items': {'Shots/[Creator] Existing/1.mp4': {'size': old_video.stat().st_size, 'modTimeUnixNano': old_video.stat().st_mtime_ns, 'durationSeconds': 9}}}), encoding='utf-8')
        with mock.patch('abridged_convert.metadata.probe_video_metadata', return_value={'durationSeconds': 12, 'description': 'new'}) as probe:
            summary = update_metadata_cache_item_with_tracking(self.archive_root, new_video, ffprobe_bin='ffprobe')
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

    def test_prune_unexpected_keeps_files_shared_with_surviving_item(self) -> None:
        # The source switched from mp4 to webm: same entry folder, same cover and subtitle names.
        old_video = self._write_video('Shots/[Creator] Shot/1.mp4')
        new_video = self._write_video('Shots/[Creator] Shot/1.webm')
        cover = self._write_video('Shots/[Creator] Shot/1.webp')
        subtitle = self._write_video('Shots/[Creator] Shot/1.0.en.vtt')
        dropped_subtitle = self._write_video('Shots/[Creator] Shot/1.1.es.vtt')

        def item(output: str, subtitles: list[str]) -> dict:
            return {'size': 5, 'modTimeUnixNano': 1, 'outputPath': output, 'coverPath': 'Shots/[Creator] Shot/1.webp', 'subtitles': [{'path': path} for path in subtitles]}

        cache_path = self.archive_root / METADATA_CACHE_FILENAME
        cache_path.write_text(json.dumps({'version': 2, 'items': {
            'Shots/[Creator] Shot/1.mp4': item('Shots/[Creator] Shot/1.mp4', ['Shots/[Creator] Shot/1.0.en.vtt', 'Shots/[Creator] Shot/1.1.es.vtt']),
            'Shots/[Creator] Shot/1.webm': item('Shots/[Creator] Shot/1.webm', ['Shots/[Creator] Shot/1.0.en.vtt']),
        }}), encoding='utf-8')
        summary = prune_unexpected_metadata_cache_items(self.archive_root, {'Shots/[Creator] Shot/1.webm'}, categories=['Shots'], delete_files=True)
        assert summary.pruned_keys == ('Shots/[Creator] Shot/1.mp4',)
        assert not old_video.exists()
        assert not dropped_subtitle.exists()
        assert new_video.exists()
        assert cover.exists()
        assert subtitle.exists()

    def _write_video(self, relative_path: str) -> Path:
        path = self.archive_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('video', encoding='utf-8')
        return path
