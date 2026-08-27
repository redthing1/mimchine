from __future__ import annotations

import os
import pwd
import re
import shlex
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .domain import validate_machine_name
from .paths import cache_dir, config_dir, data_dir
from .process import ProcessRunner
from .state import MachineStore


SSH_HOST_SUFFIX = ".mim"
INCLUDE_BEGIN = b"# >>> mimchine ssh >>>"
INCLUDE_END = b"# <<< mimchine ssh <<<"


@dataclass(frozen=True)
class SshPaths:
    client_config: Path
    server_config: Path
    state_dir: Path
    host_key: Path
    client_key: Path
    known_hosts: Path
    user_config: Path
    app_cache_dir: Path

    @classmethod
    def default(cls) -> "SshPaths":
        app_config_dir = config_dir()
        app_data_dir = data_dir()
        app_cache_dir = cache_dir()
        state_dir = app_data_dir / "ssh"
        return cls(
            client_config=app_config_dir / "ssh_config",
            server_config=app_config_dir / "sshd_config",
            state_dir=state_dir,
            host_key=state_dir / "host_ed25519",
            client_key=state_dir / "client_ed25519",
            known_hosts=state_dir / "known_hosts",
            user_config=Path.home() / ".ssh" / "config",
            app_cache_dir=app_cache_dir,
        )

    @property
    def host_public_key(self) -> Path:
        return self.host_key.with_suffix(".pub")

    @property
    def client_public_key(self) -> Path:
        return self.client_key.with_suffix(".pub")


class SshSetupManager:
    def __init__(
        self,
        paths: SshPaths,
        *,
        runner: ProcessRunner | None = None,
        ssh_path: str | None = None,
        sshd_path: str | None = None,
        mim_command: tuple[str, ...] | None = None,
        username: str | None = None,
    ):
        self.paths = paths
        self.runner = runner or ProcessRunner()
        self.ssh_path = ssh_path
        self.sshd_path = sshd_path
        self.mim_command = mim_command or current_mim_command()
        self.username = username or pwd.getpwuid(os.getuid()).pw_name

    @classmethod
    def default(cls) -> "SshSetupManager":
        return cls(SshPaths.default())

    def setup(self) -> None:
        self.paths.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.paths.state_dir, 0o700)
        self.paths.client_config.parent.mkdir(parents=True, exist_ok=True)

        self._ensure_keypair(self.paths.host_key)
        self._ensure_keypair(self.paths.client_key)
        known_hosts = self._known_hosts()
        server_config = self._server_config()
        client_config = self._client_config()
        self._validate_configs(server_config, client_config)
        _atomic_write_text(self.paths.known_hosts, known_hosts, 0o600)
        _atomic_write_text(self.paths.server_config, server_config, 0o600)
        _atomic_write_text(self.paths.client_config, client_config, 0o600)
        self._install_include(_resolved_write_path(self.paths.user_config))

    def remove(self) -> None:
        user_config = _resolved_write_path(self.paths.user_config)
        self._remove_include(user_config)
        for path in (
            self.paths.client_config,
            self.paths.server_config,
            self.paths.host_key,
            self.paths.host_public_key,
            self.paths.client_key,
            self.paths.client_public_key,
            self.paths.known_hosts,
        ):
            if path.is_file() or path.is_symlink():
                path.unlink()
        _remove_empty_dir(self.paths.state_dir)
        _remove_empty_dir(self.paths.client_config.parent)
        _remove_empty_dir(self.paths.state_dir.parent)
        _remove_empty_dir(self.paths.app_cache_dir)

    def _ensure_keypair(self, private_key: Path) -> None:
        private_key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not private_key.is_file():
            with tempfile.TemporaryDirectory(
                prefix=".key-", dir=private_key.parent
            ) as temp_dir:
                temp_key = Path(temp_dir) / private_key.name
                self.runner.run(
                    [
                        "ssh-keygen",
                        "-q",
                        "-t",
                        "ed25519",
                        "-N",
                        "",
                        "-f",
                        str(temp_key),
                    ],
                    capture=True,
                )
                os.chmod(temp_key, 0o600)
                temp_key.replace(private_key)

        os.chmod(private_key, 0o600)
        public = self.runner.run(
            ["ssh-keygen", "-y", "-f", str(private_key)],
            capture=True,
        ).stdout.strip()
        if not public:
            raise ValueError(
                f"ssh-keygen returned an empty public key for {private_key}"
            )
        _atomic_write_text(private_key.with_suffix(".pub"), public + "\n", 0o600)

    def _known_hosts(self) -> str:
        public = self.paths.host_public_key.read_text(encoding="utf-8").strip()
        return f"*{SSH_HOST_SUFFIX} {public}\n"

    def _server_config(self) -> str:
        lines = (
            f"HostKey {_ssh_value(self.paths.host_key)}",
            f"AuthorizedKeysFile {_ssh_token_value(self.paths.client_public_key)}",
            f"AllowUsers {_ssh_value(self.username)}",
            "AuthenticationMethods publickey",
            "PubkeyAuthentication yes",
            "PasswordAuthentication no",
            "KbdInteractiveAuthentication no",
            "PermitEmptyPasswords no",
            "PermitRootLogin prohibit-password",
            "StrictModes yes",
            "DisableForwarding yes",
            "AllowAgentForwarding no",
            "AllowTcpForwarding no",
            "X11Forwarding no",
            "PermitTunnel no",
            "PermitUserEnvironment no",
            "PermitUserRC no",
            "PermitTTY yes",
            "PrintMotd no",
            "PrintLastLog no",
            "MaxAuthTries 1",
            "MaxSessions 1",
            "LogLevel ERROR",
        )
        return "\n".join(lines) + "\n"

    def _client_config(self) -> str:
        proxy_parts = (
            _required_executable("env"),
            f"XDG_CONFIG_HOME={self.paths.client_config.parent.parent}",
            f"XDG_DATA_HOME={self.paths.state_dir.parent.parent}",
            f"XDG_CACHE_HOME={self.paths.app_cache_dir.parent}",
            *self.mim_command,
            "_ssh-proxy",
        )
        proxy = shlex.join((*map(_ssh_literal, proxy_parts), "%h"))
        lines = (
            f"Host *{SSH_HOST_SUFFIX}",
            f"    User {_ssh_token_value(self.username)}",
            f"    ProxyCommand {proxy}",
            f"    IdentityFile {_ssh_token_value(self.paths.client_key)}",
            "    IdentitiesOnly yes",
            "    PreferredAuthentications publickey",
            "    PasswordAuthentication no",
            "    KbdInteractiveAuthentication no",
            f"    UserKnownHostsFile {_ssh_token_value(self.paths.known_hosts)}",
            "    StrictHostKeyChecking yes",
            "    CheckHostIP no",
            "    CanonicalizeHostname no",
            "    ForwardAgent no",
            "    RequestTTY auto",
            "    StdinNull no",
        )
        return "\n".join(lines) + "\n"

    def _validate_configs(self, server_config: str, client_config: str) -> None:
        sshd_path = self.sshd_path or _required_executable("sshd")
        ssh_path = self.ssh_path or _required_executable("ssh")
        with tempfile.TemporaryDirectory(
            prefix=".ssh-setup-", dir=self.paths.client_config.parent
        ) as temp_dir:
            temp_root = Path(temp_dir)
            server_path = temp_root / "sshd_config"
            client_path = temp_root / "ssh_config"
            _atomic_write_text(server_path, server_config, 0o600)
            _atomic_write_text(client_path, client_config, 0o600)
            self.runner.run(
                [sshd_path, "-t", "-f", str(server_path)],
                capture=True,
            )
            self.runner.run(
                [
                    ssh_path,
                    "-G",
                    "-F",
                    str(client_path),
                    f"probe{SSH_HOST_SUFFIX}",
                ],
                capture=True,
            )

    def _install_include(self, user_config: Path) -> None:
        user_config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        original = user_config.read_bytes() if user_config.is_file() else b""
        preserved = _without_include_block(original)
        include = _include_block(self.paths.client_config)
        content = include + (b"\n" if preserved else b"") + preserved
        mode = _existing_mode(user_config, 0o600)
        _atomic_write_bytes(user_config, content, mode)

    def _remove_include(self, user_config: Path) -> None:
        if not user_config.is_file():
            return
        original = user_config.read_bytes()
        content = _without_include_block(original)
        if content == original:
            return
        if not content:
            user_config.unlink()
            _remove_empty_dir(user_config.parent)
            return
        _atomic_write_bytes(user_config, content, _existing_mode(user_config, 0o600))


def current_mim_command() -> tuple[str, ...]:
    invoked = Path(sys.argv[0])
    if invoked.name in {"mim", "mimchine"}:
        executable = shutil.which(str(invoked))
        if executable:
            return (str(Path(executable).resolve()),)
    return (str(Path(sys.executable).resolve()), "-m", "mimchine")


def machine_name_from_ssh_host(host: str) -> str:
    text = str(host).strip()
    if not text.endswith(SSH_HOST_SUFFIX):
        raise ValueError(f"mimchine SSH host must end with [{SSH_HOST_SUFFIX}]: {host}")
    return validate_machine_name(text[: -len(SSH_HOST_SUFFIX)])


def ssh_proxy_command(
    host: str,
    *,
    paths: SshPaths | None = None,
    store: MachineStore | None = None,
    sshd_path: str | None = None,
    mim_command: tuple[str, ...] | None = None,
    environment: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    ssh_paths = paths or SshPaths.default()
    name = machine_name_from_ssh_host(host)
    machine_store = store or MachineStore(data_dir() / "machines")
    machine_store.load(name)
    if not ssh_paths.server_config.is_file():
        raise ValueError("SSH integration is not set up; run [mim setup ssh]")

    server = sshd_path or _required_executable("sshd")
    session_command = (*(mim_command or current_mim_command()), "_ssh-session", name)
    source_env = os.environ if environment is None else environment
    session_env = tuple(
        f"{key}={source_env[key]}"
        for key in ("PATH", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME")
        if source_env.get(key)
    )
    if session_env:
        session_command = (_required_executable("env"), *session_env, *session_command)
    force_command = shlex.join(session_command)
    return (
        server,
        "-i",
        "-e",
        "-q",
        "-f",
        str(ssh_paths.server_config),
        "-o",
        f"ForceCommand={force_command}",
    )


def ssh_client_command(
    name: str,
    command: tuple[str, ...] = (),
    *,
    paths: SshPaths | None = None,
    ssh_path: str | None = None,
) -> tuple[str, ...]:
    ssh_paths = paths or SshPaths.default()
    machine = validate_machine_name(name)
    if not ssh_paths.client_config.is_file():
        raise ValueError("SSH integration is not set up; run [mim setup ssh]")
    client = ssh_path or _required_executable("ssh")
    return (
        client,
        "-F",
        str(ssh_paths.client_config),
        f"{machine}{SSH_HOST_SUFFIX}",
        *command,
    )


def exec_process(command: tuple[str, ...]) -> None:
    os.execv(command[0], command)


def _required_executable(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise ValueError(f"required command was not found: {name}")
    return str(Path(executable).resolve())


def _ssh_value(value: str | Path) -> str:
    text = _ssh_config_text(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ssh_literal(value: str | Path) -> str:
    return _ssh_config_text(value).replace("%", "%%")


def _ssh_token_value(value: str | Path) -> str:
    return _ssh_value(_ssh_literal(value))


def _ssh_config_text(value: str | Path) -> str:
    text = str(value)
    if any(character in text for character in ("\0", "\r", "\n")):
        raise ValueError("SSH configuration values cannot contain line breaks")
    return text


def _include_block(client_config: Path) -> bytes:
    include = f"Include {_ssh_token_value(client_config)}".encode("utf-8")
    return b"\n".join((INCLUDE_BEGIN, include, INCLUDE_END, b""))


def _without_include_block(content: bytes) -> bytes:
    pattern = re.compile(
        rb"(?m)^"
        + re.escape(INCLUDE_BEGIN)
        + rb"\r?\n.*?^"
        + re.escape(INCLUDE_END)
        + rb"(?:\r?\n)?(?:\r?\n)?",
        re.DOTALL,
    )
    return pattern.sub(b"", content)


def _resolved_write_path(path: Path) -> Path:
    return path.resolve(strict=False) if path.is_symlink() else path


def _existing_mode(path: Path, default: int) -> int:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return default


def _atomic_write_text(path: Path, content: str, mode: int) -> None:
    _atomic_write_bytes(path, content.encode("utf-8"), mode)


def _atomic_write_bytes(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temp_path, mode)
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _remove_empty_dir(path: Path) -> None:
    try:
        path.rmdir()
    except (FileNotFoundError, OSError):
        pass
