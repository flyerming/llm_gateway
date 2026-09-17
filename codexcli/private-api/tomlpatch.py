"""Line-preserving TOML editor.

WHY NOT A TOML LIBRARY
----------------------
Python's stdlib has `tomllib` for reading but nothing for writing, and every
write-library reformats the whole document. `~/.codex/config.toml` is a file the
user maintains by hand and that Codex rewrites on its own -- it holds `[projects.'...']`
tables with Windows backslash paths in literal strings, `[mcp_servers.*]` blocks,
and comments. Re-emitting it from a parsed tree risks mangling all of that.

So this editor only ever touches the specific lines it is asked to change:
  * `set_top(key, value)`   -- a `key = value` line before the first `[table]`
  * `set_table(header, ...)`-- key/value lines inside one named `[table]`
  * `remove_table(header)`  -- drop a whole `[table]` block

Every other byte of the file is copied through untouched.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_HEADER = re.compile(r"^\s*\[\[?([^\]]+)\]\]?\s*(?:#.*)?$")


def _render(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        # Prefer a literal ('...') string when there are no single quotes or
        # control characters -- that is the convention this file already uses for
        # Windows paths, e.g. [projects.'e:\推理加速-2026\...'].
        if "'" not in value and "\\" in value and "\n" not in value:
            return f"'{value}'"
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise TypeError(f"cannot render {type(value).__name__} as TOML")


def _is_header(line: str) -> str | None:
    m = _HEADER.match(line)
    return m.group(1).strip() if m else None


class TomlFile:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.exists():
            text = self.path.read_text(encoding="utf-8")
        else:
            text = ""
        self.trailing_newline = (not text) or text.endswith("\n")
        self.lines: list[str] = text.splitlines()
        self.header: str | None = None
        self._parse()

    # ------------------------------------------------------------------ #

    def _parse(self) -> None:
        """Record which `[table]` each line belongs to."""
        self.owner: list[str | None] = []
        current: str | None = None
        for line in self.lines:
            h = _is_header(line)
            if h is not None and not line.lstrip().startswith("#"):
                current = h
            self.owner.append(current)

    def _reparse(self) -> None:
        self._parse()

    def _blocks(self) -> list[tuple[str | None, int, int]]:
        """(header, start, end) line ranges; `end` exclusive."""
        out: list[tuple[str | None, int, int]] = []
        start = 0
        for i in range(1, len(self.lines)):
            h = _is_header(self.lines[i])
            if h is not None and not self.lines[i].lstrip().startswith("#"):
                out.append((self.owner[start] if start < len(self.owner) else None, start, i))
                start = i
        out.append((self.owner[start] if start < len(self.owner) else None, start, len(self.lines)))
        return out

    @staticmethod
    def _key_of(line: str) -> str | None:
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("["):
            return None
        if "=" not in s:
            return None
        return s.split("=", 1)[0].strip().strip('"\'')

    # ------------------------------------------------------------------ #
    # public API

    def get(self, key: str, table: str | None = None) -> str | None:
        for i, line in enumerate(self.lines):
            if self.owner[i] != table:
                continue
            if self._key_of(line) == key:
                return line.split("=", 1)[1].strip()
        return None

    def set_top(self, key: str, value: Any) -> None:
        self._set(key, value, table=None)

    def set_table(self, header: str, values: dict[str, Any]) -> None:
        if self._find_block(header) is None:
            self._append_table(header)
        for k, v in values.items():
            self._set(k, v, table=header)

    def remove_top(self, key: str) -> bool:
        """Drop a preamble `key = value` line. True when there was one."""
        return self.remove_key(key, table=None)

    def remove_key(self, key: str, table: str | None = None) -> bool:
        """Drop one `key = value` line from `table` (or the preamble). True when
        there was one. Used to clear a credential field that is no longer the
        one in use -- leaving both `env_key` and `experimental_bearer_token`
        behind is ambiguous about which Codex will honour."""
        for i, line in enumerate(self.lines):
            if self.owner[i] != table:
                continue
            if self._key_of(line) == key:
                del self.lines[i]
                if (i < len(self.lines) and not self.lines[i].strip()
                        and i > 0 and not self.lines[i - 1].strip()):
                    del self.lines[i]
                self._reparse()
                return True
        return False

    def remove_table(self, header: str) -> bool:
        blk = self._find_block(header)
        if blk is None:
            return False
        _, start, end = blk
        # Also swallow one blank separator line so we do not leave a double gap.
        if end < len(self.lines) and not self.lines[end].strip():
            end += 1
        del self.lines[start:end]
        self._reparse()
        return True

    def save(self, backup: bool = True) -> Path | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        bak = None
        if backup and self.path.exists():
            bak = self.path.with_suffix(self.path.suffix + ".bak")
            # Only ever snapshot the ORIGINAL -- see the note in jsonc.JsoncFile.save.
            if not bak.exists():
                bak.write_bytes(self.path.read_bytes())
        text = "\n".join(self.lines)
        if text and self.trailing_newline:
            text += "\n"
        self.path.write_text(text, encoding="utf-8")
        return bak

    # ------------------------------------------------------------------ #

    def _find_block(self, header: str) -> tuple[str | None, int, int] | None:
        for h, s, e in self._blocks():
            if h == header:
                return h, s, e
        return None

    def _append_table(self, header: str) -> None:
        if self.lines and self.lines[-1].strip():
            self.lines.append("")
        self.lines.append(f"[{header}]")
        self._reparse()

    def _set(self, key: str, value: Any, table: str | None) -> None:
        rendered = _render(value)
        for i, line in enumerate(self.lines):
            if self.owner[i] != table:
                continue
            if self._key_of(line) == key:
                prefix = line[: len(line) - len(line.lstrip())]
                self.lines[i] = f"{prefix}{key} = {rendered}"
                return

        # Not present -- insert at the end of the owning block.
        if table is None:
            insert_at = 0
            for i, line in enumerate(self.lines):
                if self.owner[i] is None:
                    insert_at = i + 1
            self.lines.insert(insert_at, f"{key} = {rendered}")
            # Keep the new preamble key visually separated from the first table
            # header rather than glued to it.
            if insert_at + 1 < len(self.lines) and _is_header(self.lines[insert_at + 1]) is not None:
                self.lines.insert(insert_at + 1, "")
        else:
            _, _, end = self._find_block(table) or (None, len(self.lines), len(self.lines))
            while end > 0 and not self.lines[end - 1].strip():
                end -= 1
            self.lines.insert(end, f"{key} = {rendered}")
        self._reparse()
