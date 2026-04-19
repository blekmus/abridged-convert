from __future__ import annotations

import atexit
import os
import re
import shutil
from pathlib import Path
from typing import TextIO

from abridged_convert.discovery import compact_label, relative_label
from abridged_convert.models import ConversionResult, Job, JobState, VideoEncodingConfig


def summarize_result(result: ConversionResult, repo_root: Path) -> str:
    status = "OK"
    if result.skipped:
        status = "SKIP"
    elif not result.success:
        status = "FAIL"

    details = [f"[{status}] {relative_label(repo_root, result)}"]
    if result.cover_source and result.cover_source != "none":
        details.append(f"cover={result.cover_source}")
    if result.mapped_tags:
        details.append("tags=" + ",".join(result.mapped_tags))
    if result.message:
        details.append(result.message)
    return " | ".join(details)


def encoding_label(encoding: VideoEncodingConfig) -> str:
    return "GPU / NVIDIA NVENC" if encoding.mode == "nvidia" else "CPU / libx264"


class TerminalUI:
    ANSI_RESET = "\x1b[0m"
    ANSI_BOLD = "\x1b[1m"
    ANSI_DIM = "\x1b[2m"
    ANSI_BLACK = "\x1b[30m"
    ANSI_RED = "\x1b[31m"
    ANSI_GREEN = "\x1b[32m"
    ANSI_YELLOW = "\x1b[33m"
    ANSI_BLUE = "\x1b[34m"
    ANSI_MAGENTA = "\x1b[35m"
    ANSI_CYAN = "\x1b[36m"
    ANSI_WHITE = "\x1b[37m"
    ANSI_BRIGHT_BLACK = "\x1b[90m"
    ANSI_BRIGHT_MAGENTA = "\x1b[95m"
    ANSI_BG_MAGENTA = "\x1b[45m"
    ANSI_BG_BLUE = "\x1b[44m"
    ANSI_BG_CYAN = "\x1b[46m"
    ANSI_BG_GREEN = "\x1b[42m"
    ANSI_BG_YELLOW = "\x1b[43m"
    ANSI_BG_RED = "\x1b[41m"
    ANSI_CLEAR = "\x1b[2J\x1b[H"
    ANSI_HIDE_CURSOR = "\x1b[?25l"
    ANSI_SHOW_CURSOR = "\x1b[?25h"
    ANSI_ALT_SCREEN = "\x1b[?1049h"
    ANSI_EXIT_ALT_SCREEN = "\x1b[?1049l"

    def __init__(
        self,
        *,
        repo_root: Path,
        categories: list[str],
        encoding: VideoEncodingConfig,
        jobs: list[Job],
        discovery_failures: list[ConversionResult],
        persist_final_report: bool,
        stream: TextIO,
    ) -> None:
        self.repo_root = repo_root
        self.categories = categories
        self.encoding = encoding
        self.persist_final_report = persist_final_report
        self.stream = stream
        self.enabled = bool(getattr(stream, "isatty", lambda: False)()) and os.environ.get("TERM") != "dumb"
        self.phase = "Preparing"
        self.current_detail = "Discovering work"
        self.final_message = ""
        self._active = False
        self.completed = 0
        self.failed = len(discovery_failures)
        self.skipped = 0
        self.states: list[JobState] = [
            JobState(
                label=compact_label(failure),
                detail=failure.message or "failed before conversion",
                status="failed",
                message=failure.message,
            )
            for failure in discovery_failures
        ]
        self.job_index_map: dict[tuple[str, str], int] = {}
        for job in jobs:
            index = len(self.states)
            self.job_index_map[(str(job.source_video), str(job.output_file))] = index
            self.states.append(JobState(label=compact_label(job), detail="queued"))

    def start(self) -> None:
        if not self.enabled:
            self._print_start_message()
            return
        if self._active:
            return
        self._active = True
        atexit.register(self.close)
        self.stream.write(self.ANSI_ALT_SCREEN + self.ANSI_HIDE_CURSOR)
        self.stream.flush()
        self.render()

    def finish(self, exit_code: int) -> None:
        self.phase = "Finished"
        self.current_detail = self.final_message or "Done"
        if not self.enabled:
            return
        self.render()
        self.close()
        if self.persist_final_report:
            self._print_persistent_report(exit_code)

    def close(self) -> None:
        if not self.enabled or not self._active:
            return
        self._active = False
        self.stream.write(self.ANSI_RESET + self.ANSI_SHOW_CURSOR + self.ANSI_EXIT_ALT_SCREEN)
        self.stream.flush()
        try:
            atexit.unregister(self.close)
        except ValueError:
            pass

    def _print_start_message(self) -> None:
        self.stream.write(
            f"Starting abridged-convert | encoder={encoding_label(self.encoding)}"
            f" | categories={','.join(self.categories)} | items={len(self.states)}\n"
        )
        self.stream.flush()

    def set_phase(self, phase: str, detail: str) -> None:
        self.phase = phase
        self.current_detail = detail
        self.render()

    def mark_running(self, job: Job) -> None:
        state = self._state_for_job(job)
        state.status = "running"
        state.detail = "working"
        self.phase = "Converting"
        self.current_detail = state.label
        self.render()

    def mark_result(self, job: Job, result: ConversionResult) -> None:
        state = self._state_for_job(job)
        if result.success and result.skipped:
            state.status = "skipped"
            state.detail = result.message or "skipped"
            self.skipped += 1
        elif result.success:
            state.status = "done"
            state.detail = result.message or "done"
            self.completed += 1
        else:
            state.status = "failed"
            state.detail = result.message or "failed"
            self.failed += 1
        state.message = self._result_suffix(result)
        self.current_detail = state.label
        self.render()

    def set_final_summary(self, *, converted: int, skipped: int, failed: int) -> None:
        self.final_message = f"converted={converted} skipped={skipped} failed={failed}"
        self.completed = converted
        self.skipped = skipped
        self.failed = failed
        self.render()

    def print_result_line(self, result: ConversionResult) -> None:
        if self.enabled:
            return
        self.stream.write(summarize_result(result, self.repo_root) + "\n")
        self.stream.flush()

    def render(self) -> None:
        if not self.enabled or not self._active:
            return
        frame = self._build_frame()
        self.stream.write(self.ANSI_CLEAR + frame)
        self.stream.flush()

    def _build_frame(self) -> str:
        lines = [
            self._pill("MODE", encoding_label(self.encoding), self.ANSI_BG_MAGENTA),
            " ".join(
                [
                    self._pill("DONE", str(self.completed), self.ANSI_BG_GREEN),
                    self._pill("SKIP", str(self.skipped), self.ANSI_BG_YELLOW),
                    self._pill("FAIL", str(self.failed), self.ANSI_BG_RED),
                    self._pill("LEFT", str(sum(1 for state in self.states if state.status == "pending")), self.ANSI_BG_BLUE),
                ]
            ),
            f"{self.ANSI_BRIGHT_BLACK}root{self.ANSI_RESET} {self.repo_root}",
            "",
            f"{self.ANSI_BOLD}Queue{self.ANSI_RESET}",
            *self._queue_lines(),
            "",
        ]
        return "\n".join(lines) + "\n"

    def _queue_lines(self) -> list[str]:
        height = shutil.get_terminal_size((120, 40)).lines
        max_queue_lines = max(8, height - 8)
        visible_states = [state for state in self.states if state.status != "pending"][-max_queue_lines:]
        extra = max(0, len([state for state in self.states if state.status != "pending"]) - len(visible_states))
        lines = [self._format_state_line(state) for state in visible_states]
        if not lines:
            lines.append(f"{self.ANSI_DIM}Waiting for the first file to start…{self.ANSI_RESET}")
        if extra > 0:
            lines.append(f"{self.ANSI_DIM}… {extra} more item(s) below{self.ANSI_RESET}")
        return lines

    def _format_state_line(self, state: JobState) -> str:
        label = self._highlight_creator(state.label)
        if state.status == "running":
            return (
                f"{self._status_badge('▶', 'working', self.ANSI_BRIGHT_MAGENTA)} "
                f"{self.ANSI_WHITE}{label}{self.ANSI_RESET} "
                f"{self.ANSI_DIM}{state.detail}{self.ANSI_RESET}"
            )
        if state.status == "done":
            suffix = f" {self.ANSI_DIM}{state.message}{self.ANSI_RESET}" if state.message else ""
            return f"{self._status_badge('✓', 'done', self.ANSI_CYAN)} {label}{suffix}"
        if state.status == "skipped":
            suffix = f" {self.ANSI_DIM}{state.detail}{self.ANSI_RESET}" if state.detail else ""
            return f"{self._status_badge('↷', 'skipped', self.ANSI_BLUE)} {label}{suffix}"
        if state.status == "failed":
            suffix = f" {self.ANSI_DIM}{state.detail}{self.ANSI_RESET}" if state.detail else ""
            return f"{self._status_badge('✗', 'failed', self.ANSI_RED)} {label}{suffix}"
        return ""

    def _state_for_job(self, job: Job) -> JobState:
        return self.states[self.job_index_map[(str(job.source_video), str(job.output_file))]]

    def _result_suffix(self, result: ConversionResult) -> str:
        details: list[str] = []
        if result.cover_source and result.cover_source != "none":
            details.append(result.cover_source)
        if result.message and result.message not in {"converted", "dry run"}:
            details.append(result.message)
        return " | ".join(details)

    def _pill(self, label: str, value: str, bg: str) -> str:
        return f"{bg}{self.ANSI_BLACK} {label} {self.ANSI_RESET}{self.ANSI_BRIGHT_BLACK} {value} {self.ANSI_RESET}"

    def _status_badge(self, icon: str, label: str, color: str) -> str:
        return f"{color}{icon} {label}{self.ANSI_RESET}"

    def _highlight_creator(self, label: str) -> str:
        return re.sub(
            r"\[[^\]]+\]",
            lambda match: f"{self.ANSI_BRIGHT_MAGENTA}{match.group(0)}{self.ANSI_RESET}{self.ANSI_WHITE}",
            label,
            count=1,
        )

    def _plain_status_label(self, state: JobState) -> str:
        if state.status == "running":
            return "RUN"
        if state.status == "done":
            return "OK"
        if state.status == "skipped":
            return "SKIP"
        if state.status == "failed":
            return "FAIL"
        return "PEND"

    def _plain_state_line(self, state: JobState) -> str:
        details = [f"[{self._plain_status_label(state)}] {state.label}"]
        if state.detail and state.detail != "queued":
            details.append(state.detail)
        if state.message:
            details.append(state.message)
        return " | ".join(details)

    def _print_persistent_report(self, exit_code: int) -> None:
        lines = [
            "Dry-run queue summary",
            f"Result: {'FAIL' if exit_code else 'OK'}",
            f"Encoder: {encoding_label(self.encoding)}",
            f"Categories: {','.join(self.categories)}",
            f"Summary: {self.final_message or 'converted=0 skipped=0 failed=0'}",
            "",
        ]
        lines.extend(self._plain_state_line(state) for state in self.states)
        self.stream.write("\n".join(lines) + "\n")
        self.stream.flush()
