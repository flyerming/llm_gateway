"""JSONC (JSON-with-comments) reader/writer that preserves the original formatting.

WHY
---
VSCode's `settings.json` is JSONC: it allows `//` and `/* */` comments and trailing
commas. `json.loads()` rejects all three, and the obvious workaround -- load,
mutate, `json.dump()` -- silently deletes every comment and reflows the whole file.
For a hand-maintained settings file that is a destructive edit, so this module does
text surgery instead:

  1. scan the file once, emitting a comment-free/trailing-comma-free copy of the
     text plus an index map from each kept character back to its position in the
     original,
  2. locate the value span we want to replace in that cleaned copy,
  3. map the span back through the index map and splice the new text into the
     ORIGINAL bytes.

Everything we did not touch -- comments, ordering, indentation, blank lines --
survives verbatim.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- #
# scanning
# --------------------------------------------------------------------------- #

def _scan(text: str) -> tuple[str, list[int]]:
    """Return (clean_text, index_map).

    `clean_text` has all comments removed. `index_map[i]` is the offset in `text`
    of the character that became `clean_text[i]`.
    """
    clean: list[str] = []
    idx: list[int] = []
    i, n = 0, len(text)

    while i < n:
        c = text[i]

        if c == '"':
            # Copy the whole string literal verbatim -- a `//` inside a string is
            # not a comment, and this is the entire reason we cannot regex this.
            j = _skip_string(text, i)
            clean.append(text[i:j])
            idx.extend(range(i, j))
            i = j
            continue

        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue

        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i = min(i + 2, n)
            continue

        clean.append(c)
        idx.append(i)
        i += 1

    return _drop_trailing_commas("".join(clean), idx)


def _skip_string(text: str, i: int) -> int:
    """Index just past the string literal starting at `text[i] == '"'`."""
    n = len(text)
    i += 1
    while i < n:
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i + 1
        i += 1
    return n  # unterminated; treat rest of file as the literal


def _drop_trailing_commas(clean: str, idx: list[int]) -> tuple[str, list[int]]:
    """Remove `,` immediately preceding `}` or `]` (legal in JSONC, not in JSON)."""
    out: list[str] = []
    out_idx: list[int] = []
    n = len(clean)
    for i, c in enumerate(clean):
        if c == ",":
            j = i + 1
            while j < n and clean[j] in " \t\r\n":
                j += 1
            if j < n and clean[j] in "}]":
                continue  # drop it
        out.append(c)
        out_idx.append(idx[i])
    return "".join(out), out_idx


def _skip_ws(s: str, i: int) -> int:
    n = len(s)
    while i < n and s[i] in " \t\r\n":
        i += 1
    return i


def _skip_balanced(s: str, i: int) -> int:
    """Index just past the balanced `{...}` / `[...]` starting at `s[i]`."""
    open_c = s[i]
    close_c = "}" if open_c == "{" else "]"
    depth = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == '"':
            i = _skip_string(s, i)
            continue
        if c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _value_end(s: str, i: int) -> int:
    """Index just past the JSON value starting at `s[i]`."""
    if i >= len(s):
        return i
    if s[i] == '"':
        return _skip_string(s, i)
    if s[i] in "{[":
        return _skip_balanced(s, i)
    j = i
    while j < len(s) and s[j] not in ",}]":
        j += 1
    while j > i and s[j - 1] in " \t\r\n":
        j -= 1
    return j


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #

class JsoncFile:
    """A JSONC document that can be edited without losing comments."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.original = self.path.read_text(encoding="utf-8") if self.path.exists() else "{}\n"
        self.clean, self.idx = _scan(self.original)
        self.data: Any = json.loads(self.clean) if self.clean.strip() else {}
        if not isinstance(self.data, dict):
            raise ValueError(f"{self.path}: top level is not an object")

    # -- reading ------------------------------------------------------------ #

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    # -- writing ------------------------------------------------------------ #

    def set(self, key: str, value: Any) -> None:
        """Set a top-level key, splicing the new value into the original text."""
        self.data[key] = value
        encoder = json.JSONEncoder(ensure_ascii=False, indent=2)

        member = self._find_member(key)

        if member is not None:
            kstart, vstart, vend = member
            # Nested lines align one level in from the KEY's line, not from the
            # column the value happens to start in -- otherwise a long key name
            # pushes the whole body far to the right.
            indent = self._line_indent(kstart) + " " * self._indent_step()
            body = encoder.encode(value).replace("\n", "\n" + indent)
            o_start = self.idx[vstart]
            o_end = self.idx[vend - 1] + 1
            self.original = self.original[:o_start] + body + self.original[o_end:]
        else:
            brace = self.clean.index("{")
            close = self.clean.rindex("}")
            o_pos = self.idx[brace] + 1
            root_indent = self._line_indent(brace)
            child_indent = root_indent + " " * self._indent_step()
            member = f"{json.dumps(key, ensure_ascii=False)}: " + encoder.encode(value)
            body = member.replace("\n", "\n" + child_indent)

            if not self._members():
                # Nothing to preserve -- rewrite the empty object in full rather
                # than splicing into it and leaving stray blank lines behind.
                text = "{\n" + child_indent + body + "\n" + root_indent + "}"
                self.original = (
                    self.original[: self.idx[brace]] + text + self.original[self.idx[close] + 1 :]
                )
            elif "\n" not in self.clean[brace:close]:
                # A one-line object: stay on one line, do not reflow the file.
                self.original = self.original[:o_pos] + member + ", " + self.original[o_pos:]
            else:
                self.original = (
                    self.original[:o_pos] + "\n" + child_indent + body + "," + self.original[o_pos:]
                )

        # Re-scan so a second set() sees the edits made by the first.
        self.clean, self.idx = _scan(self.original)
        self.data = json.loads(self.clean)

    def remove(self, key: str) -> bool:
        """Delete a top-level key, taking its line and separator with it."""
        member = self._find_member(key)
        self.data.pop(key, None)
        if member is None:
            return False

        kstart, _vstart, vend = member
        o_start = self.idx[kstart]
        o_end = self.idx[vend - 1] + 1

        # Pull the start back to the beginning of the line so no indentation is
        # left floating where the key used to be.
        line_start = self.original.rfind("\n", 0, o_start) + 1
        if self.original[line_start:o_start].strip() == "":
            o_start = line_start

        # Take the separator comma, then the line break.
        m = re.match(r"[ \t]*,[ \t]*", self.original[o_end:])
        if m:
            o_end += m.end()
        m = re.match(r"\r?\n", self.original[o_end:])
        o_end += m.end() if m else len(re.match(r"[ \t]*", self.original[o_end:]).group())

        text = self.original[:o_start] + self.original[o_end:]

        # If that was the last member, the previous line is left with a comma
        # pointing at the closing brace.
        if text[o_start:].lstrip()[:1] in ("}", "]"):
            i = o_start - 1
            while i >= 0 and text[i] in " \t\r\n":
                i -= 1
            if i >= 0 and text[i] == ",":
                text = text[:i] + text[i + 1:]

        self.original = text
        self.clean, self.idx = _scan(self.original)
        self.data = json.loads(self.clean) if self.clean.strip() else {}
        return True

    def save(self, backup: bool = True) -> Path | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        bak = None
        if backup and self.path.exists():
            bak = self.path.with_suffix(self.path.suffix + ".bak")
            # Only ever snapshot the ORIGINAL. Re-running would otherwise clobber
            # the pristine copy with our own previous output, and --restore would
            # stop meaning "undo everything".
            if not bak.exists():
                bak.write_bytes(self.path.read_bytes())
        self.path.write_text(self.original, encoding="utf-8")
        return bak

    # -- internals ---------------------------------------------------------- #

    def _members(self) -> list[tuple[str, int, int, int]]:
        """(key, key_start, value_start, value_end) for each top-level member."""
        s = self.clean
        out: list[tuple[str, int, int, int]] = []
        start = s.find("{")
        if start < 0:
            return out
        i = start + 1
        n = len(s)
        while i < n:
            i = _skip_ws(s, i)
            if i >= n or s[i] == "}":
                break
            if s[i] == ",":
                i += 1
                continue
            if s[i] != '"':
                i += 1
                continue
            kstart = i
            kend = _skip_string(s, i)
            key = json.loads(s[kstart:kend])
            colon = _skip_ws(s, kend)
            if colon >= n or s[colon] != ":":
                i = kend
                continue
            vstart = _skip_ws(s, colon + 1)
            vend = _value_end(s, vstart)
            out.append((key, kstart, vstart, vend))
            i = vend
        return out

    def _find_member(self, key: str) -> tuple[int, int, int] | None:
        for k, kstart, vstart, vend in self._members():
            if k == key:
                return kstart, vstart, vend
        return None

    def _find_top_level(self, key: str) -> tuple[int, int] | None:
        m = self._find_member(key)
        return (m[1], m[2]) if m else None

    def _line_indent(self, clean_index: int) -> str:
        """The leading whitespace of the original line containing `clean_index`."""
        o = self.idx[clean_index]
        line_start = self.original.rfind("\n", 0, o) + 1
        line = self.original[line_start:o]
        return line[: len(line) - len(line.lstrip())]

    def _indent_step(self) -> int:
        """How far this file indents one level -- inferred from its own siblings."""
        root = self._line_indent(self.clean.index("{"))
        for _, kstart, _, _ in self._members():
            return max(1, len(self._line_indent(kstart)) - len(root))
        return 2


def read_jsonc(path: str | Path) -> Any:
    p = Path(path)
    if not p.exists():
        return {}
    clean, _ = _scan(p.read_text(encoding="utf-8"))
    return json.loads(clean) if clean.strip() else {}
