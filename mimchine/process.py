from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class ProcessResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


class ProcessError(RuntimeError):
    def __init__(self, result: ProcessResult):
        self.result = result
        super().__init__(f"command failed with exit code {result.returncode}: {result.args[0]}")


class ProcessRunner:
    def run(
        self,
        args: Sequence[str],
        *,
        capture: bool = False,
        foreground: bool = False,
        discard_stdout: bool = False,
        check: bool = True,
        cwd: str | Path | None = None,
    ) -> ProcessResult:
        command = tuple(str(arg) for arg in args)
        if not command:
            raise ValueError("command cannot be empty")
        cwd_text = None if cwd is None else str(cwd)
        if capture and discard_stdout:
            raise ValueError("capture and discard_stdout cannot be combined")

        try:
            if foreground:
                returncode = subprocess.call(
                    command,
                    cwd=cwd_text,
                    stdout=subprocess.DEVNULL if discard_stdout else None,
                )
                result = ProcessResult(command, returncode)
            else:
                completed = subprocess.run(
                    command,
                    check=False,
                    text=True,
                    cwd=cwd_text,
                    stdout=(
                        subprocess.PIPE
                        if capture
                        else subprocess.DEVNULL
                        if discard_stdout
                        else None
                    ),
                    stderr=subprocess.PIPE if capture else None,
                )
                result = ProcessResult(
                    command,
                    completed.returncode,
                    completed.stdout or "",
                    completed.stderr or "",
                )
        except FileNotFoundError:
            result = ProcessResult(
                command,
                127,
                stderr=f"command not found: {command[0]}",
            )

        if check and result.returncode != 0:
            raise ProcessError(result)

        return result
