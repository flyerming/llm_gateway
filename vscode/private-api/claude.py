"""Point Claude Code (VSCode extension + CLI) at a private gateway.

Claude Code reads configuration from three places, and which one wins is not
obvious:

  1. `<editor>/User/settings.json`  -- the `claudeCode.environmentVariables` array.
     This is what the extension injects into the spawned CLI process.
  2. `~/.claude/settings.json`      -- the CLI's own file, `{"env": {...}}`.
     Applies to `claude` run from a terminal.
  3. a workspace `.vscode/settings.json` -- if it also defines
     `claudeCode.environmentVariables`, it REPLACES the user-level array rather
     than merging, which is the classic "I set it and nothing happened" trap.

We write 1 and 2, and warn loudly if 3 exists.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from detect import home
from jsonc import JsoncFile, read_jsonc

if TYPE_CHECKING:  # pragma: no cover - typing only
    from gateway import Model

# Always overwritten -- these are the reason the tool exists.
REQUIRED = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")

# Filled in only when the user has not set them, so hand-tuning survives a re-run.
DEFAULTS = {
    # Agentic turns routinely exceed the 60s default; the gateway itself is slow.
    "API_TIMEOUT_MS": "3000000",
    # Stops telemetry/statsig calls that would go to the public internet.
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    # Makes the extension list models from `${ANTHROPIC_BASE_URL}/v1/models`
    # instead of the built-in catalogue. Requires the binary patch to be useful
    # for non-claude-named models -- see claude_patch.py.
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
}

# Set only when --model is given. ANTHROPIC_MODEL alone is not enough: Claude Code
# resolves the opus/sonnet/haiku tiers separately, and a tier left unset falls back
# to a hardcoded claude-* id the gateway does not serve.
MODEL_KEYS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)

MANAGED = (*REQUIRED, *DEFAULTS, *MODEL_KEYS)


def build_env(base_url: str, api_key: str, model: str | None,
              existing: dict[str, str]) -> dict[str, str]:
    """Merge our settings over whatever `existing` name->value map is there."""
    env = dict(existing)
    env["ANTHROPIC_BASE_URL"] = base_url
    env["ANTHROPIC_AUTH_TOKEN"] = api_key
    for k, v in DEFAULTS.items():
        env.setdefault(k, v)
    if model:
        for k in MODEL_KEYS:
            env[k] = model
    return env


def _as_env_map(entries: list) -> dict[str, str]:
    out: dict[str, str] = {}
    for e in entries or []:
        if isinstance(e, dict) and "name" in e:
            out[str(e["name"])] = "" if e.get("value") is None else str(e["value"])
    return out


def _as_entries(env: dict[str, str]) -> list[dict[str, str]]:
    return [{"name": k, "value": v} for k, v in env.items()]


def apply_editor_settings(path: Path, base_url: str, api_key: str,
                          model: str | None) -> tuple[dict[str, str], dict[str, str]]:
    """Write `claudeCode.environmentVariables`. Returns (before, after) maps."""
    doc = JsoncFile(path)
    before = _as_env_map(doc.get("claudeCode.environmentVariables"))
    after = build_env(base_url, api_key, model, before)

    doc.set("claudeCode.environmentVariables", _as_entries(after))
    # The gateway key is not an Anthropic login; without this the extension opens
    # a browser and waits for an OAuth flow that can never complete.
    doc.set("claudeCode.disableLoginPrompt", True)
    doc.save()
    return before, after


def apply_cli_settings(path: Path, base_url: str, api_key: str,
                       model: str | None) -> tuple[dict[str, str], dict[str, str]]:
    """Write the `env` block of `~/.claude/settings.json`."""
    doc = JsoncFile(path)
    raw = doc.get("env") or {}
    before = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    after = build_env(base_url, api_key, model, before)
    doc.set("env", after)
    doc.save()
    return before, after


# --------------------------------------------------------------------------- #
# /model picker curation
# --------------------------------------------------------------------------- #

# A settings key Claude Code honours from user settings (and managed settings).
# Rows are `{model, label?, description?, behavesAs?}`; `replaceBuiltInOptions`
# is the switch that drops everything Claude Code ships with.
PICKER_KEY = "modelPicker"


def _short_tokens(n: object) -> str:
    try:
        v = int(n)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if v >= 1_000_000:
        return f"{v / 1_000_000:g}M"
    if v >= 1_000:
        return f"{v // 1000}K"
    return str(v) if v else ""


def picker_description(model: Model) -> str:
    """The grey subtitle under a /model row. LiteLLM gives us no description."""
    raw = model.raw or {}
    bits = ["From gateway"]
    ctx = _short_tokens(raw.get("max_input_tokens"))
    if ctx:
        bits.append(f"{ctx} context")
    return " · ".join(bits)


def picker_rows(models: Iterable[Model]) -> list[dict[str, str]]:
    """One /model row per chat-capable gateway model.

    Image and embedding endpoints are left out on purpose: the gateway serves
    them, but picking one fails on the first turn and the picker says nothing
    about why.
    """
    return [
        {"model": m.id, "label": m.id, "description": picker_description(m)}
        for m in models
        if m.is_chat_capable
    ]


def apply_model_picker(path: Path, models: Iterable[Model], *,
                       replace_builtin: bool = True
                       ) -> tuple[list[dict[str, str]], object]:
    """Write `modelPicker` into a settings file. Returns (rows, previous value).

    WHY THIS EXISTS
    ---------------
    Patching `claude.exe` (claude_patch.py) stops Claude Code from *discarding*
    the gateway's models, but the picker still lists its own built-in lineup --
    Opus, Sonnet, Sonnet 5 (1M), Haiku -- which a private gateway does not serve.
    Selecting any of them fails. `replaceBuiltInOptions: true` makes the picker
    show the Default row plus exactly these rows and nothing else.

    Note that it also hides the gateway-discovery list; the rows below are that
    same list, fetched live and ordered, so nothing is lost.

    Without `replaceBuiltin` the rows are appended *after* the built-in lineup,
    which is the "why is claude-opus-5 still in my list" complaint.
    """
    rows = picker_rows(models)
    if not rows:
        return [], None

    doc = JsoncFile(path)
    before = doc.get(PICKER_KEY)
    block: dict[str, object] = {"options": rows}
    if replace_builtin:
        block["replaceBuiltInOptions"] = True
    doc.set(PICKER_KEY, block)
    doc.save()
    return rows, before


def clear_model_picker(path: Path) -> bool:
    """Drop the `modelPicker` block; True when there was one to drop.

    No backup: this is the `--restore` cleanup path, and snapshotting the file
    here would save the very content we are removing.
    """
    doc = JsoncFile(path)
    if not doc.remove(PICKER_KEY):
        return False
    doc.save(backup=False)
    return True


def _existing(paths: list[Path], key: str) -> str | None:
    """First value for `key` across the settings files, in order."""
    for p in paths:
        try:
            data = read_jsonc(p)
        except Exception:  # noqa: BLE001 - a broken settings file is not fatal here
            continue
        entries = data.get("claudeCode.environmentVariables")
        for e in entries or []:
            if isinstance(e, dict) and e.get("name") == key and e.get("value"):
                return str(e["value"])
        env = data.get("env")
        if isinstance(env, dict) and env.get(key):
            return str(env[key])
    return None


def existing_base_url(paths: list[Path]) -> str | None:
    """Whatever gateway this machine is already pointed at, for use as a default."""
    return _existing(paths, "ANTHROPIC_BASE_URL")


def existing_api_key(paths: list[Path]) -> str | None:
    """The key already sitting in the settings, so a refresh need not re-ask.

    It is the same secret, for the same gateway, already written in plaintext by
    this very tool -- reading it back is not a new exposure. It is never printed.
    """
    return _existing(paths, "ANTHROPIC_AUTH_TOKEN")


def classify_models(model_ids: list[str]) -> tuple[list[str], list[str]]:
    """Split gateway models into (visible unpatched, hidden unpatched).

    Mirrors the `/（claude|anthropic)/i` test the bundled binary applies, so the
    user can see exactly what the binary patch buys them.
    """
    visible, hidden = [], []
    for mid in model_ids:
        low = mid.lower()
        (visible if ("claude" in low or "anthropic" in low) else hidden).append(mid)
    return visible, hidden


# --------------------------------------------------------------------------- #
# gateway model-list cache
# --------------------------------------------------------------------------- #

def gateway_cache_path() -> Path:
    """`~/.claude/cache/gateway-models.json`.

    Claude Code persists the discovered gateway model list here, keyed by base URL,
    and the /model picker reads it in preference to re-fetching. Critically, what
    gets written is the list AFTER the `/(claude|anthropic)/i` filter -- so a run
    that happened before the binary patch poisons this file with the filtered
    result, and patching afterwards changes nothing until the cache is dropped.
    """
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (home() / ".claude"))
    return root / "cache" / "gateway-models.json"


def read_gateway_cache(path: Path | None = None) -> dict | None:
    p = path or gateway_cache_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cache_looks_filtered(cache: dict | None) -> bool:
    """True when every cached model is claude/anthropic-named.

    That is the fingerprint of a cache written by an unpatched binary, and it is
    worth calling out by name -- the user sees a stale short list and reasonably
    concludes the patch failed.
    """
    models = (cache or {}).get("models") or []
    if not models:
        return False
    _, hidden = classify_models([str(m.get("id", "")) for m in models])
    return not hidden


def clear_gateway_cache(path: Path | None = None, *, backup: bool = True) -> Path | None:
    """Drop the cache so the next discovery re-fetches. Returns the backup path."""
    p = path or gateway_cache_path()
    if not p.exists():
        return None
    bak = p
    if backup:
        bak = p.with_suffix(p.suffix + ".bak")
        if not bak.exists():
            bak.write_bytes(p.read_bytes())
    p.unlink()
    return bak if backup else None


def restore_from_backup(path: Path) -> bool:
    """Put the pristine copy back and drop the backup, so the next configure
    snapshots a fresh original instead of resurrecting our old output.

    A zero-byte backup records "this file did not exist before" (see
    private_api.snapshot); restoring it means deleting the file we created.
    """
    bak = path.with_suffix(path.suffix + ".bak")
    if not bak.exists():
        return False
    original = bak.read_bytes()
    if original:
        path.write_bytes(original)
    elif path.exists():
        path.unlink()
    bak.unlink()
    return True
