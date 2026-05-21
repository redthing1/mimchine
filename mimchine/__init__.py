from pathlib import Path
from importlib.metadata import PackageNotFoundError, version
import tomllib

from .constants import APP_NAME


def _read_version() -> str:
    try:
        return version(APP_NAME)
    except PackageNotFoundError:
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        return str(data["project"]["version"])


__VERSION__ = _read_version()
