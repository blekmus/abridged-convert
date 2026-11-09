from __future__ import annotations
import pytest
import io
import json
import subprocess
import tempfile
from contextlib import ExitStack, contextmanager
from contextlib import redirect_stdout
from pathlib import Path
from typing import Iterator
from unittest import mock
from abridged_convert.app import main
from abridged_convert.common import source_signature
from abridged_convert.deploy import validate_remote_relpath
from abridged_convert.metadata import METADATA_CACHE_FILENAME
from abridged_convert.models import ConversionError, ConversionResult, VideoEncodingConfig

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
                if relpath not in self.files:
                    raise ConversionError(f'not found: {relpath}')
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
        if args[0] == 'rmdir':
            destination = args[1]
            if not destination.startswith(f'{self.remote_root}/'):
                raise AssertionError(f'rmdir left remote root: {args}')
            relpath = destination.removeprefix(f'{self.remote_root}/').rstrip('/')
            if any(path == relpath or path.startswith(f'{relpath}/') for path in self.files):
                raise AssertionError(f'rmdir called for non-empty remote directory: {args}')
            return subprocess.CompletedProcess([rclone_bin, *args], 0, stdout='', stderr='')
        raise AssertionError(f'unexpected rclone call: {args}')

    def assert_remote_root(self, value: str) -> None:
        if value != self.remote_root:
            raise AssertionError(f'remote root changed: {value}')

    def uploaded_media_paths(self) -> list[str]:
        return [args[2].removeprefix(f'{self.remote_root}/') for args in self.calls if args[0] == 'copyto' and args[2].startswith(f'{self.remote_root}/') and (not args[2].endswith(METADATA_CACHE_FILENAME))]

    def deleted_paths(self) -> list[str]:
        return [args[1].removeprefix(f'{self.remote_root}/') for args in self.calls if args[0] == 'deletefile']

    def removed_dirs(self) -> list[str]:
        return [args[1].removeprefix(f'{self.remote_root}/') for args in self.calls if args[0] == 'rmdir']

class DeployTests:

    def setup_method(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix='abridged-convert-deploy-')
        self.root = Path(self.temp_dir.name)
        self.archive = self.root / 'Archive'
        # Named like a real temp staging dir, since resume only trusts dirs the tool created.
        self.stage = self.root / 'abridged-deploy-test'
        self.pointer = self.archive / '.abridged-deploy.json'
        (self.archive / 'Shots' / '[Creator] Shot').mkdir(parents=True)
        (self.archive / 'Shots' / '[Creator] Shot' / '1.mkv').write_text('source', encoding='utf-8')
        self.remote_root = 'test:Abridged'
        self.fake_subtitle_languages: list[str] = []

    def teardown_method(self) -> None:
        self.temp_dir.cleanup()

    def test_first_deploy_uploads_outputs_metadata_and_state_then_cleans_staging(self) -> None:
        rclone = FakeRclone(self.remote_root)
        with self._patched_deploy(rclone):
            exit_code = main(['deploy', str(self.archive), self.remote_root])
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
        assert 'uploaded' not in item
        assert 'deployRunSignature' in metadata
        assert 'deployRunStatus' not in metadata
        assert 'metadataUploadPending' not in metadata
        assert 'deploy' not in metadata

    def test_unchanged_deploy_preserves_remote_metadata_and_skips_media_uploads(self) -> None:
        source = self.archive / 'Shots' / '[Creator] Shot' / '1.mkv'
        output_key = 'Shots/[Creator] Shot/1.mp4'
        cover_key = 'Shots/[Creator] Shot/1.webp'
        metadata = {'version': 2, 'items': {output_key: {'size': 123, 'modTimeUnixNano': 456, 'durationSeconds': 7, 'description': 'remote', 'source': source_signature(self.archive, source), 'outputPath': output_key, 'coverPath': cover_key, 'deployedAt': '2024-01-01T00:00:00+00:00', 'subtitles': []}}}
        rclone = FakeRclone(self.remote_root, {output_key: b'remote video', cover_key: b'remote cover', METADATA_CACHE_FILENAME: json.dumps(metadata).encode('utf-8')})
        with self._patched_deploy(rclone) as convert_job:
            exit_code = main(['deploy', str(self.archive), self.remote_root])
        assert exit_code == 0
        convert_job.assert_not_called()
        assert rclone.uploaded_media_paths() == []
        assert not any(args[0] == 'copyto' and args[2] == f'{self.remote_root}/{METADATA_CACHE_FILENAME}' for args in rclone.calls)
        next_metadata = json.loads(rclone.files[METADATA_CACHE_FILENAME].decode('utf-8'))
        assert next_metadata == metadata
        assert next_metadata['items'][output_key]['description'] == 'remote'
        assert next_metadata['items'][output_key]['outputPath'] == output_key
        assert 'deploy' not in next_metadata

    def test_unchanged_deploy_matches_source_path_and_size_without_mtime(self) -> None:
        source = self.archive / 'Shots' / '[Creator] Shot' / '1.mkv'
        output_key = 'Shots/[Creator] Shot/1.mp4'
        cover_key = 'Shots/[Creator] Shot/1.webp'
        source_match = {'path': 'Shots/[Creator] Shot/1.mkv', 'size': source.stat().st_size, 'modTimeUnixNano': -1}
        metadata = {'version': 2, 'items': {output_key: {'size': 123, 'modTimeUnixNano': 456, 'durationSeconds': 7, 'source': source_match, 'outputPath': output_key, 'coverPath': cover_key, 'deployedAt': '2024-01-01T00:00:00+00:00', 'subtitles': []}}}
        rclone = FakeRclone(self.remote_root, {output_key: b'remote video', cover_key: b'remote cover', METADATA_CACHE_FILENAME: json.dumps(metadata).encode('utf-8')})
        with self._patched_deploy(rclone) as convert_job:
            exit_code = main(['deploy', str(self.archive), self.remote_root])
        assert exit_code == 0
        convert_job.assert_not_called()
        assert rclone.uploaded_media_paths() == []

    def test_category_folder_archive_is_rejected_with_corrected_command(self, capsys) -> None:
        rclone = FakeRclone(self.remote_root)
        with self._patched_deploy(rclone) as convert_job:
            exit_code = main(['deploy', str(self.archive / 'Shots'), self.remote_root])
        assert exit_code == 2
        convert_job.assert_not_called()
        assert rclone.calls == []
        err = capsys.readouterr().err
        assert 'ARCHIVE must be the archive root, not the Shots category folder' in err
        assert f'Try: abridged-convert deploy {self.archive.as_posix()} {self.remote_root} --category Shots' in err

    def test_entry_deploy_only_touches_that_entry(self) -> None:
        (self.archive / 'Shots' / '[New] Shot').mkdir(parents=True)
        (self.archive / 'Shots' / '[New] Shot' / '1.mkv').write_text('new source', encoding='utf-8')
        source = self.archive / 'Shots' / '[Creator] Shot' / '1.mkv'
        kept_key = 'Shots/[Creator] Shot/1.mp4'
        gone_key = 'Shots/[Gone] Shot/1.mp4'
        gone_source = {'path': 'Shots/[Gone] Shot/1.mkv', 'size': 1, 'modTimeUnixNano': 1}
        def item(key: str, src: dict) -> dict:
            return {'size': 123, 'modTimeUnixNano': 456, 'durationSeconds': 7, 'source': src, 'outputPath': key, 'coverPath': key.replace('.mp4', '.webp'), 'deployedAt': '2024-01-01T00:00:00+00:00', 'subtitles': []}
        metadata = {'version': 2, 'items': {kept_key: item(kept_key, source_signature(self.archive, source)), gone_key: item(gone_key, gone_source)}}
        rclone = FakeRclone(self.remote_root, {kept_key: b'v', kept_key.replace('.mp4', '.webp'): b'c', gone_key: b'v', gone_key.replace('.mp4', '.webp'): b'c', METADATA_CACHE_FILENAME: json.dumps(metadata).encode('utf-8')})
        with self._patched_deploy(rclone):
            exit_code = main(['deploy', str(self.archive), self.remote_root, '--entry', str(self.archive / 'Shots' / '[New] Shot')])
        assert exit_code == 0
        assert rclone.deleted_paths() == []
        assert set(rclone.uploaded_media_paths()) == {'Shots/[New] Shot/1.mp4', 'Shots/[New] Shot/1.webp'}
        next_metadata = json.loads(rclone.files[METADATA_CACHE_FILENAME].decode('utf-8'))
        assert set(next_metadata['items']) == {kept_key, gone_key, 'Shots/[New] Shot/1.mp4'}

    def test_rename_uploads_new_path_and_deletes_old_remote_path(self) -> None:
        old_output = 'Shots/[Creator] Old Shot/1.mp4'
        old_cover = 'Shots/[Creator] Old Shot/1.webp'
        old_metadata = {'version': 2, 'items': {old_output: {'size': 123, 'modTimeUnixNano': 456, 'durationSeconds': 7, 'source': {'path': 'Shots/[Creator] Old Shot/1.mkv', 'size': 6, 'modTimeUnixNano': 1}, 'outputPath': old_output, 'coverPath': old_cover, 'deployedAt': '2024-01-01T00:00:00+00:00', 'subtitles': []}}}
        rclone = FakeRclone(self.remote_root, {old_output: b'old video', old_cover: b'old cover', METADATA_CACHE_FILENAME: json.dumps(old_metadata).encode('utf-8')})
        with self._patched_deploy(rclone):
            exit_code = main(['deploy', str(self.archive), self.remote_root])
        assert exit_code == 0
        assert 'Shots/[Creator] Shot/1.mp4' in rclone.files
        assert old_output not in rclone.files
        assert old_cover not in rclone.files
        assert sorted(rclone.deleted_paths()) == [old_output, old_cover]
        assert 'Shots/[Creator] Old Shot' in rclone.removed_dirs()

    def test_category_limited_deploy_prunes_only_selected_category(self) -> None:
        source = self.archive / 'Shots' / '[Creator] Shot' / '1.mkv'
        output_key = 'Shots/[Creator] Shot/1.mp4'
        cover_key = 'Shots/[Creator] Shot/1.webp'
        old_output = 'Shots/[Creator] Old Shot/1.mp4'
        old_cover = 'Shots/[Creator] Old Shot/1.webp'
        song_output = 'Songs/[Singer] Song/1.mp4'
        song_cover = 'Songs/[Singer] Song/1.webp'
        metadata = {'version': 2, 'items': {output_key: {'size': 123, 'modTimeUnixNano': 456, 'durationSeconds': 7, 'source': source_signature(self.archive, source), 'outputPath': output_key, 'coverPath': cover_key, 'deployedAt': '2024-01-01T00:00:00+00:00', 'subtitles': []}, old_output: {'size': 111, 'modTimeUnixNano': 222, 'durationSeconds': 3, 'source': {'path': 'Shots/[Creator] Old Shot/1.mkv', 'size': 6, 'modTimeUnixNano': 1}, 'outputPath': old_output, 'coverPath': old_cover, 'deployedAt': '2024-01-01T00:00:00+00:00', 'subtitles': []}, song_output: {'size': 333, 'modTimeUnixNano': 444, 'durationSeconds': 9, 'source': {'path': 'Songs/[Singer] Song/1.mkv', 'size': 8, 'modTimeUnixNano': 2}, 'outputPath': song_output, 'coverPath': song_cover, 'deployedAt': '2024-01-01T00:00:00+00:00', 'subtitles': []}}}
        rclone = FakeRclone(self.remote_root, {output_key: b'remote video', cover_key: b'remote cover', old_output: b'old video', old_cover: b'old cover', song_output: b'song video', song_cover: b'song cover', METADATA_CACHE_FILENAME: json.dumps(metadata).encode('utf-8')})
        with self._patched_deploy(rclone) as convert_job:
            exit_code = main(['deploy', str(self.archive), self.remote_root, '--category', 'Shots'])
        assert exit_code == 0
        convert_job.assert_not_called()
        assert sorted(rclone.deleted_paths()) == [old_output, old_cover]
        assert 'Shots/[Creator] Old Shot' in rclone.removed_dirs()
        assert song_output in rclone.files
        assert song_cover in rclone.files
        next_metadata = json.loads(rclone.files[METADATA_CACHE_FILENAME].decode('utf-8'))
        assert set(next_metadata['items']) == {output_key, song_output}
        assert next_metadata['items'][output_key]['deployedAt'] == '2024-01-01T00:00:00+00:00'
        assert next_metadata['items'][song_output]['durationSeconds'] == 9
        assert next_metadata['items'][song_output]['outputPath'] == song_output

    def test_deploy_dry_run_logs_planned_uploads_and_deletes(self) -> None:
        old_output = 'Shots/[Creator] Old Shot/1.mp4'
        old_cover = 'Shots/[Creator] Old Shot/1.webp'
        rclone = FakeRclone(self.remote_root, {old_output: b'old video', old_cover: b'old cover'})
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self._patched_deploy(rclone) as convert_job:
                exit_code = main(['deploy', str(self.archive), self.remote_root, '--dry-run'])
        output = stdout.getvalue()
        assert exit_code == 0
        convert_job.assert_not_called()
        assert set(rclone.files) == {old_cover, old_output}
        assert not (self.stage / 'Shots' / '[Creator] Shot' / '1.mp4').exists()
        assert 'Deploy: dry run: would upload 3 file(s)' in output
        assert 'Deploy: dry run: would upload 1/3: Shots/[Creator] Shot/1.mp4' in output
        assert 'Deploy: dry run: would upload 2/3: Shots/[Creator] Shot/1.webp' in output
        assert f'Deploy: dry run: would upload 3/3: {METADATA_CACHE_FILENAME}' in output
        # The remote files are not in any catalog, so the orphan sweep would remove them.
        assert 'Deploy: dry run: would delete 2 dangling remote file(s)' in output
        assert f'would delete 1/2: {old_output}' in output
        assert f'would delete 2/2: {old_cover}' in output

    def test_interrupted_deploy_reuses_staged_output_on_next_run(self) -> None:
        rclone = FakeRclone(self.remote_root)
        with self._patched_deploy(rclone) as convert_job:
            with mock.patch('abridged_convert.app.finalize_deploy', side_effect=KeyboardInterrupt):
                exit_code = main(['deploy', str(self.archive), self.remote_root])
        assert exit_code == 130
        assert (self.stage / METADATA_CACHE_FILENAME).is_file()
        assert (self.stage / 'Shots' / '[Creator] Shot' / '1.mp4').is_file()
        assert (self.stage / 'Shots' / '[Creator] Shot' / '1.webp').is_file()
        assert convert_job.call_count == 1
        assert rclone.uploaded_media_paths() == []
        interrupted_metadata = json.loads((self.stage / METADATA_CACHE_FILENAME).read_text(encoding='utf-8'))
        interrupted_item = interrupted_metadata['items']['Shots/[Creator] Shot/1.mp4']
        assert interrupted_metadata['deployRunStatus'] == 'inProgress'
        assert interrupted_metadata['metadataUploadPending'] is True
        assert interrupted_item['uploaded'] is False
        assert json.loads(self.pointer.read_text(encoding='utf-8')) == {'remote': self.remote_root, 'staging': str(self.stage)}
        resumed_exit_code, output, resumed_convert_job = self._run_deploy(rclone)
        assert resumed_exit_code == 0
        assert 'resuming deploy run' in output
        resumed_convert_job.assert_not_called()
        assert not self.stage.exists()
        assert not self.pointer.exists()
        assert 'Shots/[Creator] Shot/1.mp4' in rclone.files
        assert 'Shots/[Creator] Shot/1.webp' in rclone.files
        assert METADATA_CACHE_FILENAME in rclone.files
        remote_metadata = json.loads(rclone.files[METADATA_CACHE_FILENAME].decode('utf-8'))
        assert 'deployRunStatus' not in remote_metadata
        assert 'metadataUploadPending' not in remote_metadata
        assert 'uploaded' not in remote_metadata['items']['Shots/[Creator] Shot/1.mp4']

    def _run_deploy(self, rclone: FakeRclone, *extra_args: str) -> tuple[int, str, mock.Mock]:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self._patched_deploy(rclone) as convert_job:
                exit_code = main(['deploy', str(self.archive), self.remote_root, *extra_args])
        return exit_code, stdout.getvalue(), convert_job

    def test_deploy_dry_run_leaves_nothing_to_resume(self) -> None:
        rclone = FakeRclone(self.remote_root)
        exit_code, _, _ = self._run_deploy(rclone, '--dry-run')
        assert exit_code == 0
        assert not self.pointer.exists()
        assert not self.stage.exists()
        exit_code, output, convert_job = self._run_deploy(rclone)
        assert exit_code == 0
        assert 'resuming deploy run' not in output
        assert f'downloading {METADATA_CACHE_FILENAME}' in output
        convert_job.assert_called_once()

    def test_deploy_dry_run_after_interruption_preserves_resume_point(self) -> None:
        rclone = FakeRclone(self.remote_root)
        with self._patched_deploy(rclone):
            with mock.patch('abridged_convert.app.finalize_deploy', side_effect=KeyboardInterrupt):
                assert main(['deploy', str(self.archive), self.remote_root]) == 130
        journal_before = (self.stage / METADATA_CACHE_FILENAME).read_bytes()
        exit_code, output, convert_job = self._run_deploy(rclone, '--dry-run')
        assert exit_code == 0
        assert 'resuming deploy run' in output
        convert_job.assert_not_called()
        assert (self.stage / METADATA_CACHE_FILENAME).read_bytes() == journal_before
        assert rclone.uploaded_media_paths() == []
        exit_code, output, convert_job = self._run_deploy(rclone)
        assert exit_code == 0
        assert 'resuming deploy run' in output
        convert_job.assert_not_called()
        assert 'Shots/[Creator] Shot/1.mp4' in rclone.files
        assert 'Shots/[Creator] Shot/1.webp' in rclone.files

    def test_successful_deploys_leave_no_staging_or_pointer(self) -> None:
        rclone = FakeRclone(self.remote_root)
        assert self._run_deploy(rclone)[0] == 0
        assert not self.stage.exists()
        assert not self.pointer.exists()
        exit_code, output, convert_job = self._run_deploy(rclone)
        assert exit_code == 0
        convert_job.assert_not_called()
        assert 'resuming deploy run' not in output
        assert f'skipping {METADATA_CACHE_FILENAME}: unchanged' in output
        assert not self.stage.exists()
        assert not self.pointer.exists()

    def test_missing_staging_after_reboot_starts_again(self) -> None:
        # A run uploaded media then died before the catalog upload; the reboot wiped its temp staging.
        self.pointer.write_text(json.dumps({'remote': self.remote_root, 'staging': str(self.root / 'abridged-deploy-gone')}), encoding='utf-8')
        rclone = FakeRclone(self.remote_root, {'Shots/[Creator] Shot/1.mp4': b'half-finished', 'Shots/[Creator] Shot/1.webp': b'half-finished'})
        exit_code, output, convert_job = self._run_deploy(rclone)
        assert exit_code == 0
        assert "previous run's staging is gone; starting again" in output
        assert 'resuming deploy run' not in output
        # The catalog never listed the half-finished upload, so it is re-converted onto the same paths.
        assert convert_job.call_count == 1
        assert rclone.files['Shots/[Creator] Shot/1.mp4'] == b'converted video'
        assert rclone.deleted_paths() == []
        metadata = json.loads(rclone.files[METADATA_CACHE_FILENAME].decode('utf-8'))
        assert set(metadata['items']) == {'Shots/[Creator] Shot/1.mp4'}
        assert not self.pointer.exists()
        assert not self.stage.exists()

    def _deployed_remote(self) -> FakeRclone:
        rclone = FakeRclone(self.remote_root)
        assert self._run_deploy(rclone)[0] == 0
        return rclone

    def test_deploy_after_crash_between_catalog_upload_and_deletes_removes_leftovers(self) -> None:
        # Leftovers of an entry the catalog already dropped: the earlier run died before deleting them.
        rclone = self._deployed_remote()
        rclone.files['Shots/[Removed] Shot/1.mp4'] = b'old'
        rclone.files['Shots/[Removed] Shot/1.webp'] = b'old'
        rclone.files['Shots/[Creator] Shot/1.1.fr.vtt'] = b'stale subtitle'
        exit_code, output, convert_job = self._run_deploy(rclone)
        assert exit_code == 0
        convert_job.assert_not_called()
        assert 'found 3 orphaned remote file(s)' in output
        assert sorted(rclone.deleted_paths()) == ['Shots/[Creator] Shot/1.1.fr.vtt', 'Shots/[Removed] Shot/1.mp4', 'Shots/[Removed] Shot/1.webp']
        assert 'Shots/[Removed] Shot' in rclone.removed_dirs()
        assert {'Shots/[Creator] Shot/1.mp4', 'Shots/[Creator] Shot/1.webp', METADATA_CACHE_FILENAME} <= set(rclone.files)

    def test_orphan_sweep_stays_inside_scope(self) -> None:
        rclone = self._deployed_remote()
        rclone.files['Shots/[Stray] Shot/1.mp4'] = b'stray'
        rclone.files['notes.txt'] = b'not catalog content'
        entry = str(self.archive / 'Shots' / '[Creator] Shot')
        exit_code, _, _ = self._run_deploy(rclone, '--entry', entry)
        assert exit_code == 0
        # An entry run only sweeps inside that entry.
        assert rclone.deleted_paths() == []
        exit_code, _, _ = self._run_deploy(rclone)
        assert exit_code == 0
        # A full run sweeps the category folders, but never other files at the remote root.
        assert rclone.deleted_paths() == ['Shots/[Stray] Shot/1.mp4']
        assert 'notes.txt' in rclone.files

    def test_orphan_sweep_refuses_when_most_of_the_remote_looks_orphaned(self) -> None:
        rclone = self._deployed_remote()
        for index in range(20):
            rclone.files[f'Shots/[Stray] {index}/1.mp4'] = b'x'
        exit_code, output, _ = self._run_deploy(rclone)
        assert exit_code == 0
        assert 'look orphaned; not deleting them' in output
        assert rclone.deleted_paths() == []

    def test_pointer_for_other_remote_is_discarded(self) -> None:
        old_staging = self.root / 'abridged-deploy-old'
        old_staging.mkdir()
        (old_staging / METADATA_CACHE_FILENAME).write_text('{}', encoding='utf-8')
        self.pointer.write_text(json.dumps({'remote': 'other:Remote', 'staging': str(old_staging)}), encoding='utf-8')
        exit_code, output, convert_job = self._run_deploy(FakeRclone(self.remote_root))
        assert exit_code == 0
        assert 'unfinished deploy to other:Remote is discarded; starting again' in output
        assert not old_staging.exists()
        assert not self.pointer.exists()

    def test_pointer_never_deletes_a_dir_the_tool_did_not_create(self) -> None:
        precious = self.root / 'precious'
        precious.mkdir()
        self.pointer.write_text(json.dumps({'remote': 'other:Remote', 'staging': str(precious)}), encoding='utf-8')
        assert self._run_deploy(FakeRclone(self.remote_root))[0] == 0
        assert precious.is_dir()

    def test_failed_deploy_keeps_pointer_and_staging_for_resume(self) -> None:
        rclone = FakeRclone(self.remote_root)
        with self._patched_deploy(rclone) as convert_job:
            convert_job.side_effect = lambda job, **_k: ConversionResult(category=job.category, entry_dir=job.entry_dir, source_video=job.source_video, output_file=job.output_file, success=False, message='boom')
            assert main(['deploy', str(self.archive), self.remote_root]) == 1
        assert self.pointer.exists()
        assert self.stage.is_dir()

    def test_invalid_remote_leaves_nothing_behind(self, capsys) -> None:
        with self._patched_deploy(FakeRclone(self.remote_root)):
            assert main(['deploy', str(self.archive), 'no-colon']) == 2
        assert 'rclone remote path' in capsys.readouterr().err
        assert not self.pointer.exists()
        assert not self.stage.exists()

    def test_remote_failure_before_staging_leaves_nothing_behind(self) -> None:
        with self._patched_deploy(FakeRclone(self.remote_root)):
            with mock.patch('abridged_convert.app.prepare_deploy_context', side_effect=ConversionError('rclone failed')):
                assert main(['deploy', str(self.archive), self.remote_root]) == 2
        assert not self.pointer.exists()
        assert not self.stage.exists()

    def test_rebuild_metadata_generates_local_metadata_and_state_without_conversion(self) -> None:
        self._write_existing_outputs()
        with mock.patch('abridged_convert.app.convert_job') as convert_job:
            with mock.patch('abridged_convert.deploy.build_metadata_cache_item', return_value={'size': 15, 'modTimeUnixNano': 20, 'durationSeconds': 1}), mock.patch('abridged_convert.deploy.extract_source_subtitles', return_value=[]):
                exit_code = main(['rebuild-metadata', str(self.archive), str(self.stage)])
        assert exit_code == 0
        convert_job.assert_not_called()
        metadata = json.loads((self.stage / METADATA_CACHE_FILENAME).read_text(encoding='utf-8'))
        assert set(metadata['items']) == {'Shots/[Creator] Shot/1.mp4'}
        assert metadata['items']['Shots/[Creator] Shot/1.mp4']['outputPath'] == 'Shots/[Creator] Shot/1.mp4'
        assert 'deploy' not in metadata
        assert (self.stage / 'Shots' / '[Creator] Shot' / '1.mp4').exists()

    def test_rebuild_metadata_uploads_existing_outputs_and_root_files(self) -> None:
        self._write_existing_outputs()
        rclone = FakeRclone(self.remote_root, {'Shots/[Creator] Old Shot/1.mp4': b'old video', 'Shots/[Creator] Old Shot/1.webp': b'old cover'})
        with self._patched_rclone(rclone):
            with mock.patch('abridged_convert.deploy.build_metadata_cache_item', return_value={'size': 15, 'modTimeUnixNano': 20, 'durationSeconds': 1}), mock.patch('abridged_convert.deploy.extract_source_subtitles', return_value=[]):
                exit_code = main(['rebuild-metadata', str(self.archive), str(self.stage), '--upload', self.remote_root])
        assert exit_code == 0
        # Recovery never deletes OUTPUT: it is the only copy of the converted files.
        assert (self.stage / 'Shots' / '[Creator] Shot' / '1.mp4').exists()
        assert 'Shots/[Creator] Shot/1.mp4' in rclone.files
        assert 'Shots/[Creator] Shot/1.webp' in rclone.files
        assert METADATA_CACHE_FILENAME in rclone.files
        assert sorted(rclone.deleted_paths()) == ['Shots/[Creator] Old Shot/1.mp4', 'Shots/[Creator] Old Shot/1.webp']

    def _run_rebuild_metadata(self, rclone: FakeRclone) -> int:
        with self._patched_rclone(rclone):
            with mock.patch('abridged_convert.deploy.build_metadata_cache_item', return_value={'size': 15, 'modTimeUnixNano': 20, 'durationSeconds': 1}), mock.patch('abridged_convert.deploy.extract_source_subtitles', return_value=[]):
                return main(['rebuild-metadata', str(self.archive), str(self.stage), '--upload', self.remote_root])

    def test_rebuild_metadata_uses_existing_webm_output(self) -> None:
        output_dir = self.stage / 'Shots' / '[Creator] Shot'
        output_dir.mkdir(parents=True)
        (output_dir / '1.webm').write_bytes(b'existing webm')
        (output_dir / '1.webp').write_bytes(b'existing cover')
        rclone = FakeRclone(self.remote_root)
        assert self._run_rebuild_metadata(rclone) == 0
        assert rclone.files['Shots/[Creator] Shot/1.webm'] == b'existing webm'
        assert 'Shots/[Creator] Shot/1.mp4' not in rclone.files
        metadata = json.loads((self.stage / METADATA_CACHE_FILENAME).read_text(encoding='utf-8'))
        assert set(metadata['items']) == {'Shots/[Creator] Shot/1.webm'}
        assert metadata['items']['Shots/[Creator] Shot/1.webm']['outputPath'] == 'Shots/[Creator] Shot/1.webm'
        assert metadata['items']['Shots/[Creator] Shot/1.webm']['coverPath'] == 'Shots/[Creator] Shot/1.webp'

    def test_rebuild_metadata_dry_run_does_not_report_existing_subtitles_as_deletes(self) -> None:
        self._write_existing_outputs()
        rclone = FakeRclone(self.remote_root, {
            'Shots/[Creator] Shot/1.mp4': b'video',
            'Shots/[Creator] Shot/1.webp': b'cover',
            'Shots/[Creator] Shot/1.0.en.vtt': b'WEBVTT',
            'Shots/[Creator] Old Shot/1.0.en.vtt': b'WEBVTT',
        })
        stdout = io.StringIO()
        with redirect_stdout(stdout), self._patched_rclone(rclone):
            exit_code = main(['rebuild-metadata', str(self.archive), str(self.stage), '--upload', self.remote_root, '--dry-run'])
        output = stdout.getvalue()
        assert exit_code == 0
        assert 'Deploy: dry run: would delete 1 dangling remote file(s)' in output
        assert 'Deploy: dry run: would delete 1/1: Shots/[Creator] Old Shot/1.0.en.vtt' in output

    def test_rebuild_metadata_rejects_ambiguous_mp4_and_webm_outputs(self, capsys) -> None:
        self._write_existing_outputs()
        (self.stage / 'Shots' / '[Creator] Shot' / '1.webm').write_bytes(b'existing webm')
        rclone = FakeRclone(self.remote_root)
        assert self._run_rebuild_metadata(rclone) == 2
        assert 'both 1.mp4 and 1.webm' in capsys.readouterr().err
        assert rclone.uploaded_media_paths() == []

    def test_deploy_uploads_subtitle_sidecars_and_lists_them(self) -> None:
        self.fake_subtitle_languages = ['en', 'es']
        rclone = FakeRclone(self.remote_root)
        with self._patched_deploy(rclone):
            exit_code = main(['deploy', str(self.archive), self.remote_root])
        assert exit_code == 0
        assert 'Shots/[Creator] Shot/1.0.en.vtt' in rclone.files
        assert 'Shots/[Creator] Shot/1.1.es.vtt' in rclone.files
        item = json.loads(rclone.files[METADATA_CACHE_FILENAME].decode('utf-8'))['items']['Shots/[Creator] Shot/1.mp4']
        assert [entry['path'] for entry in item['subtitles']] == ['Shots/[Creator] Shot/1.0.en.vtt', 'Shots/[Creator] Shot/1.1.es.vtt']
        assert item['subtitles'][0]['default'] is True

    def test_deploy_backfills_subtitles_without_reuploading_video(self) -> None:
        self.fake_subtitle_languages = ['en']
        source = self.archive / 'Shots' / '[Creator] Shot' / '1.mkv'
        output_key = 'Shots/[Creator] Shot/1.mp4'
        cover_key = 'Shots/[Creator] Shot/1.webp'
        metadata = {'version': 2, 'items': {output_key: {'size': 123, 'modTimeUnixNano': 456, 'durationSeconds': 7, 'source': source_signature(self.archive, source), 'outputPath': output_key, 'coverPath': cover_key, 'deployedAt': '2024-01-01T00:00:00+00:00'}}}
        rclone = FakeRclone(self.remote_root, {output_key: b'remote video', cover_key: b'remote cover', METADATA_CACHE_FILENAME: json.dumps(metadata).encode('utf-8')})
        with self._patched_deploy(rclone) as convert_job:
            exit_code = main(['deploy', str(self.archive), self.remote_root])
        assert exit_code == 0
        convert_job.assert_not_called()
        assert rclone.uploaded_media_paths() == ['Shots/[Creator] Shot/1.0.en.vtt']
        assert rclone.files[output_key] == b'remote video'
        item = json.loads(rclone.files[METADATA_CACHE_FILENAME].decode('utf-8'))['items'][output_key]
        assert item['subtitles'][0]['path'] == 'Shots/[Creator] Shot/1.0.en.vtt'
        assert item['deployedAt'] == '2024-01-01T00:00:00+00:00'
        assert item['size'] == 123
        with self._patched_deploy(rclone) as second_convert_job:
            assert main(['deploy', str(self.archive), self.remote_root]) == 0
        second_convert_job.assert_not_called()
        self.extract_source_subtitles.assert_not_called()

    def test_pending_upload_progress_counts_items_not_files(self) -> None:
        self.fake_subtitle_languages = ['en', 'es']
        second_entry = self.archive / 'Shots' / '[Creator] Second Shot'
        second_entry.mkdir()
        (second_entry / '1.mkv').write_text('second source', encoding='utf-8')
        exit_code, output, _ = self._run_deploy(FakeRclone(self.remote_root))
        assert exit_code == 0
        progress = [line for line in output.splitlines() if 'uploading pending item' in line]
        assert progress == [
            'Deploy: uploading pending item 1/2: Shots/[Creator] Second Shot/1.mp4',
            'Deploy: uploading pending item 2/2: Shots/[Creator] Shot/1.mp4',
        ]

    def test_redeploy_with_fewer_subtitles_deletes_stale_remote_sidecar(self) -> None:
        self.fake_subtitle_languages = ['en', 'es']
        rclone = FakeRclone(self.remote_root)
        with self._patched_deploy(rclone):
            assert main(['deploy', str(self.archive), self.remote_root]) == 0
        self.fake_subtitle_languages = ['en']
        with self._patched_deploy(rclone):
            assert main(['deploy', str(self.archive), self.remote_root, '--overwrite']) == 0
        assert 'Shots/[Creator] Shot/1.0.en.vtt' in rclone.files
        assert 'Shots/[Creator] Shot/1.1.es.vtt' not in rclone.files

    def test_interrupted_redeploy_still_deletes_stale_remote_sidecar_on_resume(self) -> None:
        self.fake_subtitle_languages = ['en', 'es']
        rclone = FakeRclone(self.remote_root)
        assert self._run_deploy(rclone)[0] == 0
        self.fake_subtitle_languages = ['en']
        with self._patched_deploy(rclone):
            with mock.patch('abridged_convert.app.finalize_deploy', side_effect=KeyboardInterrupt):
                assert main(['deploy', str(self.archive), self.remote_root, '--overwrite']) == 130
        assert 'Shots/[Creator] Shot/1.1.es.vtt' in rclone.files
        exit_code, output, convert_job = self._run_deploy(rclone)
        assert exit_code == 0
        assert 'resuming deploy run' in output
        convert_job.assert_not_called()
        assert 'Shots/[Creator] Shot/1.0.en.vtt' in rclone.files
        assert 'Shots/[Creator] Shot/1.1.es.vtt' not in rclone.files
        remote_metadata = json.loads(rclone.files[METADATA_CACHE_FILENAME].decode('utf-8'))
        assert 'staleRemotePaths' not in remote_metadata

    def test_pruned_item_deletes_its_remote_subtitles(self) -> None:
        old_output = 'Shots/[Creator] Old Shot/1.mp4'
        old_cover = 'Shots/[Creator] Old Shot/1.webp'
        old_sub = 'Shots/[Creator] Old Shot/1.0.en.vtt'
        old_metadata = {'version': 2, 'items': {old_output: {'size': 123, 'modTimeUnixNano': 456, 'durationSeconds': 7, 'source': {'path': 'Shots/[Creator] Old Shot/1.mkv', 'size': 6, 'modTimeUnixNano': 1}, 'outputPath': old_output, 'coverPath': old_cover, 'subtitles': [{'path': old_sub, 'language': 'en', 'label': 'en', 'default': True, 'forced': False}]}}}
        rclone = FakeRclone(self.remote_root, {old_output: b'old video', old_cover: b'old cover', old_sub: b'WEBVTT', METADATA_CACHE_FILENAME: json.dumps(old_metadata).encode('utf-8')})
        with self._patched_deploy(rclone):
            assert main(['deploy', str(self.archive), self.remote_root]) == 0
        assert old_sub not in rclone.files
        assert old_sub in rclone.deleted_paths()

    def _changed_source_remote(self, subtitles: list[str]) -> FakeRclone:
        output = 'Shots/[Creator] Shot/1.mp4'
        cover = 'Shots/[Creator] Shot/1.webp'
        files = {output: b'old video', cover: b'old cover'}
        subtitle_entries = []
        for path in subtitles:
            files[path] = b'WEBVTT'
            subtitle_entries.append({'path': path, 'language': 'en', 'label': 'en', 'default': True, 'forced': False})
        # Same source path as the archive, but a different size: the source was replaced.
        metadata = {'version': 2, 'items': {output: {'size': 1, 'modTimeUnixNano': 1, 'source': {'path': 'Shots/[Creator] Shot/1.mkv', 'size': 999, 'modTimeUnixNano': 1}, 'outputPath': output, 'coverPath': cover, 'subtitles': subtitle_entries}}}
        files[METADATA_CACHE_FILENAME] = json.dumps(metadata).encode('utf-8')
        return FakeRclone(self.remote_root, files)

    def test_changed_source_at_same_path_keeps_reuploaded_files(self) -> None:
        rclone = self._changed_source_remote([])
        with self._patched_deploy(rclone) as convert_job:
            assert main(['deploy', str(self.archive), self.remote_root]) == 0
        convert_job.assert_called_once()
        assert rclone.files['Shots/[Creator] Shot/1.mp4'] == b'converted video'
        assert rclone.files['Shots/[Creator] Shot/1.webp'] == b'converted cover'
        assert rclone.deleted_paths() == []
        assert rclone.removed_dirs() == []

    def test_changed_source_with_fewer_subtitles_deletes_only_dropped_sidecar(self) -> None:
        self.fake_subtitle_languages = ['en']
        rclone = self._changed_source_remote(['Shots/[Creator] Shot/1.0.en.vtt', 'Shots/[Creator] Shot/1.1.es.vtt'])
        with self._patched_deploy(rclone):
            assert main(['deploy', str(self.archive), self.remote_root]) == 0
        assert 'Shots/[Creator] Shot/1.mp4' in rclone.files
        assert 'Shots/[Creator] Shot/1.webp' in rclone.files
        assert 'Shots/[Creator] Shot/1.0.en.vtt' in rclone.files
        assert rclone.deleted_paths() == ['Shots/[Creator] Shot/1.1.es.vtt']
        assert rclone.removed_dirs() == []

    def test_changed_source_dry_run_does_not_report_reuploaded_files_as_deletes(self) -> None:
        rclone = self._changed_source_remote([])
        output = io.StringIO()
        with redirect_stdout(output):
            with self._patched_deploy(rclone):
                assert main(['deploy', str(self.archive), self.remote_root, '--dry-run']) == 0
        assert 'Deploy: dry run: would delete 0 dangling remote file(s)' in output.getvalue()

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
            subtitles = self._write_fake_subtitles(job.output_file)
            return ConversionResult(category=job.category, entry_dir=job.entry_dir, source_video=job.source_video, output_file=job.output_file, success=True, cover_source='external:cover.png', subtitles=subtitles, message='converted')
        convert_job = mock.Mock(side_effect=fake_convert_job)
        with ExitStack() as stack:
            stack.enter_context(mock.patch('abridged_convert.app.convert_job', convert_job))
            stack.enter_context(mock.patch('abridged_convert.app.resolve_video_handling_plan', return_value=('.mp4', VideoEncodingConfig(mode='cpu', codec='libx264', extra_args=()))))
            stack.enter_context(self._patched_rclone(rclone))
            stack.enter_context(mock.patch('abridged_convert.app.new_staging_dir', side_effect=self._new_staging_dir))
            # A fresh dict per call, like the real function; a shared return_value would alias every item.
            stack.enter_context(mock.patch('abridged_convert.deploy.build_metadata_cache_item', side_effect=lambda *_args, **_kwargs: {'size': 15, 'modTimeUnixNano': 20, 'durationSeconds': 1}))
            stack.enter_context(mock.patch('abridged_convert.metadata.probe_video_metadata', return_value={'durationSeconds': 1}))
            self.extract_source_subtitles = stack.enter_context(mock.patch('abridged_convert.deploy.extract_source_subtitles', side_effect=self._fake_extract_source_subtitles))
            yield convert_job

    def _new_staging_dir(self) -> Path:
        self.stage.mkdir(parents=True)
        return self.stage

    @contextmanager
    def _patched_rclone(self, rclone: FakeRclone) -> Iterator[None]:
        with ExitStack() as stack:
            stack.enter_context(mock.patch('abridged_convert.deploy.run_rclone', side_effect=rclone))
            stack.enter_context(mock.patch('abridged_convert.deploy.run_rclone_progress', side_effect=rclone))
            yield

    def _write_fake_subtitles(self, output_file: Path) -> list[dict]:
        sidecars = []
        for position, language in enumerate(self.fake_subtitle_languages):
            path = output_file.with_name(f'{output_file.stem}.{position}.{language}.vtt')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('WEBVTT\n', encoding='utf-8')
            sidecars.append({'path': path, 'language': language, 'label': language, 'default': position == 0, 'forced': False})
        return sidecars

    def _fake_extract_source_subtitles(self, _ffmpeg_bin, _ffprobe_bin, _source_video, output_file):
        return self._write_fake_subtitles(output_file)

    def _write_existing_outputs(self) -> None:
        output_dir = self.stage / 'Shots' / '[Creator] Shot'
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / '1.mp4').write_bytes(b'existing video')
        (output_dir / '1.webp').write_bytes(b'existing cover')
