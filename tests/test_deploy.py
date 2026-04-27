from __future__ import annotations
import pytest
import json
import subprocess
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator
from unittest import mock
from abridged_convert.app import main
from abridged_convert.common import source_signature
from abridged_convert.deploy import validate_remote_relpath
from abridged_convert.metadata import METADATA_CACHE_FILENAME
from abridged_convert.models import ConversionResult, VideoEncodingConfig

class FakeRclone:

    def __init__(self, remote_root: str, files: dict[str, bytes] | None=None) -> None:
        self.remote_root = remote_root.rstrip('/')
        self.files: dict[str, bytes] = dict(files or {})
        self.calls: list[list[str]] = []

    def __call__(self, rclone_bin: str, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[:3] == ['lsf', '--recursive', '--files-only']:
            self.assert_remote_root(args[3])
            return subprocess.CompletedProcess([rclone_bin, *args], 0, stdout='\n'.join(sorted(self.files)) + ('\n' if self.files else ''), stderr='')
        if args[0] == 'copyto':
            source, destination = (args[1], args[2])
            if source.startswith(f'{self.remote_root}/'):
                relpath = source.removeprefix(f'{self.remote_root}/')
                Path(destination).parent.mkdir(parents=True, exist_ok=True)
                Path(destination).write_bytes(self.files[relpath])
            elif destination.startswith(f'{self.remote_root}/'):
                relpath = destination.removeprefix(f'{self.remote_root}/')
                self.files[relpath] = Path(source).read_bytes()
            else:
                raise AssertionError(f'copyto left remote root: {args}')
            return subprocess.CompletedProcess([rclone_bin, *args], 0, stdout='', stderr='')
        if args[0] == 'deletefile':
            destination = args[1]
            if not destination.startswith(f'{self.remote_root}/'):
                raise AssertionError(f'deletefile left remote root: {args}')
            relpath = destination.removeprefix(f'{self.remote_root}/')
            self.files.pop(relpath, None)
            return subprocess.CompletedProcess([rclone_bin, *args], 0, stdout='', stderr='')
        raise AssertionError(f'unexpected rclone call: {args}')

    def assert_remote_root(self, value: str) -> None:
        if value != self.remote_root:
            raise AssertionError(f'remote root changed: {value}')

    def uploaded_media_paths(self) -> list[str]:
        return [args[2].removeprefix(f'{self.remote_root}/') for args in self.calls if args[0] == 'copyto' and args[2].startswith(f'{self.remote_root}/') and (not args[2].endswith(METADATA_CACHE_FILENAME))]

    def deleted_paths(self) -> list[str]:
        return [args[1].removeprefix(f'{self.remote_root}/') for args in self.calls if args[0] == 'deletefile']

class DeployTests:

    def setup_method(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix='abridged-convert-deploy-')
        self.root = Path(self.temp_dir.name)
        self.archive = self.root / 'Archive'
        self.stage = self.root / 'Web'
        (self.archive / 'Shots' / '[Creator] Shot').mkdir(parents=True)
        (self.archive / 'Shots' / '[Creator] Shot' / '1.mkv').write_text('source', encoding='utf-8')
        self.remote_root = 'test:Abridged'

    def teardown_method(self) -> None:
        self.temp_dir.cleanup()

    def test_first_deploy_uploads_outputs_metadata_and_state_then_cleans_staging(self) -> None:
        rclone = FakeRclone(self.remote_root)
        with self._patched_deploy(rclone):
            exit_code = main([str(self.archive), str(self.stage), '--deploy', self.remote_root])
        assert exit_code == 0
        assert not self.stage.exists()
        assert 'Shots/[Creator] Shot/1.mp4' in rclone.files
        assert 'Shots/[Creator] Shot/1.webp' in rclone.files
        assert METADATA_CACHE_FILENAME in rclone.files
        assert rclone.uploaded_media_paths()
        metadata = json.loads(rclone.files[METADATA_CACHE_FILENAME].decode('utf-8'))
        assert metadata['version'] == 2
        assert set(metadata['items']) == {'Shots/[Creator] Shot/1.mp4'}
        item = metadata['items']['Shots/[Creator] Shot/1.mp4']
        assert item['durationSeconds'] == 1
        assert item['outputPath'] == 'Shots/[Creator] Shot/1.mp4'
        assert item['coverPath'] == 'Shots/[Creator] Shot/1.webp'
        assert 'source' in item
        assert 'deployedAt' in item
        assert 'deploy' not in metadata

    def test_unchanged_deploy_preserves_remote_metadata_and_skips_media_uploads(self) -> None:
        source = self.archive / 'Shots' / '[Creator] Shot' / '1.mkv'
        output_key = 'Shots/[Creator] Shot/1.mp4'
        cover_key = 'Shots/[Creator] Shot/1.webp'
        metadata = {'version': 2, 'items': {output_key: {'size': 123, 'modTimeUnixNano': 456, 'durationSeconds': 7, 'description': 'remote', 'source': source_signature(self.archive, source), 'outputPath': output_key, 'coverPath': cover_key, 'deployedAt': '2024-01-01T00:00:00+00:00'}}}
        rclone = FakeRclone(self.remote_root, {output_key: b'remote video', cover_key: b'remote cover', METADATA_CACHE_FILENAME: json.dumps(metadata).encode('utf-8')})
        with self._patched_deploy(rclone) as convert_job:
            exit_code = main([str(self.archive), str(self.stage), '--deploy', self.remote_root])
        assert exit_code == 0
        convert_job.assert_not_called()
        assert rclone.uploaded_media_paths() == []
        next_metadata = json.loads(rclone.files[METADATA_CACHE_FILENAME].decode('utf-8'))
        assert next_metadata['items'][output_key]['description'] == 'remote'
        assert next_metadata['items'][output_key]['outputPath'] == output_key
        assert 'deploy' not in next_metadata

    def test_rename_uploads_new_path_and_deletes_old_remote_path(self) -> None:
        old_output = 'Shots/[Creator] Old Shot/1.mp4'
        old_cover = 'Shots/[Creator] Old Shot/1.webp'
        old_metadata = {'version': 2, 'items': {old_output: {'size': 123, 'modTimeUnixNano': 456, 'durationSeconds': 7, 'source': {'path': 'Shots/[Creator] Old Shot/1.mkv', 'size': 6, 'modTimeUnixNano': 1}, 'outputPath': old_output, 'coverPath': old_cover, 'deployedAt': '2024-01-01T00:00:00+00:00'}}}
        rclone = FakeRclone(self.remote_root, {old_output: b'old video', old_cover: b'old cover', METADATA_CACHE_FILENAME: json.dumps(old_metadata).encode('utf-8')})
        with self._patched_deploy(rclone):
            exit_code = main([str(self.archive), str(self.stage), '--deploy', self.remote_root])
        assert exit_code == 0
        assert 'Shots/[Creator] Shot/1.mp4' in rclone.files
        assert old_output not in rclone.files
        assert old_cover not in rclone.files
        assert sorted(rclone.deleted_paths()) == [old_output, old_cover]

    def test_category_limited_deploy_prunes_only_selected_category(self) -> None:
        source = self.archive / 'Shots' / '[Creator] Shot' / '1.mkv'
        output_key = 'Shots/[Creator] Shot/1.mp4'
        cover_key = 'Shots/[Creator] Shot/1.webp'
        old_output = 'Shots/[Creator] Old Shot/1.mp4'
        old_cover = 'Shots/[Creator] Old Shot/1.webp'
        song_output = 'Songs/[Singer] Song/1.mp4'
        song_cover = 'Songs/[Singer] Song/1.webp'
        metadata = {'version': 2, 'items': {output_key: {'size': 123, 'modTimeUnixNano': 456, 'durationSeconds': 7, 'source': source_signature(self.archive, source), 'outputPath': output_key, 'coverPath': cover_key, 'deployedAt': '2024-01-01T00:00:00+00:00'}, old_output: {'size': 111, 'modTimeUnixNano': 222, 'durationSeconds': 3, 'source': {'path': 'Shots/[Creator] Old Shot/1.mkv', 'size': 6, 'modTimeUnixNano': 1}, 'outputPath': old_output, 'coverPath': old_cover, 'deployedAt': '2024-01-01T00:00:00+00:00'}, song_output: {'size': 333, 'modTimeUnixNano': 444, 'durationSeconds': 9, 'source': {'path': 'Songs/[Singer] Song/1.mkv', 'size': 8, 'modTimeUnixNano': 2}, 'outputPath': song_output, 'coverPath': song_cover, 'deployedAt': '2024-01-01T00:00:00+00:00'}}}
        rclone = FakeRclone(self.remote_root, {output_key: b'remote video', cover_key: b'remote cover', old_output: b'old video', old_cover: b'old cover', song_output: b'song video', song_cover: b'song cover', METADATA_CACHE_FILENAME: json.dumps(metadata).encode('utf-8')})
        with self._patched_deploy(rclone) as convert_job:
            exit_code = main([str(self.archive), str(self.stage), '--category', 'Shots', '--deploy', self.remote_root])
        assert exit_code == 0
        convert_job.assert_not_called()
        assert sorted(rclone.deleted_paths()) == [old_output, old_cover]
        assert song_output in rclone.files
        assert song_cover in rclone.files
        next_metadata = json.loads(rclone.files[METADATA_CACHE_FILENAME].decode('utf-8'))
        assert set(next_metadata['items']) == {output_key, song_output}
        assert next_metadata['items'][song_output]['durationSeconds'] == 9
        assert next_metadata['items'][song_output]['outputPath'] == song_output

    def test_interrupted_deploy_reuses_staged_output_on_next_run(self) -> None:
        rclone = FakeRclone(self.remote_root)
        with self._patched_deploy(rclone) as convert_job:
            with mock.patch('abridged_convert.app.finalize_deploy', side_effect=KeyboardInterrupt):
                exit_code = main([str(self.archive), str(self.stage), '--deploy', self.remote_root])
        assert exit_code == 130
        assert (self.stage / METADATA_CACHE_FILENAME).is_file()
        assert (self.stage / 'Shots' / '[Creator] Shot' / '1.mp4').is_file()
        assert (self.stage / 'Shots' / '[Creator] Shot' / '1.webp').is_file()
        assert convert_job.call_count == 1
        assert rclone.uploaded_media_paths() == []
        with self._patched_deploy(rclone) as resumed_convert_job:
            resumed_exit_code = main([str(self.archive), str(self.stage), '--deploy', self.remote_root])
        assert resumed_exit_code == 0
        resumed_convert_job.assert_not_called()
        assert 'Shots/[Creator] Shot/1.mp4' in rclone.files
        assert 'Shots/[Creator] Shot/1.webp' in rclone.files
        assert METADATA_CACHE_FILENAME in rclone.files

    def test_bootstrap_deploy_generates_local_metadata_and_state_without_conversion(self) -> None:
        self._write_existing_outputs()
        with mock.patch('abridged_convert.app.convert_job') as convert_job:
            with mock.patch('abridged_convert.deploy.build_metadata_cache_item', return_value={'size': 15, 'modTimeUnixNano': 20, 'durationSeconds': 1}):
                exit_code = main([str(self.archive), str(self.stage), '--bootstrap-deploy'])
        assert exit_code == 0
        convert_job.assert_not_called()
        metadata = json.loads((self.stage / METADATA_CACHE_FILENAME).read_text(encoding='utf-8'))
        assert set(metadata['items']) == {'Shots/[Creator] Shot/1.mp4'}
        assert metadata['items']['Shots/[Creator] Shot/1.mp4']['outputPath'] == 'Shots/[Creator] Shot/1.mp4'
        assert 'deploy' not in metadata
        assert (self.stage / 'Shots' / '[Creator] Shot' / '1.mp4').exists()

    def test_bootstrap_deploy_uploads_existing_outputs_and_root_files(self) -> None:
        self._write_existing_outputs()
        rclone = FakeRclone(self.remote_root, {'Shots/[Creator] Old Shot/1.mp4': b'old video', 'Shots/[Creator] Old Shot/1.webp': b'old cover'})
        with self._patched_rclone(rclone):
            with mock.patch('abridged_convert.deploy.build_metadata_cache_item', return_value={'size': 15, 'modTimeUnixNano': 20, 'durationSeconds': 1}):
                exit_code = main([str(self.archive), str(self.stage), '--deploy', self.remote_root, '--bootstrap-deploy'])
        assert exit_code == 0
        assert not self.stage.exists()
        assert 'Shots/[Creator] Shot/1.mp4' in rclone.files
        assert 'Shots/[Creator] Shot/1.webp' in rclone.files
        assert METADATA_CACHE_FILENAME in rclone.files
        assert sorted(rclone.deleted_paths()) == ['Shots/[Creator] Old Shot/1.mp4', 'Shots/[Creator] Old Shot/1.webp']

    def test_rejects_unsafe_remote_relative_paths(self) -> None:
        with pytest.raises(Exception, match='unsafe remote relative path'):
            validate_remote_relpath('../outside.mp4')
        with pytest.raises(Exception, match='unsafe remote relative path'):
            validate_remote_relpath('/outside.mp4')
        with pytest.raises(Exception, match='unsafe remote relative path'):
            validate_remote_relpath('Series\\outside.mp4')

    @contextmanager
    def _patched_deploy(self, rclone: FakeRclone) -> Iterator[mock.Mock]:

        def fake_convert_job(job, **_kwargs):
            job.output_file.parent.mkdir(parents=True, exist_ok=True)
            job.output_file.write_bytes(b'converted video')
            job.output_file.with_suffix('.webp').write_bytes(b'converted cover')
            return ConversionResult(category=job.category, entry_dir=job.entry_dir, source_video=job.source_video, output_file=job.output_file, success=True, cover_source='external:cover.png', message='converted')
        convert_job = mock.Mock(side_effect=fake_convert_job)
        with ExitStack() as stack:
            stack.enter_context(mock.patch('abridged_convert.app.convert_job', convert_job))
            stack.enter_context(mock.patch('abridged_convert.app.resolve_video_handling_plan', return_value=('.mp4', VideoEncodingConfig(mode='cpu', codec='libx264', extra_args=()))))
            stack.enter_context(self._patched_rclone(rclone))
            stack.enter_context(mock.patch('abridged_convert.deploy.build_metadata_cache_item', return_value={'size': 15, 'modTimeUnixNano': 20, 'durationSeconds': 1}))
            stack.enter_context(mock.patch('abridged_convert.metadata.probe_video_metadata', return_value={'durationSeconds': 1}))
            yield convert_job

    @contextmanager
    def _patched_rclone(self, rclone: FakeRclone) -> Iterator[None]:
        with ExitStack() as stack:
            stack.enter_context(mock.patch('abridged_convert.deploy.run_rclone', side_effect=rclone))
            stack.enter_context(mock.patch('abridged_convert.deploy.run_rclone_progress', side_effect=rclone))
            yield

    def _write_existing_outputs(self) -> None:
        output_dir = self.stage / 'Shots' / '[Creator] Shot'
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / '1.mp4').write_bytes(b'existing video')
        (output_dir / '1.webp').write_bytes(b'existing cover')
