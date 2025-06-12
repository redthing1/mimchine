import sh
import json
from minlog import logger

from .config import get_container_runtime


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
    containers = json.loads(container_json)
    return containers


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
    images = json.loads(images_json)
    return images


def image_exists(image_name):
    image_exists_cmd = CONTAINER_CMD.bake("image", "exists", image_name)
    try:
        image_exists_cmd()
        return True
    except sh.ErrorReturnCode:
        return False
