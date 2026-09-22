"""Locate the Codex CLI's config files and binary on a Linux box.

WHY THIS IS A SEPARATE FILE FROM THE VSCode ONE
-----------------------------------------------
The VSCode toolkit (`vscode/private-api/detect.py`) probes `%APPDATA%`, the
`.vscode-server` extension folders, and the `openai.chatgpt-*` bundles that ship
`codex.exe`. On a headless Linux box none of those exist: `codex` came from npm,
Homebrew, cargo, or a tarball, and the only thing that matters is `$CODEX_HOME`
plus whatever `which codex` says.

WHAT CODEX READS
----------------
  * `$CODEX_HOME/config.toml`  -- provider table, model, model_catalog_json
  * `$CODEX_HOME/auth.json`    -- the file whose PRESENCE decides "logged in"
  * `$CODEX_HOME/models_cache.json` -- the catalog Codex fetched for itself; we
                                  read it to copy OpenAI's own reasoning levels
                                  and input modalities verbatim
  * `$CODEX_HOME/private-*.json`    -- this toolkit's per-model overrides

`CODEX_HOME` defaults to `~/.codex` and is what makes the whole thing portable:
its value is honoured verbatim, including a relative one.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path


def home() -> Path:
    """`$HOME`, falling back to the passwd entry. Never `USERPROFILE`."""
    return Path(os.environ.get("HOME") or Path.home())


# --------------------------------------------------------------------------- #
# Codex
# --------------------------------------------------------------------------- #

def codex_home() -> Path:
    """`$CODEX_HOME`, else `~/.codex`. Not created here -- see `ensure_home`."""
    raw = os.environ.get("CODEX_HOME")
    if raw:
        return Path(raw).expanduser()
    return home() / ".codex"


def ensure_home() -> Path:
    """`codex_home()`, with the directory created. Codex creates it too, but we
    write into it before Codex's first run and would rather not race it."""
    d = codex_home()
    d.mkdir(parents=True, exist_ok=True)
    return d


def codex_config() -> Path:
    return codex_home() / "config.toml"


def codex_auth() -> Path:
    return codex_home() / "auth.json"


def codex_models_cache() -> Path:
    return codex_home() / "models_cache.json"


# Where a Linux codex binary actually lands, most likely first. `which` is tried
# before this list, so these are only for the case where PATH is not set up yet
# (a fresh shell, or a wrapper installed by this tool shadowing the real binary).
_CODEX_DIRS = (
    "~/.local/bin",
    "~/.npm-global/bin",
    "~/.cargo/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/home/linuxbrew/.linuxbrew/bin",
    "~/.linuxbrew/bin",
    "~/.bun/bin",
    "~/.volta/bin",
    "~/node_modules/.bin",
    "~/.codex/bin",
)

# Names the binary goes by: the npm package installs a `codex` shim, some
# tarballs unpack a `codex-x86_64-unknown-linux-musl`.
_CODEX_NAMES = ("codex", "codex-x86_64-unknown-linux-gnu", "codex-x86_64-unknown-linux-musl")


def _unshim(path: Path) -> Path:
    """Follow a wrapper to the binary it launches, when it obviously is one.

    Both installers put a thin script on PATH: npm writes a JS shim, and this
    tool itself installs a `codex` wrapper. Either can end up being what `which`
    returns, and re-wrapping our own wrapper is a loop -- so resolve one level of
    indirection through `exec`/`node` when we can see it plainly.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return path
    if len(text) > 200_000:  # a real binary, not a script
        return path

    import re
    for m in re.finditer(r"(?:exec\s+)?\"?([^\s\"']*codex[^\s\"']*)\"?", text):
        cand = m.group(1)
        if cand.startswith("-") or cand.endswith(("codex", "codex.js")):
            continue
        p = Path(cand)
        if p.is_file() and os.access(p, os.X_OK):
            return p
    return path


def codex_binary(exclude: "set[Path] | None" = None) -> Path | None:
    """The real `codex` executable, or None when it is not installed.

    `PATH` wins, because that is what the user's shell actually runs. Only when
    `which` finds nothing do we walk the well-known install directories.

    `exclude` is how `--install-wrapper` finds the binary to exec: once our
    wrapper is on PATH, `which codex` returns the wrapper itself, and wrapping
    our own wrapper is an infinite loop.
    """
    skip = set()
    for p in (exclude or ()):
        try:
            skip.add(Path(p).resolve())
        except OSError:
            continue

    def ok(p: Path) -> bool:
        if not (p.is_file() and os.access(p, os.X_OK)):
            return False
        try:
            return p.resolve() not in skip
        except OSError:
            return True

    on_path = shutil.which("codex")
    if on_path and ok(Path(on_path)):
        return _unshim(Path(on_path))

    for d in _CODEX_DIRS:
        directory = Path(d).expanduser()
        for name in _CODEX_NAMES:
            p = directory / name
            if ok(p):
                return _unshim(p)
    return None


def codex_version() -> str | None:
    """`codex --version`, or None. Used only for reporting."""
    exe = codex_binary()
    if not exe:
        return None
    import subprocess
    try:
        r = subprocess.run([str(exe), "--version"], capture_output=True,
                           text=True, timeout=30)
    except Exception:  # noqa: BLE001 - a broken binary must not break --status
        return None
    return (r.stdout or r.stderr).strip() or None


def python_binary() -> str:
    """The interpreter to bake into the installed wrapper script.

    `sys.executable` is right when this tool is run by the same interpreter the
    user will keep using. It is empty in embedded interpreters, hence the
    fallback to whatever `python3` resolves to.
    """
    return sys.executable or shutil.which("python3") or "python3"


# --------------------------------------------------------------------------- #

@dataclass
class Report:
    codex_home: Path | None = None
    codex_config: Path | None = None
    codex_auth: Path | None = None
    codex_models_cache: Path | None = None
    codex_binary: Path | None = None
    codex_version: str | None = None
    # Set by main() once --install-wrapper has run, so --status can say where
    # `codex` now resolves to.
    wrapper: Path | None = None

    def render(self) -> str:
        def rows(title: str, items: list) -> list[str]:
            if not items:
                return [f"  {title}: 未找到"]
            lines = [f"  {title}:"]
            for i in items:
                mark = "" if Path(i).exists() else "   （将会创建）"
                lines.append(f"      {i}{mark}")
            return lines

        lines = ["检测结果："]
        lines += rows("codex 主目录", [self.codex_home] if self.codex_home else [])
        lines += rows("codex config.toml", [self.codex_config] if self.codex_config else [])
        lines += rows("codex auth.json", [self.codex_auth] if self.codex_auth else [])
        lines += rows("codex models_cache.json",
                      [self.codex_models_cache] if self.codex_models_cache else [])
        lines += rows("codex 可执行文件", [self.codex_binary] if self.codex_binary else [])
        if self.codex_version:
            lines.append(f"      版本：{self.codex_version}")
            # Local import: codex.py reaches back into this module for
            # codex_home(), so a top-level import here would be a cycle.
            import codex
            ok, verdict = codex.version_note(self.codex_version)
            lines.append(f"      {'' if ok else '! '}{verdict}")
        if self.wrapper:
            lines.append(f"  codex 包装脚本：{self.wrapper}")
        env = os.environ.get("CODEX_HOME")
        lines.append(f"  CODEX_HOME 环境变量：{env if env else '（未设置 —— 使用 ~/.codex）'}")
        return "\n".join(lines)


def discover(*, probe_version: bool = True) -> Report:
    """Locate everything this toolkit touches.

    `probe_version=False` skips the `codex --version` subprocess. `--sync` runs
    on the critical path of every `codex` launch, and spawning the binary a
    second time just to read a version string is not worth the delay there --
    the version is reported by `--status` / `--setup` / `--detect` instead, and
    `run_sync` probes it only on the runs that actually rewrite the catalog.
    """
    r = Report()
    r.codex_home = codex_home()
    # Both are reported whether or not they exist: `render()` marks the missing
    # ones "(will be created)", which is what a first run needs to see.
    r.codex_config = codex_config()
    r.codex_auth = codex_auth()
    cache = codex_models_cache()
    if cache.exists():
        r.codex_models_cache = cache
    r.codex_binary = codex_binary()
    if r.codex_binary and probe_version:
        r.codex_version = codex_version()
    return r
