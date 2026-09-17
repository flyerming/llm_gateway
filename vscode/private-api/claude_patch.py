"""Patch the bundled Claude Code binary so its gateway model list is not Claude-only.

THE PROBLEM
-----------
Point Claude Code at a private gateway (`ANTHROPIC_BASE_URL` + poster
`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`) and it fetches

    GET ${ANTHROPIC_BASE_URL}/v1/models?limit=1000

then throws away every model whose id does not match `/(claude|anthropic)/i`:

    j.data.data.filter((F) => /(claude|anthropic)/i.test(F.id))   // gatewayDiscovery
    c.data.data.filter((r) => /(claude|anthropic)/i.test(r.id))   // bootstrap options

The regex is hardcoded -- no env var or setting relaxes it. A gateway serving
`gpt-*`, `deepseek-*`, `qwen-*` therefore shows only the claude-named entries in
the /model picker; every other model is dropped silently.

THE PATCH
---------
`claude.exe` is a Bun single-file executable with the JS bundle embedded verbatim,
so the regex can be edited in place -- but the replacement must be EXACTLY the same
byte length, because Bun records each module's byte length and shifting bytes
corrupts the bundle.

    /(claude|anthropic)/i   ->   /.*||||||||||||||||/i
     ^^^^^^^^^^^^^^^^^^            ^^^^^^^^^^^^^^^^^^
        18 bytes                        18 bytes

`.*` followed by empty alternations matches every string, so `.test()` is always
true and both filters become no-ops. Empty alternations are legal JS regex.

NOTES
-----
* The binary is not hash-verified and carries no code signature on this path, so
  the patch survives normal launches.
* It is LOST every time the Claude Code extension updates. Re-run this script.
* Very large models land in the picker too (image endpoints etc.). Selecting one
  still fails -- restrict the gateway's model list if that is a nuisance.
* VSCode must be fully closed before install: Windows locks the image of a running
  claude.exe, and the editor reloads the extension on restart.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

# The two hardcoded discovery filters, as they appear in the bundle. The capture
# group deliberately INCLUDES the parentheses, because those two bytes are part of
# what has to be replaced to keep the length identical.
_GROUP = rb"(\(claude\|anthropic\))"
_TARGET = re.compile(rb"/" + _GROUP + rb"/([a-z]*)")
# What a patched group looks like, for idempotency + status reporting:
# `/` `.` `*` one-or-more `|` `/` flags.  (Note the `*` -- `.*||||` , not `.|...`.)
_PATCHED = re.compile(rb"/\.\*\|+/[a-z]*")


def _replacement(group: bytes) -> bytes:
    """An always-matching regex body of exactly the same length as `group`."""
    if len(group) < 2:
        raise ValueError("regex group too short to neutralise losslessly")
    return b".*" + b"|" * (len(group) - 2)


def running_processes() -> list[str]:
    """Names of processes that hold claude.exe locked right now."""
    names = ["claude.exe", "Code.exe", "Code - Insiders.exe", "Cursor.exe", "Windsurf.exe"]
    if os.name != "nt":
        names = ["claude", "code", "code-insiders", "cursor", "windsurf"]

    alive: list[str] = []
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=30).stdout
            low = out.lower()
            for n in names:
                if n.lower() in low:
                    alive.append(n)
        else:
            out = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True,
                                 timeout=30).stdout
            comms = {line.strip().lower() for line in out.splitlines()}
            for n in names:
                if n in comms:
                    alive.append(n)
    except Exception:  # noqa: BLE001 - a failed check must not block the user
        return []
    return alive


def running() -> bool:
    return "claude.exe" in running_processes() or "claude" in running_processes()


def status(bin_path: Path) -> str:
    """ORIGINAL | PATCHED | UNKNOWN, with a hit count for diagnostics."""
    data = bin_path.read_bytes()
    orig_hits = len(_TARGET.findall(data))

    if orig_hits == 2:
        return "ORIGINAL"
    if orig_hits == 0 and _PATCHED.search(data):
        return "PATCHED"
    return f"UNKNOWN (unpatched filters found: {orig_hits}, expected 2)"


def build_patched(src: Path) -> tuple[Path, int, int]:
    """Write `<src>.patched`. Returns (path, occurrences, bytes_changed)."""
    data = src.read_bytes()
    n = len(_TARGET.findall(data))
    if n == 0:
        raise SystemExit(
            "No `/(claude|anthropic)/i` filter found in this binary.\n"
            "Either it is already patched, or the upstream bundle changed shape --\n"
            "check what `claude_patch.py --check` reports and inspect the binary."
        )

    patched = _TARGET.sub(lambda m: b"/" + _replacement(m.group(1)) + b"/" + m.group(2), data)
    if len(patched) != len(data):
        raise SystemExit(
            f"internal error: patch changed the file size ({len(data)} -> {len(patched)}).\n"
            "Refusing to write -- Bun would fail to load the bundle."
        )

    out = src.with_suffix(src.suffix + ".patched")
    out.write_bytes(patched)
    changed = sum(1 for a, b in zip(data, patched) if a != b)
    return out, n, changed


def install(src: Path, patched: Path) -> Path:
    """Back up `src` and move the patched build into place."""
    bak = src.with_suffix(src.suffix + ".bak")
    if not bak.exists():
        shutil.copy2(src, bak)
    shutil.move(str(patched), str(src))
    return bak


def restore(src: Path) -> bool:
    bak = src.with_suffix(src.suffix + ".bak")
    if not bak.exists():
        return False
    shutil.copy2(bak, src)
    bak.unlink()
    return True


def write_swap_scripts(src: Path) -> tuple[Path, Path]:
    """Emit double-clickable .bat helpers so no Python is needed to finish the swap.

    Handy when the toolkit folder is not on the colleague's machine and all they
    received was the `.patched` file next to their extension.
    """
    d = src.parent
    swap = d / "swap_claude_binary.bat"
    restore_bat = d / "restore_claude_binary.bat"

    swap.write_text(
        "@echo off\r\nsetlocal\r\n"
        f'set "BIN={src}"\r\n'
        f'set "PATCHED={src}.patched"\r\n'
        f'set "BAK={src}.bak"\r\n'
        'tasklist /FI "IMAGENAME eq claude.exe" 2>nul | find /I "claude.exe" >nul\r\n'
        "if not errorlevel 1 (\r\n"
        "    echo ERROR: claude.exe is still running.\r\n"
        "    echo Quit VSCode COMPLETELY, then run this again.\r\n"
        "    pause & exit /b 1\r\n)\r\n"
        'if not exist "%PATCHED%" ( echo ERROR: %PATCHED% missing. & pause & exit /b 1 )\r\n'
        'if not exist "%BAK%" (\r\n'
        "    echo Backing up original -^> claude.exe.bak\r\n"
        '    copy /Y "%BIN%" "%BAK%" >nul || ( echo ERROR: backup failed. & pause & exit /b 1 )\r\n'
        ")\r\n"
        'move /Y "%PATCHED%" "%BIN%" >nul || ( echo ERROR: swap failed. & pause & exit /b 1 )\r\n'
        "echo Done. Reopen VSCode.\r\npause\r\n",
        encoding="utf-8",
    )

    restore_bat.write_text(
        "@echo off\r\nsetlocal\r\n"
        f'set "BIN={src}"\r\n'
        f'set "BAK={src}.bak"\r\n'
        'tasklist /FI "IMAGENAME eq claude.exe" 2>nul | find /I "claude.exe" >nul\r\n'
        "if not errorlevel 1 (\r\n"
        "    echo ERROR: claude.exe is still running.\r\n"
        "    echo Quit VSCode COMPLETELY, then run this again.\r\n"
        "    pause & exit /b 1\r\n)\r\n"
        'if not exist "%BAK%" ( echo ERROR: backup not found. & pause & exit /b 1 )\r\n'
        'copy /Y "%BAK%" "%BIN%" >nul || ( echo ERROR: restore failed. & pause & exit /b 1 )\r\n'
        'del /Q "%BAK%"\r\necho Original restored.\r\npause\r\n',
        encoding="utf-8",
    )
    return swap, restore_bat
