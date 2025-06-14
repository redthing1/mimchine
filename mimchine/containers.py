import sh
import json
from minlog import logger

from .config import get_container_runtime


def parse_container_json(json_output):
    """Parse JSON output that could be either a JSON array or JSONL format."""
    if not json_output.strip():
        return []
    
    try:
        # First try parsing as a single JSON document (could be an array)
        result = json.loads(json_output)
        # If it's not a list, wrap it in one
        return result if isinstance(result, list) else [result]
    except json.JSONDecodeError:
        # If that fails, try parsing as JSONL (newline-separated JSON objects)
        containers = []
        for line in json_output.strip().split('\n'):
            if line.strip():
                containers.append(json.loads(line))
        return containers


def get_container_command():
    """Get the configured container runtime command."""
    runtime = get_container_runtime()
    try:
        return sh.Command(runtime)
    except sh.CommandNotFound:
        logger.error(f"Container runtime '{runtime}' not found. Please ensure it's installed and in your PATH.")
        raise


CONTAINER_CMD = get_container_command()
PODMAN = CONTAINER_CMD

FORMAT_CONTAINER_OUTPUT = {
    "_out": lambda line: print(f"  {line}", end=""),
    "_err": lambda line: print(f"  {line}", end=""),
}

FORMAT_PODMAN_OUTPUT = FORMAT_CONTAINER_OUTPUT


def get_containers(only_mim=False):
    ps_args = ["-a", "--format", "json"]
    if only_mim:
        ps_args.append("--filter")
        ps_args.append("label=mim=1")
    ps_cmd = CONTAINER_CMD.bake("ps", *ps_args)
    container_json = ps_cmd()
    return parse_container_json(container_json)


def container_exists(container_name):
    podman_containers = get_containers()
    for container in podman_containers:
        if container_name in container["Names"]:
            return True
    return False


def container_is_running(container_name):
    podman_containers = get_containers()
    for container in podman_containers:
        if container_name in container["Names"]:
            if container["State"] == "running":
                return True
    return False


def container_is_mim(container_name):
    podman_containers = get_containers()
    for container in podman_containers:
        if container_name in container["Names"]:
            if container["Labels"]["mim"] == "1":
                return True
    return False


def get_images():
    images_cmd = CONTAINER_CMD.bake("images", "--format", "json")
    images_json = images_cmd()
    return parse_container_json(images_json)


def image_exists(image_name):
    image_exists_cmd = CONTAINER_CMD.bake("image", "exists", image_name)
    try:
        image_exists_cmd()
        return True
    except sh.ErrorReturnCode:
        return False
