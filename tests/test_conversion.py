import subprocess
from pathlib import Path
from unittest import mock

from abridged_convert.conversion import convert_job, resume_interrupted_job, tracking_file_for_job
from abridged_convert.media import nvidia_video_encoding_config
from abridged_convert.models import EntryMetadata, Job


class ConversionRecoveryTests:
    def test_marker_does_not_remove_another_episode(self, tmp_path: Path) -> None:
        first = Job('Series', tmp_path, tmp_path / 'a.mkv', tmp_path / 'Episode 1.mp4')
        second = Job('Series', tmp_path, tmp_path / 'b.mkv', tmp_path / 'Episode_1.mp4')
        first.output_file.write_bytes(b'partial')
        second.output_file.write_bytes(b'complete')
        tracking_file_for_job(first).write_text(f'{first.output_file}\n', encoding='utf-8')
        assert not resume_interrupted_job(second, dry_run=False)
        assert second.output_file.read_bytes() == b'complete'
        assert resume_interrupted_job(first, dry_run=False)
        assert not first.output_file.exists()

    def test_legacy_marker_only_resumes_its_recorded_output(self, tmp_path: Path) -> None:
        first = Job('Series', tmp_path, tmp_path / 'a.mkv', tmp_path / 'Episode 1.mp4')
        second = Job('Series', tmp_path, tmp_path / 'b.mkv', tmp_path / 'Episode_1.mp4')
        legacy = tmp_path / '.abridged-convert.Episode_1.incomplete'
        legacy.write_text(f'{first.output_file}\n', encoding='utf-8')
        first.output_file.write_bytes(b'partial')
        assert not resume_interrupted_job(second, dry_run=False)
        assert resume_interrupted_job(first, dry_run=True)
        assert legacy.exists()
        assert first.output_file.exists()
        assert resume_interrupted_job(first, dry_run=False)
        assert not legacy.exists()
        assert not first.output_file.exists()


class GpuFallbackTests:
    def test_failed_cpu_fallback_reports_the_cpu_error(self, tmp_path: Path) -> None:
        job = Job('Shots', tmp_path, tmp_path / '1.mkv', tmp_path / 'out' / '1.mp4')
        gpu_error = subprocess.CalledProcessError(1, 'ffmpeg', stderr='Cannot load libcuda.so')
        cpu_error = subprocess.CalledProcessError(1, 'ffmpeg', stderr='cpu encoder exploded')
        with mock.patch('abridged_convert.conversion.collect_metadata', return_value=EntryMetadata(video_index=0)), \
                mock.patch('abridged_convert.conversion.choose_cover', return_value=(None, 'none')), \
                mock.patch('abridged_convert.conversion.run_command', side_effect=[gpu_error, cpu_error]):
            result = convert_job(job, ffmpeg_bin='ffmpeg', ffprobe_bin='ffprobe', encoding=nvidia_video_encoding_config(), overwrite=False, dry_run=False)
        assert not result.success
        assert result.message == 'cpu encoder exploded'
        assert not tracking_file_for_job(job).exists()
