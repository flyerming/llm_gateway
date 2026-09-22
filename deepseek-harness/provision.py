#!/usr/bin/env python3
"""Provision one DSH process and workspace per authenticated Nginx user."""

from __future__ import annotations

import hashlib
import os
import re
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


USER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
BASE_PORT = 31000
PORT_SPAN = 10000


def _yaml_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9._:/+@-]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def model_block() -> str:
    models: list[str] = []
    for raw in os.environ.get("DSH_DEFAULT_MODEL_IDS", "").split(","):
        model = raw.strip()
        if model and model not in models:
            models.append(model)
    if not models:
        raise RuntimeError("DSH_DEFAULT_MODEL_IDS must contain at least one model")
    return "".join(
        f"        - id: {_yaml_quote(model)}\n"
        f"          name: {_yaml_quote(model)}\n"
        for model in models
    )


class Provisioner:
    def __init__(self) -> None:
        self.data = Path(os.environ.get("DSH_DATA_DIR", "/data"))
        self.users = self.data / "users"
        self.workspaces = Path("/workspaces")
        self.template = Path("/etc/dsh/settings.yaml")
        self.lock = threading.Lock()
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.ports: dict[str, int] = {}

    @staticmethod
    def _validate_user(user: str) -> str:
        if not USER_RE.fullmatch(user):
            raise ValueError("invalid username")
        return user

    @staticmethod
    def _listening(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.05)
            return sock.connect_ex(("127.0.0.1", port)) == 0

    def _port(self, user: str) -> int:
        if user in self.ports:
            return self.ports[user]
        seed = int.from_bytes(hashlib.sha256(user.encode()).digest()[:4], "big")
        candidate = BASE_PORT + seed % PORT_SPAN
        used = set(self.ports.values())
        while candidate in used or self._listening(candidate):
            candidate = BASE_PORT + ((candidate - BASE_PORT + 1) % PORT_SPAN)
        self.ports[user] = candidate
        return candidate

    def ensure(self, raw_user: str) -> int:
        user = self._validate_user(raw_user)
        with self.lock:
            user_root = self.users / user
            dsh_home = user_root / ".dsh"
            workspace = self.workspaces / user
            user_root.mkdir(parents=True, exist_ok=True)
            dsh_home.mkdir(parents=True, exist_ok=True)
            workspace.mkdir(parents=True, exist_ok=True)

            settings = dsh_home / "settings.yaml"
            if not settings.exists():
                rendered = self.template.read_text(encoding="utf-8").replace(
                    "__DSH_MODELS__", model_block()
                )
                settings.write_text(rendered, encoding="utf-8")
                settings.chmod(0o600)

            process = self.processes.get(user)
            if process is None or process.poll() is not None:
                port = self._port(user)
                log = user_root / "dsh.log"
                env = os.environ.copy()
                env.update(
                    {
                        "HOME": str(user_root),
                        "DSH_HOME": str(dsh_home),
                    }
                )
                log_handle = log.open("ab")
                process = subprocess.Popen(
                    [
                        "pnpm",
                        "dsh",
                        "web",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--trusted-host",
                        "127.0.0.1",
                        "--no-open",
                    ],
                    cwd="/opt/dsh",
                    env=env,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                self.processes[user] = process
            return self._port(user)


PROVISIONER = Provisioner()


class Handler(BaseHTTPRequestHandler):
    server_version = "dsh-provisioner/1"

    def do_GET(self) -> None:  # noqa: N802
        user = self.headers.get("X-Remote-User", "")
        try:
            port = PROVISIONER.ensure(user)
        except (RuntimeError, ValueError, OSError) as exc:
            self.send_error(500, str(exc))
            return
        self.send_response(204)
        self.send_header("X-DSH-Port", str(port))
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        print("provisioner:", fmt % args, flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 3090), Handler).serve_forever()
