from rich.console import Console
from rich.table import Table
from rich.text import Text

from .services import MachineSummary

_stdout = Console()
_stderr = Console(stderr=True)


def print_version(name: str, version: str) -> None:
    _stdout.print(f"{name} v{version}", markup=False, highlight=False)


def print_machine_list(rows: list[MachineSummary]) -> None:
    if len(rows) == 0:
        _stdout.print("no mimchines found", markup=False, highlight=False)
        return

    table = Table(title=f"mimchines ({len(rows)})")
    table.add_column("name", style="cyan")
    table.add_column("runner")
    table.add_column("isolation")
    table.add_column("state", style="green")
    table.add_column("operation")
    table.add_column("image")
    table.add_column("problem")

    for summary in rows:
        view = summary.view
        if view is None:
            table.add_row(
                summary.name, "", "", "unknown", "", "", summary.problem or ""
            )
            continue
        table.add_row(
            view.machine.name,
            view.machine.spec.engine,
            view.machine.spec.isolation.value,
            view.state.value,
            view.machine.operation.value,
            view.machine.spec.image,
            view.problem or "",
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
        table.add_row(*(Text(value) for value in row))

    _stdout.print(table)
