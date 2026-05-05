from __future__ import annotations

import pytest

from mimchine.process import ProcessError, ProcessRunner


def test_process_runner_captures_output() -> None:
    result = ProcessRunner().run(
        ["python", "-c", "print('ok')"],
        capture=True,
    )

    assert result.returncode == 0
    assert result.stdout == "ok\n"


def test_process_runner_reports_missing_command() -> None:
    with pytest.raises(ProcessError) as exc:
        ProcessRunner().run(["definitely-not-a-real-mimchine-command"])

    assert exc.value.result.returncode == 127
    assert "command not found" in exc.value.result.stderr
