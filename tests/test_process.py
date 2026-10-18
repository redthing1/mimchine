from __future__ import annotations

import sys

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


def test_process_runner_can_discard_only_stdout(capfd) -> None:
    ProcessRunner().run(
        [
            "python",
            "-c",
            "import sys; print('hidden'); print('visible', file=sys.stderr)",
        ],
        foreground=True,
        discard_stdout=True,
    )

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == "visible\n"


def test_process_runner_rejects_capture_with_discard_stdout() -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        ProcessRunner().run(["python", "-c", "pass"], capture=True, discard_stdout=True)


@pytest.mark.parametrize("foreground", [False, True])
def test_process_runner_bounds_a_hung_command(foreground):
    with pytest.raises(ProcessError) as error:
        ProcessRunner().run(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            capture=not foreground,
            foreground=foreground,
            timeout=0.05,
        )
    assert error.value.result.returncode == 124
    assert "timed out" in error.value.result.stderr


def test_process_runner_returns_timeout_for_a_diagnostic_query():
    result = ProcessRunner().run(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        capture=True,
        check=False,
        timeout=0.05,
    )
    assert result.returncode == 124 and "timed out" in result.stderr
