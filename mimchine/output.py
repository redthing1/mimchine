from rich.console import Console
from rich.table import Table

from .services import MachineView

_stdout = Console()
_stderr = Console(stderr=True)


def print_version(name: str, version: str) -> None:
    _stdout.print(f"{name} v{version}", markup=False, highlight=False)


def print_machine_list(rows: list[MachineView]) -> None:
    if len(rows) == 0:
        _stdout.print("no mimchines found", markup=False, highlight=False)
        return

    table = Table(title=f"mimchines ({len(rows)})")
    table.add_column("name", style="cyan")
    table.add_column("runner")
    table.add_column("state", style="green")
    table.add_column("image")

    for view in rows:
        table.add_row(
            view.record.name,
            view.record.runner,
            view.status.state.value,
            view.record.image.display(),
        )

    _stdout.print(table)


def print_key_value_table(title: str, rows: list[tuple[str, str]]) -> None:
    table = Table(title=title)
    table.add_column("field", style="cyan")
    table.add_column("value", style="green")

    for key, value in rows:
        table.add_row(key, value)

    _stdout.print(table)


def print_table(title: str, columns: list[str], rows: list[tuple[str, ...]]) -> None:
    if len(rows) == 0:
        _stdout.print(f"{title}: none", markup=False, highlight=False)
        return

    table = Table(title=title)
    for column in columns:
        table.add_column(column)

    for row in rows:
        table.add_row(*row)

    _stdout.print(table)
