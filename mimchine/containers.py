import json
import os
import subprocess
import tempfile

import sh

from .log import logger

from .config import get_container_runtime

SHELL_USER_LABEL_KEY = "mim.shell-user"
SHELL_USER_ROOT = "root"
SHELL_USER_USER = "user"
SUPPORTED_SHELL_USERS = (SHELL_USER_ROOT, SHELL_USER_USER)
SUPPORTED_IMAGE_ARCHIVE_SUFFIXES = (".tar", ".zst")
DEFAULT_ZSTD_EXPORT_ARGS = ("-T0", "--long", "-19")


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
            f"container runtime '{runtime}' not found. please ensure it's installed and in your PATH."
        )
        raise


class _LazyContainerCommand:
    def bake(self, *args, **kwargs):
        return get_container_command().bake(*args, **kwargs)


CONTAINER_CMD = _LazyContainerCommand()


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


def _normalize_shell_user(value: str) -> str:
    return value.strip().lower()


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


def get_container_display_name(container: dict) -> str:
    names = _parse_container_names(container.get("Names"))
    if names:
        return names[0]

    return str(container.get("Id", "<unknown>"))


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


def _get_image_inspect(image_name):
    inspect_cmd = CONTAINER_CMD.bake("image", "inspect", image_name)
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


def get_container_labels(container_name: str) -> dict[str, str]:
    inspect_data = _get_container_inspect(container_name)
    if inspect_data is None:
        return {}

    labels = inspect_data.get("Config", {}).get("Labels", {})
    return _parse_container_labels(labels)


def get_container_image(container_name: str) -> str:
    inspect_data = _get_container_inspect(container_name)
    if inspect_data is None:
        return ""

    image_name = inspect_data.get("Config", {}).get("Image", "")
    if not isinstance(image_name, str):
        return ""

    return image_name.strip()


def get_image_labels(image_name: str) -> dict[str, str]:
    inspect_data = _get_image_inspect(image_name)
    if inspect_data is None:
        return {}

    labels = inspect_data.get("Config", {}).get("Labels", {})
    return _parse_container_labels(labels)


def resolve_container_shell_user(container_name: str) -> str | None:
    label_value = get_container_labels(container_name).get(SHELL_USER_LABEL_KEY, "")
    if not isinstance(label_value, str):
        label_value = ""

    if len(label_value.strip()) == 0:
        image_name = get_container_image(container_name)
        if len(image_name) > 0:
            image_label_value = get_image_labels(image_name).get(
                SHELL_USER_LABEL_KEY, ""
            )
            if isinstance(image_label_value, str):
                label_value = image_label_value

    if len(label_value.strip()) == 0:
        return None

    normalized_shell_user = _normalize_shell_user(label_value)
    if normalized_shell_user not in SUPPORTED_SHELL_USERS:
        logger.warn(
            f"ignoring invalid shell user label [{label_value}] on container [{container_name}]"
        )
        return None

    return normalized_shell_user


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


def resolve_image_home(image_name: str) -> str:
    probe_cmd = CONTAINER_CMD.bake(
        "run",
        "--rm",
        "--entrypoint",
        "sh",
        image_name,
        "-lc",
        'printf "%s" "${HOME:-}"',
    )
    try:
        output = str(probe_cmd()).strip()
        if output:
            return output
    except sh.ErrorReturnCode:
        pass

    return "/root"


def _normalize_archive_path(archive_path: str) -> str:
    stripped_archive_path = archive_path.strip()
    if len(stripped_archive_path) == 0:
        raise ValueError("archive path cannot be empty")

    normalized_archive_path = os.path.abspath(os.path.expanduser(stripped_archive_path))
    return normalized_archive_path


def is_supported_image_archive_path(archive_path: str) -> bool:
    normalized_archive_path = archive_path.strip().lower()
    return normalized_archive_path.endswith(SUPPORTED_IMAGE_ARCHIVE_SUFFIXES)


def is_zstd_archive(archive_path: str) -> bool:
    return archive_path.strip().lower().endswith(".zst")


def _validate_image_archive_path(archive_path: str) -> str:
    normalized_archive_path = _normalize_archive_path(archive_path)
    if not is_supported_image_archive_path(normalized_archive_path):
        raise ValueError(
            f"unsupported archive path [{normalized_archive_path}]. expected a .tar or .zst file"
        )

    return normalized_archive_path


def _require_zstd() -> None:
    try:
        sh.Command("zstd")
    except sh.CommandNotFound as exc:
        raise ValueError(
            "zstd not found. please ensure it is installed and in your PATH."
        ) from exc


def _build_runtime_args(*args: str) -> list[str]:
    runtime = get_container_runtime()
    try:
        sh.Command(runtime)
    except sh.CommandNotFound as exc:
        raise RuntimeError(
            f"command [{runtime}] not found. please ensure it is installed and in your PATH."
        ) from exc

    return [runtime, *args]


def _build_image_save_command(image_name: str) -> list[str]:
    save_args = ["save"]

    if get_container_runtime() == "podman":
        save_args.extend(["--format", "docker-archive"])

    save_args.append(image_name)
    return _build_runtime_args(*save_args)


def _build_image_load_command(input_path: str | None = None) -> list[str]:
    load_args = ["load"]

    if input_path is not None:
        load_args.extend(["--input", input_path])

    return _build_runtime_args(*load_args)


def _spawn_process(
    args: list[str],
    *,
    stdin=None,
    stdout=None,
):
    try:
        return subprocess.Popen(args, stdin=stdin, stdout=stdout)
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"command [{args[0]}] not found. please ensure it is installed and in your PATH."
        ) from exc


def _raise_process_failure(error_action: str, return_code: int) -> None:
    raise RuntimeError(f"{error_action} failed with error code {return_code}")


def _run_process(
    args: list[str],
    *,
    error_action: str,
) -> None:
    process = _spawn_process(args)
    return_code = process.wait()
    if return_code != 0:
        _raise_process_failure(error_action, return_code)


def _run_stream_to_file(
    args: list[str],
    output_path: str,
    *,
    error_action: str,
) -> None:
    with open(output_path, "wb") as archive_file:
        process = _spawn_process(args, stdout=archive_file)
        return_code = process.wait()

    if return_code != 0:
        _raise_process_failure(error_action, return_code)


def _run_pipeline(
    producer_args: list[str],
    consumer_args: list[str],
    *,
    error_action: str,
) -> None:
    producer_process = _spawn_process(producer_args, stdout=subprocess.PIPE)
    assert producer_process.stdout is not None

    consumer_process = _spawn_process(consumer_args, stdin=producer_process.stdout)
    producer_process.stdout.close()

    consumer_return_code = consumer_process.wait()
    producer_return_code = producer_process.wait()

    if consumer_return_code != 0:
        _raise_process_failure(error_action, consumer_return_code)

    if producer_return_code != 0:
        _raise_process_failure(error_action, producer_return_code)


def _run_pipeline_to_file(
    producer_args: list[str],
    consumer_args: list[str],
    output_path: str,
    *,
    error_action: str,
) -> None:
    with open(output_path, "wb") as archive_file:
        producer_process = _spawn_process(producer_args, stdout=subprocess.PIPE)
        assert producer_process.stdout is not None

        consumer_process = _spawn_process(
            consumer_args,
            stdin=producer_process.stdout,
            stdout=archive_file,
        )
        producer_process.stdout.close()

        consumer_return_code = consumer_process.wait()
        producer_return_code = producer_process.wait()

    if consumer_return_code != 0:
        _raise_process_failure(error_action, consumer_return_code)

    if producer_return_code != 0:
        _raise_process_failure(error_action, producer_return_code)


def _create_temp_output_path(output_path: str) -> str:
    output_dir = os.path.dirname(output_path) or "."
    if not os.path.isdir(output_dir):
        raise ValueError(f"output directory [{output_dir}] does not exist")

    file_descriptor, temp_output_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(output_path)}.",
        suffix=".tmp",
        dir=output_dir,
    )
    os.close(file_descriptor)
    os.unlink(temp_output_path)
    return temp_output_path


def _cleanup_temp_path(temp_output_path: str) -> None:
    try:
        os.remove(temp_output_path)
    except FileNotFoundError:
        pass


def export_image_archive(
    image_name: str,
    output_path: str,
    *,
    force: bool = False,
) -> None:
    normalized_image_name = image_name.strip()
    if len(normalized_image_name) == 0:
        raise ValueError("image name cannot be empty")

    normalized_output_path = _validate_image_archive_path(output_path)
    if os.path.isdir(normalized_output_path):
        raise ValueError(
            f"archive output path [{normalized_output_path}] is a directory"
        )

    if os.path.exists(normalized_output_path) and not force:
        raise ValueError(
            f"archive [{normalized_output_path}] already exists. use --force to overwrite it"
        )

    temp_output_path = _create_temp_output_path(normalized_output_path)
    save_command = _build_image_save_command(normalized_image_name)

    try:
        if is_zstd_archive(normalized_output_path):
            _require_zstd()
            _run_pipeline_to_file(
                save_command,
                ["zstd", *DEFAULT_ZSTD_EXPORT_ARGS],
                temp_output_path,
                error_action="image export",
            )
        else:
            _run_stream_to_file(
                save_command,
                temp_output_path,
                error_action="image export",
            )

        os.replace(temp_output_path, normalized_output_path)
    except Exception:
        _cleanup_temp_path(temp_output_path)
        raise


def import_image_archive(input_path: str) -> None:
    normalized_input_path = _validate_image_archive_path(input_path)
    if not os.path.exists(normalized_input_path):
        raise ValueError(f"archive [{normalized_input_path}] does not exist")

    if not os.path.isfile(normalized_input_path):
        raise ValueError(f"archive [{normalized_input_path}] is not a file")

    if is_zstd_archive(normalized_input_path):
        _require_zstd()
        _run_pipeline(
            ["zstd", "-d", "-c", normalized_input_path],
            _build_image_load_command(),
            error_action="image import",
        )
        return

    _run_process(
        _build_image_load_command(normalized_input_path),
        error_action="image import",
    )
