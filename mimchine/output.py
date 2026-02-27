from rich.console import Console
from rich.table import Table

_stdout = Console()
_stderr = Console(stderr=True)


def print_version(name: str, version: str) -> None:
    _stdout.print(f"{name} v{version}", markup=False, highlight=False)


def stream_stdout(line: str) -> None:
    _stdout.print(f"  {line}", end="", markup=False, highlight=False)


def stream_stderr(line: str) -> None:
    _stderr.print(f"  {line}", end="", markup=False, highlight=False)


def print_container_list(rows: list[tuple[str, str]]) -> None:
    if len(rows) == 0:
        _stdout.print("no mim containers found", markup=False, highlight=False)
        return

    table = Table(title=f"mim containers ({len(rows)})")
    table.add_column("name", style="cyan")
    table.add_column("state", style="green")

    for name, state in rows:
        table.add_row(name, state)

    _stdout.print(table)
