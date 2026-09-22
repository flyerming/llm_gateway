"""Locate the config files each client actually reads, without being told where.

Users configuring this by hand do not know whether their settings live in
`%APPDATA%\\Code\\User\\settings.json`, `~/.claude/settings.json`, or somewhere
under a VSCode fork they installed two years ago. So we probe every location the
relevant client is documented to read, keep only the ones that exist, and rank
them by which the client consults last (i.e. which wins).
"""

from __future__ import annotations

import glob
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home())


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or home() / "AppData/Roaming")


# --------------------------------------------------------------------------- #
# editor / IDE settings
# --------------------------------------------------------------------------- #

# VSCode and the forks that can host the Claude Code extension. The folder name
# under %APPDATA% (Windows) / ~/Library/Application Support (macOS) / ~/.config
# (Linux) differs per fork, and Windows stores it under Roaming while Linux and
# macOS use a different root entirely.
_EDITOR_DIRS = (
    "Code",
    "Code - Insiders",
    "VSCodium",
    "Cursor",
    "Windsurf",
    "Trae",
    "Trae CN",
)


def editor_user_settings() -> list[Path]:
    """Existing per-user settings.json files, best candidate first."""
    found: list[Path] = []

    if sys.platform == "win32":
        roots = [_appdata()]
    elif sys.platform == "darwin":
        roots = [home() / "Library/Application Support"]
    else:
        roots = [Path(os.environ.get("XDG_CONFIG_HOME", home() / ".config"))]

    for root in roots:
        for name in _EDITOR_DIRS:
            p = root / name / "User" / "settings.json"
            if p.exists():
                found.append(p)

    # Remote-SSH / devcontainer: the extension runs server-side and reads the
    # machine-scoped settings file instead of the user one.
    for pat in (
        "~/.vscode-server/data/Machine/settings.json",
        "~/.vscode-server-insiders/data/Machine/settings.json",
        "~/.vscode-remote/data/Machine/settings.json",
    ):
        for p in glob.glob(str(home() / pat.lstrip("~/"))):
            found.append(Path(p))

    # Rank: the most recently touched file is the editor the user is actually in.
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found


# Extension root under $HOME -> the editor's per-user config directory name.
# `.vscode-server` is absent on purpose: a remote server keeps its settings in
# the Machine scope, not User, so it has no per-user settings.json to create.
_EXT_ROOT_TO_EDITOR = {
    ".vscode": "Code",
    ".vscode-insiders": "Code - Insiders",
    ".cursor": "Cursor",
    ".windsurf": "Windsurf",
}


def editor_user_settings_fallback() -> Path | None:
    """Where to create `settings.json` when no editor has one yet.

    `editor_user_settings()` only reports files that exist -- right for telling
    the user what is there, wrong for a brand-new VSCode profile where the User
    directory exists but nothing has been saved into it yet. Without this, a
    fresh machine would get no `claudeCode.environmentVariables` at all.
    """
    if sys.platform == "win32":
        root = _appdata()
    elif sys.platform == "darwin":
        root = home() / "Library/Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", home() / ".config"))

    # Prefer the editor that actually has the Claude Code extension installed.
    for ext_root, name in _EXT_ROOT_TO_EDITOR.items():
        if not (home() / ext_root / "extensions").is_dir():
            continue
        d = root / name / "User"
        if d.is_dir():
            return d / "settings.json"

    cands = [root / n / "User" / "settings.json" for n in _EDITOR_DIRS
             if (root / n / "User").is_dir()]
    if cands:
        cands.sort(key=lambda p: p.parent.stat().st_mtime, reverse=True)
        return cands[0]
    return None


def workspace_settings(start: Path | None = None) -> list[Path]:
    """`.vscode/settings.json` files from `start` up to the filesystem root."""
    out: list[Path] = []
    cur = (start or Path.cwd()).resolve()
    for parent in [cur, *cur.parents]:
        p = parent / ".vscode" / "settings.json"
        if p.exists():
            out.append(p)
    return out


# --------------------------------------------------------------------------- #
# Claude Code
# --------------------------------------------------------------------------- #

def claude_user_settings() -> Path:
    """`~/.claude/settings.json`, whether or not it exists yet.

    `claude_cli_settings()` only reports files that are already on disk, which is
    right for detection but wrong for writing: on a machine that has never run
    Claude Code the file is absent, and skipping it would silently drop both the
    `env` block and the `modelPicker` list. The parent directory is created on
    save.
    """
    return home() / ".claude" / "settings.json"


def claude_cli_settings() -> list[Path]:
    """`~/.claude/settings.json`, plus any project-scoped `.claude/settings.json`."""
    out: list[Path] = []
    p = claude_user_settings()
    if p.exists():
        out.append(p)
    cur = Path.cwd().resolve()
    for parent in [cur, *cur.parents]:
        q = parent / ".claude" / "settings.json"
        if q.exists():
            out.append(q)
    return out


def claude_binaries() -> list[Path]:
    """Bundled `claude` executables shipped inside the VSCode extensions."""
    names = ("claude.exe", "claude") if sys.platform == "win32" else ("claude", "claude.exe")
    patterns: list[str] = []
    for root in (".vscode", ".vscode-insiders", ".cursor", ".windsurf", ".vscode-server"):
        for name in names:
            patterns.append(f"{root}/extensions/anthropic.claude-code-*/resources/native-binary/{name}")
    patterns.append(f".claude/local/{names[0]}")

    found: list[Path] = []
    for pat in patterns:
        found.extend(Path(p) for p in glob.glob(str(home() / pat)))
    found.sort(key=lambda p: (p.stat().st_mtime, p.stat().st_size), reverse=True)
    return found


def claude_extension_dirs() -> list[Path]:
    out: list[Path] = []
    for root in (".vscode", ".vscode-insiders", ".cursor", ".windsurf", ".vscode-server"):
        out.extend(
            Path(p) for p in glob.glob(str(home() / root / "extensions" / "anthropic.claude-code-*"))
        )
    return out


# --------------------------------------------------------------------------- #
# Codex
# --------------------------------------------------------------------------- #

def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (home() / ".codex"))


def codex_config() -> Path:
    return codex_home() / "config.toml"


def codex_auth() -> Path:
    return codex_home() / "auth.json"


def codex_models_cache() -> Path:
    """Codex's own model list, written when it signs in.

    Only OpenAI's models are read from here (their reasoning levels and input
    types are OpenAI's to declare, and the catalog we write REPLACES theirs, so
    omitting them would revoke official capability). A headless box that never
    signed in has no cache -- callers fall back to a conservative set.
    """
    return codex_home() / "models_cache.json"


def codex_binary() -> Path | None:
    """The `codex` executable -- extension-bundled copy, else whatever is on PATH."""
    import shutil

    if sys.platform == "win32":
        target = "windows-x86_64"
        exe = "codex.exe"
    elif sys.platform == "darwin":
        target = "darwin-arm64"
        exe = "codex"
    else:
        target = "linux-x86_64"
        exe = "codex"

    cands = [
        *glob.glob(str(home() / f".vscode/extensions/openai.chatgpt-*/bin/{target}/{exe}")),
        *glob.glob(str(home() / f".cursor/extensions/openai.chatgpt-*/bin/{target}/{exe}")),
        *glob.glob(str(_appdata().parent / f"Local/OpenAI/Codex/bin/*/{exe}")),
    ]
    cands = [Path(p) for p in cands if Path(p).exists()]
    if cands:
        cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return cands[0]

    on_path = shutil.which("codex")
    return Path(on_path) if on_path else None


# --------------------------------------------------------------------------- #

@dataclass
class Report:
    claude_editor_settings: list[Path] = field(default_factory=list)
    claude_workspace_settings: list[Path] = field(default_factory=list)
    claude_cli_settings: list[Path] = field(default_factory=list)
    claude_binaries: list[Path] = field(default_factory=list)
    codex_config: Path | None = None
    codex_auth: Path | None = None
    codex_binary: Path | None = None

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
        lines += rows("编辑器 settings.json", self.claude_editor_settings)
        lines += rows("工作区 settings.json", self.claude_workspace_settings)
        lines += rows("claude 命令行 settings.json", self.claude_cli_settings)
        lines += rows("claude 可执行文件", self.claude_binaries)
        lines += rows("codex config.toml", [self.codex_config] if self.codex_config else [])
        lines += rows("codex auth.json", [self.codex_auth] if self.codex_auth else [])
        lines += rows("codex 可执行文件", [self.codex_binary] if self.codex_binary else [])
        return "\n".join(lines)


def discover() -> Report:
    r = Report()
    r.claude_editor_settings = editor_user_settings()
    if not r.claude_editor_settings:
        fb = editor_user_settings_fallback()
        if fb is not None:
            r.claude_editor_settings = [fb]  # will be created
    r.claude_workspace_settings = workspace_settings()
    r.claude_cli_settings = claude_cli_settings()
    r.claude_binaries = claude_binaries()

    cfg = codex_config()
    if cfg.exists():
        r.codex_config = cfg
    elif codex_home().exists():
        r.codex_config = cfg  # will be created
    auth = codex_auth()
    if auth.exists():
        r.codex_auth = auth
    r.codex_binary = codex_binary()
    return r
