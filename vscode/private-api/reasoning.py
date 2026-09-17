r"""Which thinking levels each model offers Codex, and which one it starts on.

WHY THIS IS A SEPARATE MODULE
-----------------------------
Codex renders its "Reasoning" submenu purely from the model catalog we generate
(`model_catalog_json`). The catalog entry carries two fields that decide it:

    "supported_reasoning_levels": [{"effort": "low", "description": "..."}, ...]
    "default_reasoning_level": "low"

Get those wrong and either the submenu vanishes or `codex` refuses to start.
They are also the ONE thing a user genuinely wants to tune per model: a private
DeepSeek backend and a private Qwen backend do not necessarily accept the same
efforts. So the levels live in a small user-editable file rather than being
hardcoded, and this module is the only place that knows how to read it.

THE SCHEMA IS OBJECTS, NOT STRINGS
----------------------------------
An earlier version of `codex.py` emitted `["low", "high", "max"]`. That is wrong
and silently breaks the catalog. Verified against two independent sources:

  * `~/.codex/models_cache.json` -- the catalog Codex fetches for itself
  * strings inside `codex.exe`, where the schema reads
    `... default_reasoning_level supported_reasoning_levels shell_type ...`

Both agree each level is `{"effort": <str>, "description": <str>}`.

OPENAI'S OWN MODELS ARE LEFT ALONE
----------------------------------
Codex ships a catalog for the models OpenAI serves, and this toolkit's catalog
REPLACES that list -- so an entry we write for `gpt-5.6-sol` overrides whatever
OpenAI intended, including its reasoning levels. To keep those "as they were",
we copy the levels straight out of `models_cache.json` verbatim and ignore any
user override for them. `--keep-builtin-models` is unrelated; this is about the
per-entry fields, not which entries appear.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from detect import codex_home

# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #

STORE_FILENAME = "private-reasoning.json"

# Descriptions are copied from OpenAI's own catalog so the submenu reads the same
# whichever model the user picks.
EFFORT_DESCRIPTIONS: dict[str, str] = {
    "none": "No reasoning",
    "minimal": "Barely any reasoning",
    "low": "Fast responses with lighter reasoning",
    "medium": "Balances speed and reasoning depth for everyday tasks",
    "high": "Greater reasoning depth for complex problems",
    "xhigh": "Extra high reasoning depth for complex problems",
    "max": "Maximum reasoning depth for the hardest problems",
    "ultra": "Maximum reasoning with automatic task delegation",
    "persistent": "Keeps reasoning active across the whole session",
}

# Every value Codex's own enum accepts -- read out of codex.exe. A level outside
# this set makes the catalog fail to parse, which takes Codex down on startup
# rather than degrading, so it is worth validating rather than trusting input.
KNOWN_EFFORTS = tuple(EFFORT_DESCRIPTIONS)

# What a private model gets when nobody has configured it.
#
# `max` is deliberately absent even though it is a valid effort value, our
# backends accept it, and the picker has a label for it ("Max"). Codex will not
# RENDER it: with `max` in this list the submenu shows only Light/High, and the
# same happens for `gpt-5.6-sol`, whose catalog entry lists six levels. So the
# ceiling tracks the provider rather than the model, and a level the user cannot
# click is worse than one rung lower that they can. `xhigh` ("Extra High") is the
# highest value the picker actually draws; it is what OpenAI's own gpt-5.5 entry
# stops at too.
#
# `max` still works if set directly (`--reasoning-levels low,high,max`, or
# `model_reasoning_effort = "max"` in config.toml) -- it just cannot be picked
# from the menu.
DEFAULT_EFFORTS = ("low", "high", "xhigh")
DEFAULT_EFFORT = "high"

# Used for OpenAI's models only when `models_cache.json` cannot be read. Kept
# deliberately conservative: a level the model does not really support is worse
# than a missing one, because the user only finds out mid-task.
OPENAI_FALLBACK_EFFORTS = ("low", "medium", "high", "xhigh")


def store_path() -> Path:
    return codex_home() / STORE_FILENAME


def _levels(efforts: list[str] | tuple[str, ...] | str) -> list[dict[str, str]]:
    """Turn effort names into catalog objects, dropping anything Codex rejects."""
    if isinstance(efforts, str):
        efforts = [e.strip() for e in efforts.split(",")]
    out: list[dict[str, str]] = []
    for e in efforts:
        name = str(e).strip()
        if name and name in KNOWN_EFFORTS and name not in {x["effort"] for x in out}:
            out.append({"effort": name, "description": EFFORT_DESCRIPTIONS[name]})
    return out


def normalize_entries(raw: object) -> list[dict[str, str]]:
    """Accept either effort names or full objects, return catalog objects.

    Gateways describe their reasoning support inconsistently -- some send
    `["low","high"]`, some send `[{"effort": "high"}]`. Both are worth honouring.
    """
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            names.append(str(item.get("effort", "")))
        else:
            names.append(str(item))
    return _levels(names)


# --------------------------------------------------------------------------- #
# who is "OpenAI's own"
# --------------------------------------------------------------------------- #

# `gpt-5.6-sol`, `codex-mini`, `chatgpt-4o`, `o3-mini` ... A bare `gpt` with no
# separator is included because the catalog uses it for the default entry.
_OPENAI_RE = re.compile(r"^(?:gpt|chatgpt|codex)(?:[-.]|$)|^o[1-9](?:[-.]|$)", re.I)


def is_openai_official(model_id: str) -> bool:
    """True for models OpenAI itself serves, whose levels we must not override."""
    return bool(_OPENAI_RE.match(model_id.strip()))


def openai_levels_from_cache(model_id: str) -> tuple[list[dict[str, str]], str | None]:
    """Codex's own levels for one OpenAI model, straight out of its cache.

    Returns ([], None) when the cache is missing or has nothing for this slug;
    callers fall back to OPENAI_FALLBACK_EFFORTS.
    """
    path = codex_home() / "models_cache.json"
    if not path.exists():
        return [], None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], None

    entries = data.get("models") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return [], None

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("slug", "")) != model_id:
            continue
        levels = normalize_entries(entry.get("supported_reasoning_levels"))
        default = entry.get("default_reasoning_level")
        return levels, (str(default) if default else None)
    return [], None


# --------------------------------------------------------------------------- #
# user overrides
# --------------------------------------------------------------------------- #

def load() -> dict[str, dict[str, object]]:
    """The `$CODEX_HOME/private-reasoning.json` map, or {} when absent/broken.

    A malformed file must not take the whole toolkit down: the catalog can always
    be regenerated from defaults, so this degrades instead of raising.
    """
    path = store_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def save(store: dict[str, dict[str, object]]) -> Path:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        if not bak.exists():  # only ever snapshot the original
            bak.write_bytes(path.read_bytes())
    path.write_text(
        json.dumps(store, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def configure(model_id: str, efforts: list[str] | tuple[str, ...] | str,
              default: str | None = None) -> dict[str, object]:
    """Store one model's levels. Returns the entry that was written.

    An empty `efforts` is meaningful and is kept: it means "no Reasoning submenu
    for this model", which is how a plain non-reasoning chat model should look.
    """
    levels = _levels(efforts)
    entry: dict[str, object] = {"levels": levels}
    if levels:
        chosen = default or levels[0]["effort"]
        # A default outside the offered set would make Codex open the submenu on
        # a value the user cannot even see.
        if chosen not in {x["effort"] for x in levels}:
            raise ValueError(
                f"default {chosen!r} is not one of the configured levels "
                f"({', '.join(x['effort'] for x in levels)})"
            )
        entry["default"] = chosen

    store = load()
    store[model_id] = entry
    save(store)
    return entry


def clear(model_id: str) -> bool:
    store = load()
    if model_id not in store:
        return False
    store.pop(model_id)
    save(store)
    return True


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #

def levels_for(model_id: str, raw_meta: dict | None = None) -> tuple[list[dict[str, str]], str | None]:
    """The catalog levels + default for one model. `([], None)` = no submenu.

    Order, most specific first:

      1. OpenAI's own models -- always `models_cache.json`, never the user file.
         Their behaviour is OpenAI's to define, and overriding it is the thing
         the user asked us not to do.
      2. An explicit entry in `private-reasoning.json`.
      3. What the gateway advertises in `GET /v1/models` for that model.
      4. DEFAULT_EFFORTS.
    """
    if is_openai_official(model_id):
        levels, default = openai_levels_from_cache(model_id)
        if levels:
            return levels, default
        return _levels(OPENAI_FALLBACK_EFFORTS), OPENAI_FALLBACK_EFFORTS[0]

    entry = load().get(model_id)
    if entry is not None:
        levels = normalize_entries(entry.get("levels"))
        default = entry.get("default")
        return levels, (str(default) if default else None)

    meta = raw_meta or {}
    for key in ("supported_reasoning_levels", "reasoning_levels"):
        levels = normalize_entries(meta.get(key))
        if levels:
            default = meta.get("default_reasoning_level") or meta.get("default_reasoning_effort")
            if str(default) not in {x["effort"] for x in levels}:
                default = levels[0]["effort"]
            return levels, str(default)

    return _levels(DEFAULT_EFFORTS), DEFAULT_EFFORT
