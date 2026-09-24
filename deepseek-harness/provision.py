#!/usr/bin/env python3
"""Provision one DSH process and workspace per authenticated Nginx user."""

from __future__ import annotations

import hashlib
import http.client
import os
import re
import shutil
import socket
import stat
import subprocess
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO


# The username arrives from Nginx (`$remote_user`, i.e. the Basic Auth handle)
# and is pasted straight into `/data/users/<user>` and `/workspaces/<user>`.
# It therefore must not be able to denote anything but a literal child
# directory: a leading position is restricted to alphanumerics/underscore so
# `.`, `..` and dotfiles can never be requested (`/data/users/..` would
# otherwise resolve to `/data` and escape the user's own tree).
USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")
# `dsh web` prints exactly one authenticated URL line per process:
#   dsh web: http://127.0.0.1:31003/?token=<base64url> (LAN: ...)
# That token is the only way to mint DSH's browser-session cookie, and DSH only
# mints it for `GET /?token=<one token>` on the loopback authority. The
# provisioner therefore performs the exchange itself and hands the resulting
# cookie to Nginx, which injects it on every proxied request (see
# `_exchange_session`). The browser never sees a token, so there is no
# redirect-to-`/?token=` step and none of its loop hazards.
LAUNCH_URL_RE = re.compile(
    r"dsh web:\s+http://127\.0\.0\.1:\d+/\?token=([A-Za-z0-9_-]+)"
)
# The URL line is printed after the Loader tree settles, which can trail the
# moment the HTTP port starts answering by a short moment.
TOKEN_WAIT_S = float(os.environ.get("DSH_TOKEN_WAIT_SECONDS", "10"))

# Upper bound on how much freshly appended log we scan for the launch URL.
LOG_SCAN_BYTES = 262144

# How long a minted browser-session cookie is trusted before the provisioner
# mints a fresh one. DSH's cookie is authority-bound and hard-expires (default 30
# days), and the provisioner — not the browser — holds it, so on a container that
# stays up for weeks every request would start failing at the 30-day cliff.
# Re-minting well ahead of that deadline keeps a running instance usable.
COOKIE_REFRESH_S = float(os.environ.get("DSH_COOKIE_REFRESH_SECONDS", str(12 * 3600)))

BASE_PORT = 31000
PORT_SPAN = 10000

# How each user's `dsh web` is launched.
#
# The Dockerfile builds the checkout with `pnpm run build`, which emits the
# bundled CLI at `apps/cli/lib/bin.js`. Running that with plain `node` is both
# the officially shipped entry point and the only viable option once the user
# is dropped to a non-root UID: the `pnpm dsh` script goes through `tsx`, which
# wants to write a transpile cache under the (root-owned, read-only) install
# tree. The prebuilt bundle needs no write access to `/opt/dsh`.
DSH_CLI_ENTRY = os.environ.get("DSH_CLI_ENTRY", "/opt/dsh/apps/cli/lib/bin.js")
DSH_WEB_COMMAND = [os.environ.get("DSH_NODE_BIN", "node"), DSH_CLI_ENTRY]

# 每个 Harness 用户分配一个专属的容器内 UID，用它启动该用户的 `dsh web`。
# 这是文件隔离的**唯一**可靠手段：同一 UID 下 chmod 挡不住任何东西（root 直接
# 无视权限位），所以只有真正降权成不同 UID，`/data/users/<user>`、
# `/workspaces/<user>` 才互为不可读。UID 从 BASE_UID 起顺序分配，映射写在
# `/etc/dsh-auth/.uidmap`（0600 root）里 —— 容器重建会重置镜像内的 /etc/passwd，
# 只有落在数据卷里的映射才能保证重启后新旧文件仍归同一个 UID 所有。
BASE_UID = int(os.environ.get("DSH_BASE_UID", "20000"))
UID_CEILING = int(os.environ.get("DSH_UID_CEILING", "60000"))

# 逃生开关：设为 0/false/no 时退回「所有用户共用一个 root 进程」的旧行为。
# 仅在降权启动出问题时用来临时恢复可用性，不要长期开启。
ISOLATE_USERS = os.environ.get("DSH_ISOLATE_USERS", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)

# How long to wait for a freshly spawned `dsh web` to start accepting
# connections before reporting failure. Without this wait, the first request
# after spawning proxies into a port that is not listening yet and Nginx turns
# it into an opaque 502/500.
STARTUP_TIMEOUT_S = float(os.environ.get("DSH_STARTUP_TIMEOUT_SECONDS", "120"))
STARTUP_POLL_S = 0.2

# 镜像里 `/root/.npmrc`（构建期写入的 registry / 重试参数）只对 root 生效。
# 降权运行后 HOME 变成用户自己的目录，运行期 pnpm（`dsh plugin add` 等）会读
# 用户 HOME 下的 .npmrc —— 不补一份它就会直连 registry.npmjs.org。
NPMRC_TEMPLATE = os.environ.get("DSH_NPMRC_FILE", "/root/.npmrc")

# DSH 的 home 级用户补丁层（`$DSH_HOME/cordis.patch.yml`），应用顺序在官方
# bundle 与 profile 之后、`--patch` overlay 之前，因此可以用来做部署级覆盖而
# **完全不改官方源码**。镜像里这份模板做两件事：
#   1) 把内置 `web.searchProvider` 指到一个未注册的 id，让只认 DEEPSEEK_API_KEY
#      的内置 web_search 确定性地失败（本部署不导出该 key），使模型改走 MCP 搜索；
#   2) 通过 `@deepseek-ai/dsh-mcp-client` 接入 compose 里的 searxng-mcp 服务。
HOME_PATCH = Path(os.environ.get("DSH_HOME_PATCH_FILE", "/etc/dsh/cordis.patch.yml"))
# Keep a built-in copy so a deployment copied from Git before the new, optional
# cordis.patch.yml file was added can still build and retain the search fix.
# When the file exists in the image, it remains the source of truth and can be
# changed without editing this provisioner.
DEFAULT_HOME_PATCH = """\
# DSH home-level deployment patch.
- id: web
  config:
    searchProvider: none
    fetchProvider: http
- insert:
    - id: mcp-searxng
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: searxng
        transport: streamable-http
        url: http://searxng-mcp:8090/mcp
        headers:
          Authorization: !!js '`Bearer ${process.env.MCP_SEARXNG_TOKEN}`'
        failOnStartupError: false
"""
# 判断用户目录里的补丁是否已是当前版本：模板里的 `mcp-searxng` row id 只在
# 真正接入 MCP 时出现（注释里写的是 `searxng-mcp`，不会误命中）。缺这个特征串
# 就说明是旧版本（或用户删改坏了），用模板覆盖重写。
PATCH_MARKER = "mcp-searxng"


def _yaml_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9._:/+@-]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _read_home_patch() -> str:
    """Read the external patch, or use the migration-safe built-in fallback."""
    try:
        return HOME_PATCH.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"provisioner: cannot read home patch template {HOME_PATCH}: {exc}; "
            "using built-in fallback",
            flush=True,
        )
        return DEFAULT_HOME_PATCH


SETTINGS_PLACEHOLDER = "__DSH_MODELS__"
# Which model a freshly created session uses. DSH's built-in default points at
# its own `deepseek-official` provider (`packages/bundle/base/cordis.patch.yml`),
# which only reads DEEPSEEK_API_KEY from the environment. Nothing here exports
# that variable, so the first message of every session would fail with
# `MISSING_CREDENTIAL ... provider route "deepseek-official"`. The template's
# `agent-default-model` section overrides it with our private gateway route.
DEFAULT_MODEL_PLACEHOLDER = "__DSH_DEFAULT_MODEL__"
# Signatures left behind by earlier templates, so per-user settings files
# already sitting in the named volume are regenerated from the current template
# instead of pinning users to an outdated (or outright broken) document.
#
# - `BROKEN_SETTINGS_MARKER`: an old template's *comment* also contained the
#   placeholder, so `str.replace` substituted inside the comment, jammed the
#   models block into the middle of a comment line, and produced invalid YAML
#   (`settings-file: invalid document ... UNEXPECTED_TOKEN`), which crashed
#   `dsh web` on every start.
# - `STALE_SETTINGS_MARKER`: templates written before `agent-default-model` was
#   added, which kept new sessions pointed at `deepseek-official`.
BROKEN_SETTINGS_MARKER = "with DSH_DEFAULT_MODEL_IDS."
STALE_SETTINGS_MARKER = "agent-default-model:"
# A versioned comment survives normal settings UI edits and gives migrations a
# deterministic signature. It also catches the first broken renderer's output
# even when its old marker text was partially consumed by str.replace.
SETTINGS_TEMPLATE_MARKER = "# DSH_DEPLOY_SETTINGS_V4"

# 手写模型条目默认「无推理能力」，DSH 就不会渲染思考强度选择器。为每个
# 私有网关模型补上 `reasoningEfforts`，把私有网关（LiteLLM）支持的思考档位
# 暴露出来；具体线协议由 `compat.thinkingFormat` 决定（默认 openai 风格，
# 上游 README 示例用 deepseek。可用 DSH_MODEL_REASONING_FORMAT 环境变量覆盖）。
#
# 语义（见官方 `packages/llm/llm-pi-ai/src/catalog.ts` resolveModelReasoning）：
#   - key = 可选档位（off/low/high/max），value = 发给
#     网关的线值；只有 off 允许留空（null，表示该档不发送任何东西）；
#   - 至少要声明一个非 off 档位，否则视为配置错误；
#   - 未声明的档位会被 pin 成 null（即界面上不可选）。
MODEL_REASONING_FORMAT = os.environ.get("DSH_MODEL_REASONING_FORMAT", "openai").strip() or "openai"
# 私有 DeepSeek 路由对齐官方模型的四档选择器。
MODEL_REASONING_LEVELS: tuple[tuple[str, str | None], ...] = (
    ("off", None),
    ("low", "low"),
    ("high", "high"),
    ("max", "max"),
)


def model_ids() -> list[str]:
    models: list[str] = []
    for raw in os.environ.get("DSH_DEFAULT_MODEL_IDS", "").split(","):
        model = raw.strip()
        if model and model not in models:
            models.append(model)
    if not models:
        raise RuntimeError("DSH_DEFAULT_MODEL_IDS must contain at least one model")
    return models


def model_block() -> str:
    parts: list[str] = []
    for model in model_ids():
        parts.append(f"        - id: {_yaml_quote(model)}\n")
        parts.append(f"          name: {_yaml_quote(model)}\n")
        parts.append("          compat:\n")
        parts.append("            supportsReasoningEffort: true\n")
        parts.append(
            f"            thinkingFormat: {_yaml_quote(MODEL_REASONING_FORMAT)}\n"
        )
        parts.append("          reasoningEfforts:\n")
        for level, wire in MODEL_REASONING_LEVELS:
            # 空值档位（off）写成 YAML null，表示该档不发送任何线值；
            # 其余档位写实际的线字符串。
            #
            # 档位名一律加引号：`off`/`on`/`yes`/`no` 在 YAML 1.1 里是布尔字面量
            # （PyYAML 会把 `off:` 读成键 False）。DSH 用的是 js-yaml v4（YAML 1.2
            # core schema，`off` 保持字符串），所以不加引号本来也能跑；但加引号后
            # 任何解析器都得到同一个字符串键，省得将来换工具时踩坑。
            parts.append(
                f'            "{level}": {_yaml_quote(wire) if wire is not None else "null"}\n'
            )
    return "".join(parts)


def render_settings(template: str) -> str:
    models = model_ids()
    rendered = template.replace(SETTINGS_PLACEHOLDER, model_block())
    return rendered.replace(DEFAULT_MODEL_PLACEHOLDER, _yaml_quote(models[0]))


class Provisioner:
    def __init__(self) -> None:
        self.data = Path(os.environ.get("DSH_DATA_DIR", "/data"))
        self.users = self.data / "users"
        self.workspaces = Path("/workspaces")
        self.template = Path("/etc/dsh/settings.yaml")
        # 用户 → 容器内专属 UID 的持久映射，和 htpasswd 一起放在独立的
        # /etc/dsh-auth 卷（0750 root:www-data）：容器重建会重置镜像里的
        # /etc/passwd，只有落在数据卷里的这份映射能保证重启后旧文件仍归同一个
        # UID 所有；不放在 /data 下是为了让用户既列举不到、也读不到它。
        self.auth = Path(os.environ.get("DSH_AUTH_DIR", "/etc/dsh-auth"))
        self.uidmap = self.auth / ".uidmap"
        self.uids: dict[str, int] = {}
        self.uidmap_loaded = False
        self.lock = threading.Lock()
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.ports: dict[str, int] = {}
        # DSH prints one `?token=` launch URL per process; it is the only way to
        # mint DSH's own browser-session cookie.
        self.tokens: dict[str, str] = {}
        # Cookie minted by exchanging that token. Nginx injects it on every
        # proxied request, so the browser never needs a token URL of its own.
        self.cookies: dict[str, str] = {}
        # Monotonic timestamp of when each cookie was minted, so a stale cookie
        # can be refreshed before DSH's hard expiry rejects it.
        self.cookie_minted_at: dict[str, float] = {}
        # Set once a user's DSH instance is accepting connections. Concurrent
        # requests for a user that is still starting wait on this instead of
        # being proxied to a port that is not listening yet (which Nginx would
        # surface as a bare 500/502).
        self.ready: dict[str, threading.Event] = {}
        # A legacy root-run DSH may have left root-owned children below a user
        # directory whose top-level owner is already correct. Adopt each user's
        # tree once per provisioner lifetime so those stale children are fixed
        # without recursively chowning on every authenticated request.
        self.adopted_users: set[str] = set()

    @staticmethod
    def _validate_user(user: str) -> str:
        if not USER_RE.fullmatch(user):
            raise ValueError("invalid username")
        return user

    def _load_uidmap(self) -> None:
        """Read the persistent user → UID map, tolerating a missing/partial file."""
        self.uidmap_loaded = True
        try:
            text = self.uidmap.read_text(encoding="utf-8")
        except OSError:
            return
        for line in text.splitlines():
            name, _, raw = line.partition(":")
            name = name.strip()
            raw = raw.strip()
            if not name or not raw.isdigit():
                continue
            if USER_RE.fullmatch(name):
                self.uids[name] = int(raw)

    def _save_uidmap(self) -> None:
        """Rewrite the map atomically; it is the only record of these UIDs."""
        self.auth.mkdir(parents=True, exist_ok=True)
        tmp = self.uidmap.with_name(self.uidmap.name + ".tmp")
        body = "".join(f"{name}:{uid}\n" for name, uid in sorted(self.uids.items()))
        tmp.write_text(body, encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.uidmap)

    @staticmethod
    def _ensure_name_entry(path: Path, name: str, uid: int) -> None:
        """Append a `name:...:uid:` entry to /etc/passwd or /etc/group if absent.

        `subprocess` only needs the numeric UID to drop privileges, but tools the
        agent runs inside the workspace (`git commit`, `id`, `whoami`) resolve
        the current UID's *name* through these files. Without an entry they fail
        with "unable to look up current user in the passwd file: no such user".
        """
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            return
        entry_name = f"dsh-{name}"
        # Name-based check (not `:uid:`): a UID can legitimately appear as
        # *someone else's* GID field, and skipping on that would leave this user
        # with no resolvable name at all.
        for line in existing.splitlines():
            if line.split(":", 1)[0] == entry_name:
                return
        if f":{uid}:" in existing:
            print(
                f"provisioner: UID {uid} for {name} already appears in {path}; "
                f"not adding {entry_name}",
                flush=True,
            )
            return
        if path.name == "passwd":
            line = f"{entry_name}:x:{uid}:{uid}:DSH user {name}:/data/users/{name}:/bin/bash\n"
        else:
            line = f"{entry_name}:x:{uid}:\n"
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError as exc:
            print(f"provisioner: cannot add {path} entry for {name}: {exc}", flush=True)

    def _uid_for(self, user: str) -> int:
        """Return this user's dedicated UID, allocating one on first sight.

        `setuid` itself only needs the number, but the agent's tools resolve
        their own name through `/etc/passwd`, so a matching entry is created as
        well (see `_ensure_name_entry`). Those entries live in the container
        filesystem, not the data volume, and therefore vanish on every container
        rebuild — hence the (re)create on the cached path too.

        Caller must already hold `self.lock` (this is only reached from
        `ensure`, which is serialized precisely so two cold-start requests for
        different users cannot race the UID allocation).
        """
        if not self.uidmap_loaded:
            self._load_uidmap()
        existing = self.uids.get(user)
        if existing is not None:
            self._ensure_name_entry(Path("/etc/passwd"), user, existing)
            self._ensure_name_entry(Path("/etc/group"), user, existing)
            return existing
        used = set(self.uids.values())
        candidate = BASE_UID
        while candidate in used:
            candidate += 1
            if candidate >= UID_CEILING:
                raise RuntimeError(
                    f"ran out of UIDs between {BASE_UID} and {UID_CEILING} "
                    f"while allocating one for {user!r}"
                )
        self.uids[user] = candidate
        self._save_uidmap()
        self._ensure_name_entry(Path("/etc/passwd"), user, candidate)
        self._ensure_name_entry(Path("/etc/group"), user, candidate)
        return candidate

    @staticmethod
    def _chown_tree(path: Path, uid: int, gid: int) -> None:
        """Recursively hand `path` to `uid:gid`, without following symlinks.

        Used once per user to adopt files an earlier deployment created while
        every `dsh web` still ran as root. Later files are created by the user's
        own process, so their ownership is already correct.
        """
        os.chown(path, uid, gid, follow_symlinks=False)
        for root, dirs, files in os.walk(path):
            for name in (*dirs, *files):
                try:
                    os.chown(os.path.join(root, name), uid, gid, follow_symlinks=False)
                except OSError:
                    # A concurrent writer racing this walk is not fatal: the new
                    # file belongs to the user anyway.
                    continue

    @staticmethod
    def _prepare_dir(path: Path, uid: int, gid: int) -> None:
        """Create `path` as a real directory and give it to `uid`, safely.

        `path` lives inside a directory the user themselves owns, so on every
        request after the first they could have replaced it with a symlink
        pointing somewhere privileged. `lstat` + `O_NOFOLLOW` are therefore
        mandatory here: a naive `chown`/`chmod` would follow that link and hand
        (or lock down) e.g. `/etc` on the user's behalf.
        """
        # exist_ok absorbs the race with a concurrent ensure() for the same user
        # (the two-lock split lets two requests share this path). A *symlink*
        # already sitting there is not a directory as far as `mkdir` sees it,
        # and `mkdir` on a symlink raises FileExistsError, so the loop below
        # removes it and recreates a real directory.
        for attempt in range(4):
            try:
                st = path.lstat()
            except FileNotFoundError:
                st = None
            if st is not None and stat.S_ISDIR(st.st_mode):
                break
            if attempt == 3:
                # Three rounds of "create/replace then look again" all landed on
                # something that is not a directory. Refusing is safer than
                # guessing which inode we are about to chown.
                raise RuntimeError(f"could not establish {path} as a directory")
            if st is None:
                try:
                    path.mkdir(parents=True, exist_ok=True)
                except FileExistsError:
                    # Raced with a concurrent create/replace; look again.
                    pass
                continue
            # Regular file or symlink dropped in the way — remove and recreate.
            try:
                path.unlink()
            except OSError:
                raise
            try:
                path.mkdir(parents=True, exist_ok=True)
            except FileExistsError:
                pass
        # fchown/fchmod on an O_NOFOLLOW|O_DIRECTORY fd cannot be redirected by
        # a symlink swap between the check above and this call.
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchown(fd, uid, gid)
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)

    def _own_user_tree(self, path: Path, uid: int, gid: int) -> None:
        """Adopt `path` for the user, skipping the walk once it already matches."""
        try:
            current = path.lstat()
        except OSError:
            return
        if current.st_uid != uid or current.st_gid != gid or not stat.S_ISDIR(
            current.st_mode
        ):
            self._chown_tree(path, uid, gid)
        self._prepare_dir(path, uid, gid)

    @staticmethod
    def _clear_if_not_regular(path: Path) -> None:
        """Unlink `path` when it is a symlink or anything that is not a plain file.

        Everything below a user's own directory is writable by that user, so on
        any request after the first they can replace `dsh.log`/`settings.yaml`
        with a symlink to `/etc/passwd`. The provisioner still runs as root at
        that point, so opening such a path normally would let them redirect a
        root write anywhere on the filesystem.
        """
        try:
            st = path.lstat()
        except FileNotFoundError:
            return
        except OSError:
            return
        if stat.S_ISREG(st.st_mode):
            return
        try:
            if stat.S_ISDIR(st.st_mode):
                # A directory in a file's place (e.g. the user ran `mkdir
                # settings.yaml`) cannot be unlinked; drop the whole thing.
                # `rmtree` refuses symlinks, so this never follows a link.
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError:
            pass

    def _write_user_file(self, path: Path, uid: int, gid: int, text: str) -> None:
        """Write `text` to `path` as a real, user-owned 0600 regular file."""
        self._clear_if_not_regular(path)
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.fchown(fd, uid, gid)
            os.fchmod(fd, 0o600)
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)

    @classmethod
    def _read_user_file(cls, path: Path) -> bytes:
        """Read a user-owned regular file without following a symlink.

        The same hazard as `_write_user_file`: the file sits below a directory
        the user owns, so between `_clear_if_not_regular` and the open they can
        swap in a symlink to e.g. `/etc/shadow`. Root-side opens therefore also
        need `O_NOFOLLOW`.
        """
        with cls._open_user_file_ro(path) as handle:
            return handle.read()

    @classmethod
    def _open_user_file_ro(cls, path: Path) -> BinaryIO:
        """Open a user-owned regular file read-only, never following a symlink."""
        cls._clear_if_not_regular(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise OSError(f"{path} is not a regular file")
        return os.fdopen(fd, "rb")

    def _seed_npmrc(self, user_root: Path, uid: int, gid: int) -> None:
        """Copy the build-time `.npmrc` into the user's HOME if it is missing.

        Without it, any runtime `pnpm` the agent runs (installing plugins) falls
        back to the default registry and loses the mirror/retry tuning, which on
        the target networks manifests as the same `error (23)` storm the build
        stage hit. Only seeded when absent, so a user can still override it.
        """
        target = user_root / ".npmrc"
        if target.is_symlink() or target.exists():
            return
        try:
            body = Path(NPMRC_TEMPLATE).read_text(encoding="utf-8")
        except OSError:
            return
        try:
            self._write_user_file(target, uid, gid, body)
        except OSError as exc:
            print(f"provisioner: cannot seed {target}: {exc}", flush=True)

    def _open_log(self, path: Path, uid: int, gid: int) -> BinaryIO:
        """Open the per-user `dsh.log` for append without following a symlink."""
        self._clear_if_not_regular(path)
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o600,
        )
        os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "ab")

    @staticmethod
    def _listening(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.05)
            return sock.connect_ex(("127.0.0.1", port)) == 0

    @staticmethod
    def _serving(port: int) -> bool:
        """Whether the user's DSH instance is answering HTTP yet.

        TCP-listening alone is not enough: DSH binds the socket before its
        frontend fallback route is registered, and until then every request
        (including `/`) gets a bare 404. `?token=` absent means the index
        route answers 401 once it is mounted, so 200/303/401 all prove that
        the web app has finished mounting.
        """
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request(
                "GET",
                "/",
                headers={"Host": "127.0.0.1", "Connection": "close"},
            )
            response = connection.getresponse()
            response.read()
            # 401 = frontend-static is mounted and asking for the launch token,
            # which is exactly the ready state we are waiting for. 404 means the
            # fallback seat is still empty, so the page would not load yet.
            return response.status in (200, 303, 401)
        except (OSError, http.client.HTTPException):
            return False
        finally:
            connection.close()

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
        user_root = self.users / user
        dsh_home = user_root / ".dsh"
        workspace = self.workspaces / user
        spawned: (
            tuple[subprocess.Popen[bytes], int, Path, threading.Event, int] | None
        ) = None
        with self.lock:
            uid = self._uid_for(user)
            gid = uid
            # 这三个目录的父目录要么是 root 独占的（self.users、/workspaces），
            # 要么就是用户自己的 0700 目录 —— 后者里的 `.dsh` 可以被用户换成
            # 指向任意位置的符号链接，所以一律走 _prepare_dir（lstat + O_NOFOLLOW），
            # 绝不用会跟随链接的 mkdir。
            self._prepare_dir(user_root, uid, gid)
            self._prepare_dir(dsh_home, uid, gid)
            self._prepare_dir(workspace, uid, gid)
            if user not in self.adopted_users:
                self._chown_tree(user_root, uid, gid)
                self.adopted_users.add(user)

            settings = dsh_home / "settings.yaml"
            # Regenerate when missing, when an earlier template renderer
            # corrupted it, or when it predates the `agent-default-model`
            # override (see the marker comments above). Without this self-heal,
            # the copy already inside the data volume would keep its old
            # contents across image rebuilds — either crash-looping `dsh web` or
            # sending new sessions to the `deepseek-official` provider.
            self._clear_if_not_regular(settings)
            try:
                existing = self._read_user_file(settings).decode("utf-8")
            except OSError:
                existing = None
            settings_needs_rewrite = (
                existing is None
                or BROKEN_SETTINGS_MARKER in (existing or "")
                or STALE_SETTINGS_MARKER not in (existing or "")
                or SETTINGS_TEMPLATE_MARKER not in (existing or "")
            )
            if settings_needs_rewrite:
                rendered = render_settings(self.template.read_text(encoding="utf-8"))
                self._write_user_file(settings, uid, gid, rendered)

            # home 级补丁层：把镜像里的模板写进用户的 `$DSH_HOME/cordis.patch.yml`。
            # 与 settings.yaml 一样放进数据卷、跨镜像重建保留，所以也做陈旧自愈：
            # 模板升级后（特征串变化）覆盖重写，保证用户拿到的是当前版本。
            patch_target = dsh_home / "cordis.patch.yml"
            self._clear_if_not_regular(patch_target)
            try:
                existing_patch = self._read_user_file(patch_target).decode("utf-8")
            except OSError:
                existing_patch = None
            if existing_patch is None or PATCH_MARKER not in existing_patch:
                self._write_user_file(patch_target, uid, gid, _read_home_patch())

        # 降权运行前必须先把用户目录交还给它自己：以 root 创建的文件如果不是
        # 该 UID 所有，降权后的 `dsh web` 连自己的 settings/日志目录都读不了。
        # 放在锁外：首次对已有大量文件的用户做递归 chown 可能很慢，不能因此
        # 把其他用户一起卡住；这里只是幂等的归属修正，并发执行也无害。
        self._own_user_tree(user_root, uid, gid)
        # .dsh 里有用户自己的凭据（.credentials.yaml），单独再收紧一次：
        # user_root 本身已是 0700，这里是纵深防御，防止将来有人放宽上面那层。
        self._prepare_dir(dsh_home, uid, gid)
        self._seed_npmrc(user_root, uid, gid)
        self._own_user_tree(workspace, uid, gid)

        with self.lock:
            ready = self.ready.get(user)
            if ready is None:
                ready = threading.Event()
                self.ready[user] = ready

            process = self.processes.get(user)
            if process is None or process.poll() is not None:
                ready.clear()
                # A replacement process re-announces its own launch token; drop
                # both caches so the next exchange cannot reuse a stale token or
                # a cookie minted against the previous process's secret.
                self.tokens.pop(user, None)
                self.cookies.pop(user, None)
                self.cookie_minted_at.pop(user, None)
                port = self._port(user)
                log = user_root / "dsh.log"
                # Only the bytes this process appends may be scanned for the
                # launch URL: the log survives restarts, so an earlier run's
                # token line would otherwise be mistaken for the live token.
                self._clear_if_not_regular(log)
                log_offset = log.stat().st_size if log.exists() else 0
                # 每个用户一个私有 TMPDIR，直接放在他自己的 0700 目录里：
                # 既避免用户间通过共享 /tmp 窥探或抢占符号链接，也让 tsx /
                # Node 的临时文件落在该用户自己的目录里。
                tmpdir = user_root / "tmp"
                self._prepare_dir(tmpdir, uid, gid)
                env = os.environ.copy()
                env.update(
                    {
                        "HOME": str(user_root),
                        "DSH_HOME": str(dsh_home),
                        "TMPDIR": str(tmpdir),
                        "XDG_CACHE_HOME": str(user_root / ".cache"),
                        "XDG_CONFIG_HOME": str(user_root / ".config"),
                        "XDG_DATA_HOME": str(user_root / ".local" / "share"),
                        "XDG_STATE_HOME": str(user_root / ".local" / "state"),
                        # tsx 默认把转译缓存写进 node_modules/.cache，而 /opt/dsh
                        # 属 root、降权后不可写。禁用它的磁盘缓存，改为内存转译；
                        # 这个变量在不认识它的版本里只是被忽略，无副作用。
                        "TSX_DISABLE_CACHE": "1",
                    }
                )
                # searxng MCP 的 Bearer token：home 补丁里用
                # `!!js process.env.MCP_SEARXNG_TOKEN` 读它，所以必须出现在
                # 用户进程的环境里。它由 compose 从 .env 传入（服务端内部凭据，
                # 不是每个用户自己的 key）。
                if os.environ.get("MCP_SEARXNG_TOKEN"):
                    env["MCP_SEARXNG_TOKEN"] = os.environ["MCP_SEARXNG_TOKEN"]
                # 思考强度线协议；home patch 不读它，但让用户进程也带上，
                # 便于 `!!js` 覆盖或排障时保持与非补丁路径一致。
                env["DSH_MODEL_REASONING_FORMAT"] = MODEL_REASONING_FORMAT
                log_handle = self._open_log(log, uid, gid)
                popen_kwargs: dict[str, object] = {
                    # 进程 cwd 决定两件事，都必须指向用户自己的目录而不是只读的
                    # 安装树 `/opt/dsh`：
                    #   1) 新会话的默认 workspace —— 官方
                    #      `packages/api/session-controller/src/index.ts` 用
                    #      `process.cwd()` 建 SessionCommandController，
                    #      `packages/bundle/base/cordis.patch.yml` 的
                    #      `sandbox-policy.config.workspaceRoot` 也读它；
                    #   2) DSH 的项目级 `.env` 发现路径（cwd/.env）。
                    # 指到 `/opt/dsh` 会让 Agent 默认在只读源码树里工作，用户
                    # 还得手动注册 workspace；这里改成 `/workspaces/<user>`，
                    # 该目录已 chown 给本用户 UID 且 0700。
                    "cwd": str(workspace),
                    "env": env,
                    "stdout": log_handle,
                    "stderr": subprocess.STDOUT,
                    "start_new_session": True,
                    # 该用户进程创建的新文件默认 0600/0700，不用每个 umask 调用
                    # 去操心；用户之间要互相传文件时应显式 chmod。
                    "umask": 0o077,
                }
                if ISOLATE_USERS:
                    # CPython 在 fork 后 exec 前依次 setgroups/setgid/setuid，
                    # extra_groups=[] 会清空 root 的附加组（尤其是 www-data，
                    # 否则能读到 /etc/dsh-auth/htpasswd）。
                    popen_kwargs.update({"user": uid, "group": gid, "extra_groups": []})
                process = subprocess.Popen(
                    [
                        *DSH_WEB_COMMAND,
                        "web",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--trusted-host",
                        "127.0.0.1",
                        "--no-open",
                    ],
                    **popen_kwargs,  # type: ignore[arg-type]
                )
                # 子进程已经 dup 了这份 stdout，父进程的副本必须关掉，否则每个
                # 用户就泄漏一个 fd（进程长期运行，用户多了会撞 ulimit）。
                log_handle.close()
                self.processes[user] = process
                spawned = (process, port, log, ready, log_offset)
            port = self._port(user)
            is_ready = ready.is_set()

        # Wait outside the lock: a slow first start must not block other users.
        if spawned is not None:
            process, port, log, ready, log_offset = spawned
            try:
                self._await_startup(user, process, port, log, log_offset)
            except RuntimeError:
                # Drop the dead handle so the next request retries the spawn
                # instead of reusing a Popen that already exited.
                with self.lock:
                    if self.processes.get(user) is process:
                        self.processes.pop(user, None)
                    ready.clear()
                raise
            ready.set()
        elif not is_ready and ready is not None:
            # Another request is already starting this user's instance; wait for
            # it rather than proxying to a port that may not be listening yet.
            if not ready.wait(STARTUP_TIMEOUT_S):
                raise RuntimeError(
                    f"timed out waiting for another request to finish starting "
                    f"dsh web for {user}"
                )
            with self.lock:
                if self.processes.get(user) is None:
                    raise RuntimeError(f"dsh web for {user} failed to start")

        # Reused running instances (and restarts of this provisioner) still need
        # the token exchanged once; both lookups are idempotent.
        self._ensure_token(user, self.users / user / "dsh.log")
        self._ensure_cookie(user, port)
        return port

    def _ensure_token(self, user: str, log: Path) -> str:
        """Return the live launch token, recovering it from the log if needed."""
        token = self.tokens.get(user)
        if token is not None:
            return token
        # A reused instance started before this process: its URL line is
        # somewhere in the existing log, so scan it from the beginning.
        try:
            text = self._read_user_file(log).decode("utf-8", "replace")
        except OSError as exc:
            raise RuntimeError(f"cannot read {log} to recover the DSH launch token: {exc}")
        matches = LAUNCH_URL_RE.findall(text)
        if not matches:
            raise RuntimeError(
                f"no DSH launch token found in {log}; restart the container so a "
                f"fresh `dsh web` can announce itself"
            )
        token = matches[-1]
        with self.lock:
            self.tokens[user] = token
        return token

    def _ensure_cookie(self, user: str, port: int) -> str:
        """Return a fresh DSH browser-session cookie for this user's instance.

        Minted on first use and then re-minted once it is older than
        COOKIE_REFRESH_S, so a long-lived container does not hit DSH's hard
        cookie expiry (default 30 days) with a cookie it can no longer refresh.
        """
        cookie = self.cookies.get(user)
        minted_at = self.cookie_minted_at.get(user)
        if cookie is not None and minted_at is not None \
                and time.monotonic() - minted_at < COOKIE_REFRESH_S:
            return cookie
        token = self._ensure_token(user, self.users / user / "dsh.log")
        cookie = self._exchange_session(port, token)
        with self.lock:
            self.cookies[user] = cookie
            self.cookie_minted_at[user] = time.monotonic()
        return cookie

    @staticmethod
    def _exchange_session(port: int, token: str) -> str:
        """Trade the launch token for DSH's signed session cookie.

        DSH mints the cookie only for `GET /?token=<one token>` whose Host is the
        authority the cookie gets bound to. Proxied traffic reaches DSH with
        `Host: 127.0.0.1`, so the exchange is issued with that same authority and
        the resulting cookie matches every later request.
        """
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request(
                "GET",
                f"/?token={token}",
                headers={"Host": "127.0.0.1", "Connection": "close"},
            )
            response = connection.getresponse()
            response.read()
            if response.status != 303:
                raise RuntimeError(
                    f"DSH launch-token exchange returned {response.status}, expected 303"
                )
            set_cookie = response.getheader("set-cookie") or ""
        except (OSError, http.client.HTTPException) as exc:
            raise RuntimeError(f"DSH launch-token exchange failed: {exc}")
        finally:
            connection.close()
        # Send back only the `name=value` pair; attributes are re-decided here.
        pair = set_cookie.split(";", 1)[0].strip()
        if "=" not in pair or pair.startswith("="):
            raise RuntimeError(
                f"DSH launch-token exchange returned no session cookie: {set_cookie!r}"
            )
        return pair

    @staticmethod
    def _tail(path: Path, limit: int = 2000) -> str:
        try:
            data = Provisioner._read_user_file(path)
        except OSError:
            return ""
        return data[-limit:].decode("utf-8", "replace").strip()

    def _await_startup(
        self,
        user: str,
        process: subprocess.Popen[bytes],
        port: int,
        log: Path,
        log_offset: int,
    ) -> None:
        """Block until the spawned DSH instance is serving and its token is known.

        Raises RuntimeError with the tail of the user's dsh.log so the failure
        is visible in the container log instead of becoming an opaque 500.
        """
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        token_deadline: float | None = None
        while time.monotonic() < deadline:
            if self._listening(port) and self._serving(port):
                token = self._launch_token(log, log_offset)
                if token is not None:
                    with self.lock:
                        self.tokens[user] = token
                    return
                # Serving but not announced yet: the URL line trails Loader
                # settlement. A request answered in that window cannot mint the
                # browser cookie, so keep waiting instead of reporting ready.
                if token_deadline is None:
                    token_deadline = time.monotonic() + TOKEN_WAIT_S
                elif time.monotonic() >= token_deadline:
                    raise RuntimeError(
                        f"dsh web for {user} answers on 127.0.0.1:{port} but printed "
                        f"no launch token within {TOKEN_WAIT_S:.0f}s; last log lines: "
                        f"{self._tail(log)}"
                    )
            if process.poll() is not None:
                raise RuntimeError(
                    f"dsh web for {user} exited with code {process.returncode}; "
                    f"last log lines: {self._tail(log)}"
                )
            time.sleep(STARTUP_POLL_S)
        raise RuntimeError(
            f"dsh web for {user} did not listen on 127.0.0.1:{port} within "
            f"{STARTUP_TIMEOUT_S:.0f}s; last log lines: {self._tail(log)}"
        )

    @staticmethod
    def _launch_token(log: Path, offset: int) -> str | None:
        """Extract this process's `?token=` value from the log bytes it appended."""
        # Same symlink hazard as every other read below a user-owned directory:
        # never let a user-replaced symlink redirect this open.
        try:
            with Provisioner._open_user_file_ro(log) as handle:
                handle.seek(offset)
                recent = handle.read(LOG_SCAN_BYTES)
        except OSError:
            return None
        match = LAUNCH_URL_RE.search(recent.decode("utf-8", "replace"))
        return None if match is None else match.group(1)


PROVISIONER = Provisioner()


class Handler(BaseHTTPRequestHandler):
    server_version = "dsh-provisioner/1"

    def do_GET(self) -> None:  # noqa: N802
        user = self.headers.get("X-Remote-User", "")
        try:
            port = PROVISIONER.ensure(user)
        except (RuntimeError, ValueError, OSError) as exc:
            # Nginx's auth_request discards this body and reports a bare 500 to
            # the browser, so the reason must reach the container log here.
            print(f"provisioner: provision failed for user={user!r}: {exc}", flush=True)
            traceback.print_exc()
            self.send_error(500, str(exc))
            return
        self.send_response(204)
        self.send_header("X-DSH-Port", str(port))
        # Nginx injects this cookie on the proxied request, which is what makes a
        # browser with no DSH session of its own load the page successfully.
        cookie = PROVISIONER.cookies.get(user)
        if cookie is not None:
            self.send_header("X-DSH-Cookie", cookie)
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        print("provisioner:", fmt % args, flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 3090), Handler).serve_forever()
