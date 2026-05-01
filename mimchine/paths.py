import os


def normalize_host_path(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))
