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
        for line in json_output.strip().split('\n'):
            if line.strip():
                containers.append(json.loads(line))
        return containers


def get_container_command():
    """get the configured container runtime command."""
    runtime = get_container_runtime()
    try:
        return sh.Command(runtime)
    except sh.CommandNotFound:
        logger.error(f"Container runtime '{runtime}' not found. Please ensure it's installed and in your PATH.")
        raise


CONTAINER_CMD = get_container_command()

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


def get_containers(only_mim=False):
    """get list of containers, optionally filtered to only mim containers."""
    ps_args = ["-a", "--format", "json"]
    if only_mim:
        ps_args.append("--filter")
        ps_args.append("label=mim=1")
    ps_cmd = CONTAINER_CMD.bake("ps", *ps_args)
    container_json = ps_cmd()
    return parse_container_json(container_json)


def container_exists(container_name):
    """check if a container exists by name."""
    containers = get_containers()
    for container in containers:
        if container_name in container["Names"]:
            return True
    return False


def container_is_running(container_name):
    """check if a container is currently running."""
    containers = get_containers()
    for container in containers:
        if container_name in container["Names"]:
            if container["State"] == "running":
                return True
    return False


def container_is_mim(container_name):
    """check if a container is a mim container (has mim=1 label)."""
    containers = get_containers()
    for container in containers:
        if container_name in container["Names"]:
            if container["Labels"]["mim"] == "1":
                return True
    return False


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
