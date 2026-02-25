import sh
import json
from minlog import logger

from .config import get_container_runtime


def parse_container_json(json_output):
    """parse json output that could be either a json array or jsonl format."""
    if not json_output.strip():
        return []

    try:
        # first try parsing as a single json document (could be an array)
        result = json.loads(json_output)
        # if it's not a list, wrap it in one
        return result if isinstance(result, list) else [result]
    except json.JSONDecodeError:
        # if that fails, try parsing as jsonl (newline-separated json objects)
        containers = []
        for line in json_output.strip().split("\n"):
            if line.strip():
                containers.append(json.loads(line))
        return containers


def get_container_command():
    """get the configured container runtime command."""
    runtime = get_container_runtime()
    try:
        return sh.Command(runtime)
    except sh.CommandNotFound:
        logger.error(
            f"Container runtime '{runtime}' not found. Please ensure it's installed and in your PATH."
        )
        raise


class _LazyContainerCommand:
    def bake(self, *args, **kwargs):
        return get_container_command().bake(*args, **kwargs)


CONTAINER_CMD = _LazyContainerCommand()

FORMAT_CONTAINER_OUTPUT = {
    "_out": lambda line: print(f"  {line}", end=""),
    "_err": lambda line: print(f"  {line}", end=""),
}


def _supports_image_exists():
    """check if the container runtime supports 'image exists' command."""
    return get_container_runtime() == "podman"


def _image_exists_podman(image_name):
    """check image existence using podman-style 'image exists' command."""
    try:
        image_exists_cmd = CONTAINER_CMD.bake("image", "exists", image_name)
        image_exists_cmd()
        return True
    except sh.ErrorReturnCode:
        return False


def _image_exists_docker(image_name):
    """check image existence using docker-style 'image inspect' command."""
    try:
        image_inspect_cmd = CONTAINER_CMD.bake("image", "inspect", image_name)
        image_inspect_cmd()
        return True
    except sh.ErrorReturnCode:
        return False


def _parse_container_labels(labels):
    """parse container labels that could be either a dict (podman) or string (docker)."""
    if isinstance(labels, dict):
        # podman format: {"key": "value", "mim": "1"}
        return labels
    elif isinstance(labels, str):
        # docker format: "key=value,mim=1"
        parsed = {}
        if labels.strip():
            for pair in labels.split(","):
                if "=" in pair:
                    key, value = pair.split("=", 1)
                    parsed[key.strip()] = value.strip()
        return parsed
    else:
        # handle null/empty labels
        return {}


def _parse_container_names(names):
    if isinstance(names, list):
        return [str(name).lstrip("/") for name in names if str(name).strip()]

    if isinstance(names, str):
        return [name.strip().lstrip("/") for name in names.split(",") if name.strip()]

    return []


def _get_container_by_name(container_name):
    for container in get_containers():
        names = _parse_container_names(container.get("Names"))
        if container_name in names:
            return container
    return None


def get_container_display_name(container):
    names = _parse_container_names(container.get("Names"))
    if names:
        return names[0]

    return container.get("Id", "<unknown>")


def get_containers(only_mim=False):
    """get list of containers, optionally filtered to only mim containers."""
    ps_args = ["-a", "--format", "json"]
    if only_mim:
        ps_args.append("--filter")
        ps_args.append("label=mim=1")
    ps_cmd = CONTAINER_CMD.bake("ps", *ps_args)
    container_json = ps_cmd()
    return parse_container_json(container_json)


def _get_container_inspect(container_name):
    inspect_cmd = CONTAINER_CMD.bake("inspect", container_name)
    inspect_json = inspect_cmd()
    inspect_data = parse_container_json(inspect_json)
    if len(inspect_data) == 0:
        return None

    return inspect_data[0]


def get_container_mounts(container_name):
    inspect_data = _get_container_inspect(container_name)
    if inspect_data is None:
        return []

    mounts = inspect_data.get("Mounts", [])
    parsed_mounts = []
    for mount in mounts:
        source = mount.get("Source") or mount.get("source")
        destination = mount.get("Destination") or mount.get("destination")
        if source and destination:
            parsed_mounts.append(
                {
                    "source": source,
                    "destination": destination,
                }
            )

    return parsed_mounts


def get_container_env(container_name):
    inspect_data = _get_container_inspect(container_name)
    if inspect_data is None:
        return {}

    env = inspect_data.get("Config", {}).get("Env", [])
    if not isinstance(env, list):
        return {}

    parsed_env = {}
    for item in env:
        if not isinstance(item, str) or "=" not in item:
            continue
        key, value = item.split("=", 1)
        parsed_env[key] = value

    return parsed_env


def container_exists(container_name):
    """check if a container exists by name."""
    return _get_container_by_name(container_name) is not None


def container_is_running(container_name):
    """check if a container is currently running."""
    container = _get_container_by_name(container_name)
    if container is None:
        return False

    return container.get("State") == "running"


def container_is_mim(container_name):
    """check if a container is a mim container (has mim=1 label)."""
    container = _get_container_by_name(container_name)
    if container is None:
        return False

    labels = _parse_container_labels(container.get("Labels"))
    return labels.get("mim") == "1"


def get_images():
    """get list of container images."""
    images_cmd = CONTAINER_CMD.bake("images", "--format", "json")
    images_json = images_cmd()
    return parse_container_json(images_json)


def image_exists(image_name):
    """check if an image exists using the appropriate method for the container runtime."""
    if _supports_image_exists():
        return _image_exists_podman(image_name)
    else:
        return _image_exists_docker(image_name)
