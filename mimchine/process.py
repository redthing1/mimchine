from __future__ import annotations

import subprocess
from dataclasses import dataclass
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
        check: bool = True,
    ) -> ProcessResult:
        command = tuple(str(arg) for arg in args)
        if not command:
            raise ValueError("command cannot be empty")

        try:
            if foreground:
                returncode = subprocess.call(command)
                result = ProcessResult(command, returncode)
            else:
                completed = subprocess.run(
                    command,
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE if capture else None,
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
