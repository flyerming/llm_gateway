r"""Which thinking levels each model offers Codex, and which one it starts on.

THE SAME FILE NAME AS THE VSCode TOOLKIT, ON PURPOSE
----------------------------------------------------
The store is `$CODEX_HOME/private-reasoning.json`, byte-compatible with the one
`vscode/private-api/reasoning.py` writes. A machine that has both toolkits
configured reads the SAME per-model levels: whichever one wrote last wins, and
neither has to be re-taught. Same for `private-modalities.json`.

WHAT DRIVES THE MENU
--------------------
Codex renders its reasoning choice purely from the model catalog we generate
(`model_catalog_json`). Two fields decide it:

    "supported_reasoning_levels": [{"effort": "low", "description": "..."}, ...]
    "default_reasoning_level": "low"

They are OBJECTS, not bare strings. `["low","high"]` makes the whole catalog
fail to parse, which stops Codex from starting rather than degrading.

THE PRIVATE DEFAULT IS THREE RUNGS: low / high / max
----------------------------------------------------
The design brief asks for three, so DEFAULT_EFFORTS carries three and a private
model gets that menu out of the box. The vscode toolkit stops at `xhigh` instead
of `max` -- its webview could not draw a `max` row for a private provider (it
showed Light/High and silently dropped everything above), so it tops out where
its menu does. The CLI has no such limit, so it goes to the real ceiling.

Measured against this deployment's backends (2026-09-16, `POST /v1/responses`
with `reasoning.effort` set, one call per model per level):

    deepseek-v4.1-flash-test   low, high, xhigh, max  -> 200
                               minimal, medium        -> 400
                                 "DeepSeek V4.1 reasoning_effort must be low,
                                  high, xhigh, max, or an integer within [1, 10]"
    deepseek-v4-flash          every value -> 200  (the backend does not
    glm-5.3-flash              validate at all -- it accepts `medium` and
    qwen3-5-397b               quietly ignores it, so 200 proves nothing)
    xinghai-ultra

The strict backend accepts FOUR values, and `xhigh` is one of them; it is not a
synonym for `max` but the rung below it. It is left out of the default because
the brief asks for three -- but it is one flag away, as the `xhigh` shorthand
(`--reasoning-levels xhigh`), for a model where the extra rung is wanted.

The cost of dropping it from the default is a TUI detail: Codex files `max`
(and `ultra`) under the **"Advanced Reasoning"** submenu, so the ordinary
reasoning menu now offers only `low` / `high` and `max` needs that submenu.
`xhigh`, had it been kept, would have been reachable from the ordinary menu.

`minimal` and `medium` are deliberately absent: on the strict backend they are
an HTTP 400 on every turn, and on the permissive ones they are indistinguishable
from `high` -- a level that means nothing but looks like a choice.

OVERRIDING
----------
Per model, with `--configure-reasoning`. An empty list is meaningful and kept:
it means "no reasoning menu for this model", which is right for a plain
chat model whose backend has no such knob.

OPENAI'S OWN MODELS ARE LEFT ALONE
----------------------------------
For `gpt-*` / `o*` slugs the levels come out of `$CODEX_HOME/models_cache.json`
verbatim, because the catalog we write REPLACES Codex's own and an entry we emit
for `gpt-5.6-sol` overrides whatever OpenAI shipped. A headless box that has
never signed in has no cache; those models then get OPENAI_FALLBACK_EFFORTS.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from detect import codex_home, codex_models_cache

# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #

STORE_FILENAME = "private-reasoning.json"

# Descriptions are copied from OpenAI's own catalog so the menu reads the same
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

# Every value Codex's own enum accepts. A level outside this set makes the
# catalog fail to parse, which takes Codex down on startup rather than
# degrading, so input is validated rather than trusted.
KNOWN_EFFORTS = tuple(EFFORT_DESCRIPTIONS)

# What a private (custom_openai) model gets when nobody has configured it: the
# three rungs the design brief asks for. See the module docstring -- the strict
# backend also accepts `xhigh`, which is reachable via the `xhigh` shorthand
# below rather than being part of the default.
DEFAULT_EFFORTS = ("low", "high", "max")
DEFAULT_EFFORT = "high"

# Shorthands for `--reasoning-levels`, which accepts these names directly.
PROFILE_PRIVATE = "private"   # low,high,max        (the default)
PROFILE_THREE = "three"       # low,high,max        (alias of `private`)
PROFILE_XHIGH = "xhigh"       # low,high,xhigh,max  (the strict backend's full set)
PROFILE_NONE = "none"         # []  -- no reasoning menu at all
PROFILES: dict[str, tuple[str, ...]] = {
    PROFILE_PRIVATE: DEFAULT_EFFORTS,
    PROFILE_THREE: DEFAULT_EFFORTS,
    PROFILE_XHIGH: ("low", "high", "xhigh", "max"),
    PROFILE_NONE: (),
}

# Used for OpenAI's models only when `models_cache.json` cannot be read (a
# headless box that never signed in). Conservative on purpose: a level the model
# does not really support is worse than a missing one, because the user only
# finds out mid-task.
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


def resolve_profile(spec: str) -> tuple[str, ...]:
    """`--reasoning-levels` value -> effort tuple. `private`/`three`/`none` are
    shorthands for the sets this deployment is known to accept."""
    key = spec.strip().lower()
    if key in PROFILES:
        return PROFILES[key]
    return tuple(e.strip() for e in spec.split(",") if e.strip())


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
    path = codex_models_cache()
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

    An empty `efforts` is meaningful and is kept: it means "no reasoning menu for
    this model", which is how a plain non-reasoning chat model should look.
    """
    levels = _levels(efforts)
    entry: dict[str, object] = {"levels": levels}
    if levels:
        chosen = default or DEFAULT_EFFORT
        # A default outside the offered set would make Codex open the menu on a
        # value the user cannot even see.
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


def clear_all() -> int:
    store = load()
    n = len(store)
    save({})
    return n


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #

def levels_for(model_id: str, raw_meta: dict | None = None) -> tuple[list[dict[str, str]], str | None]:
    """The catalog levels + default for one model. `([], None)` = no reasoning menu.

    Order, most specific first:

      1. OpenAI's own models -- always `models_cache.json`, never the user file.
         Their behaviour is OpenAI's to define.
      2. An explicit entry in `private-reasoning.json`.
      3. What the gateway advertises in `GET /v1/models` for that model.
      4. DEFAULT_EFFORTS (low/high/max -- see the module docstring).
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
