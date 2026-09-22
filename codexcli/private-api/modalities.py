r"""Which input types each model accepts -- i.e. whether you can paste an image.

WHY THE CATALOG DECIDES THIS
----------------------------
Codex never asks the model whether it can see. It asks the CATALOG, and refuses
the attachment client-side before any request is built. Two separate gates, both
reading the same field:

  * the webview, when you paste -- `inputModalities.includes("image") === false`
    raises the toast "This model does not support image inputs."
  * the app-server, when you submit -- "Model <id> does not support image inputs.
    Remove images or switch models."

The catalog's `input_modalities` becomes `inputModalities` on the model object the
app-server hands the webview. Get it wrong in the permissive direction and the
request goes out and fails upstream; get it wrong in the restrictive direction and
the user simply cannot attach anything. Both are why this is worth a module.

THE VALUES
----------
`["text", "image"]`, plus `audio` which Codex's `InputModality` enum also knows but
no model here uses. Read off `~/.codex/models_cache.json`, which Codex wrote
itself, and confirmed by feeding a probe catalog through `codex debug models`.

CAPABILITY IS MEASURED, NOT DECLARED
------------------------------------
The gateway is no help here: `GET /v1/models` reports `mode` ("chat" vs
"image_generation") and nothing about input types. And "accepts the request" is
not the same as "can see" -- one model on this deployment answers HTTP 200 to an
image and then describes a colour that is not in it. So each model was measured,
by sending a solid-red and a solid-blue PNG and checking the model tells them
apart. `--probe-modalities` re-runs that measurement.

Whichever way the measurement goes, the verdict is written down explicitly -- a
model that CANNOT see is recorded as `["text"]` rather than left to the fallback, so
that nobody later mistakes "not configured" for "not capable" and switches it on.

WHERE THE VERDICTS LIVE
-----------------------
In data, not here: `$CODEX_HOME/model-config.jsonc` (user-editable) seeded by
`model-config.seed.jsonc` (shipped). This module owns the MEASUREMENT -- the probe
images, the judge, the fallback policy -- and `modelconfig.py` owns the storage.
The split exists because keeping the verdicts in source made a gateway slug rename
unfixable without a new release; see modelconfig.py's docstring.

OPENAI'S OWN MODELS
-------------------
Their capability is OpenAI's to declare, so -- exactly as with reasoning levels --
it is copied out of `models_cache.json` verbatim and every config file is ignored
for them. Note this is not just tidiness: the catalog we write REPLACES Codex's, so
an entry we emit for `gpt-5.6-sol` overrides what OpenAI shipped. Before this module
existed the catalog hardcoded `["text"]` for everything, which quietly took image
input away from the OpenAI models too.
"""

from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path

import modelconfig
from detect import codex_home, codex_models_cache
from reasoning import is_openai_official

# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #

STORE_FILENAME = "private-modalities.json"

# Every value Codex's `InputModality` enum accepts. A value outside this set makes
# the catalog fail to parse, which takes Codex down on startup rather than
# degrading, so it is validated rather than trusted.
KNOWN_MODALITIES = ("text", "image", "audio")

# What an unrecognised private model gets. Deliberately the narrow choice: a wrong
# "yes" sends an image the backend may reject mid-task (or worse, silently
# hallucinate about), while a wrong "no" costs the user one CLI flag.
DEFAULT_MODALITIES = ("text",)

# OpenAI's models when `models_cache.json` cannot be read. Every one of them is
# multimodal today, so this is the permissive fallback; unlike the private case a
# missing cache is a local problem, not evidence about the model.
OPENAI_FALLBACK_MODALITIES = ("text", "image")


# --------------------------------------------------------------------------- #
# measured capability of THIS deployment's private models
# --------------------------------------------------------------------------- #

# The measurements USED to be a dict here, keyed by slug. They are now data:
# `$CODEX_HOME/model-config.jsonc` for the live values, seeded by
# `model-config.seed.jsonc`. See modelconfig.py for why -- short version: a table
# written in source meant the gateway renaming `deepseek-v4.1-flash-test` to
# `deepseek-v4.1-flash` silently sent the new slug to DEFAULT_MODALITIES, and
# Codex then refused every image paste client-side with no way to fix it short of
# a new release.
#
# The measurement METHOD is unchanged and still lives here (`probe_images`,
# `judge`, and `--probe-modalities` to run them). Re-measure after the backends
# change, then record the verdict in the config file -- a slug rename is a
# re-measure, not a find-and-replace.


def store_path() -> Path:
    return codex_home() / STORE_FILENAME


def _modalities(raw: list[str] | tuple[str, ...] | str) -> list[str]:
    """Normalise to known values, in enum order, without duplicates."""
    if isinstance(raw, str):
        raw = [m.strip() for m in raw.split(",")]
    seen = {str(m).strip() for m in raw}
    # Enum order rather than input order, so the catalog is stable regardless of
    # how the user happened to type it.
    return [m for m in KNOWN_MODALITIES if m and m in seen]


def normalize_entries(raw: object) -> list[str]:
    """Accept a list of names or of `{"modality": ...}` objects, return names."""
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            names.append(str(item.get("modality") or item.get("type") or ""))
        else:
            names.append(str(item))
    return _modalities(names)


# --------------------------------------------------------------------------- #
# OpenAI's own models
# --------------------------------------------------------------------------- #

def openai_modalities_from_cache(model_id: str) -> list[str]:
    """Codex's own `input_modalities` for one OpenAI model, straight from its cache.

    Returns [] when the cache is missing or has nothing for this slug; callers
    fall back to OPENAI_FALLBACK_MODALITIES.
    """
    path = codex_models_cache()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []

    entries = data.get("models") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return []

    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("slug", "")) == model_id:
            return normalize_entries(entry.get("input_modalities"))
    return []


# --------------------------------------------------------------------------- #
# user overrides
# --------------------------------------------------------------------------- #

def load() -> dict[str, object]:
    """The `$CODEX_HOME/private-modalities.json` map, or {} when absent/broken.

    The OLDER store, read but never written now -- `model-config.jsonc` outranks
    it. Kept so a machine configured before that file existed keeps its choices.

    A malformed file must not take the toolkit down: the catalog can always be
    rebuilt from `model-config.jsonc` and the seed, so this degrades instead of
    raising.
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
    return {str(k): v for k, v in data.items()}


def save(store: dict[str, object]) -> Path:
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


def configure(model_id: str, modalities: list[str] | tuple[str, ...] | str) -> list[str]:
    """Store one model's input types. Returns what was written.

    `"text"` alone is meaningful and kept: it is how a model that cannot see is
    recorded as a deliberate decision rather than a gap.
    """
    chosen = _modalities(modalities)
    if not chosen:
        raise ValueError(
            f"{modalities!r} 中没有可用的输入类型；"
            f"应为 {', '.join(KNOWN_MODALITIES)} 的子集"
        )
    store = load()
    store[model_id] = {"input_modalities": chosen}
    save(store)
    return chosen


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

def modalities_for(model_id: str, raw_meta: dict | None = None,
                   toolkit: str = modelconfig.TOOLKIT_CODEX) -> list[str]:
    """The catalog `input_modalities` for one model.

    Order, most specific first:

      1. OpenAI's own models -- always `models_cache.json`, never any config file.
         Their capability is OpenAI's to declare and the catalog replaces theirs,
         so a value we write for `gpt-5.6-sol` overrides what OpenAI shipped.
      2. `$CODEX_HOME/model-config.jsonc` -- the user's file.
      3. `private-modalities.json` -- the older per-model store. Still read so a
         machine configured before the config file existed keeps its choices, but
         no longer written.
      4. What the gateway advertises in `GET /v1/models` for that model. Nothing
         on this deployment does, but LiteLLM will pass such a field through if a
         backend ever sends one.
      5. `model-config.seed.jsonc` -- this deployment's measured verdict.
      6. `_defaults` / `_toolkits` from the config file, then DEFAULT_MODALITIES
         ("text") if even that is unreadable.
    """
    if is_openai_official(model_id):
        return openai_modalities_from_cache(model_id) or list(OPENAI_FALLBACK_MODALITIES)

    chosen = normalize_entries(modelconfig.configured(model_id,
                                                      modelconfig.FIELD_MODALITIES))
    if chosen:
        return chosen

    entry = load().get(model_id)
    if isinstance(entry, dict):
        chosen = normalize_entries(entry.get("input_modalities"))
        if chosen:
            return chosen

    meta = raw_meta or {}
    for key in ("input_modalities", "modalities", "supported_modalities"):
        advertised = normalize_entries(meta.get(key))
        if advertised:
            return advertised

    # Seed first, then the defaults block. `normalize_entries` drops anything
    # outside KNOWN_MODALITIES, so a hand-edited typo degrades to the fallback
    # instead of reaching the catalog and stopping Codex from starting.
    measured = normalize_entries(
        modelconfig.suggested_value(model_id, modelconfig.FIELD_MODALITIES, toolkit))
    if measured:
        return measured

    return list(DEFAULT_MODALITIES)


# --------------------------------------------------------------------------- #
# measuring capability
# --------------------------------------------------------------------------- #

PROBE_COLOURS = {"red": (220, 20, 20), "blue": (20, 20, 220)}
PROBE_QUESTION = ("What is the dominant colour of this image? "
                  "Answer with a single colour name.")


def _png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A minimal solid-colour PNG. Small enough to inline, big enough to see."""
    row = b"\x00" + bytes(rgb) * width
    raw = row * height

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def probe_images() -> dict[str, str]:
    """`{"red": <b64>, "blue": <b64>}` -- the two images a probe sends."""
    return {name: base64.b64encode(_png(64, 64, rgb)).decode("ascii")
            for name, rgb in PROBE_COLOURS.items()}


# A model naming the shade rather than the primary is still reading the pixels --
# "maroon" and "navy blue" for a red and a blue image is a pass, so each family
# carries its near-misses. Chinese names are included because these backends are
# Chinese-hosted and a correct answer in the other language is still correct.
COLOUR_FAMILIES: dict[str, tuple[str, ...]] = {
    "red": ("red", "crimson", "scarlet", "maroon", "ruby", "vermilion", "cherry",
            "brick", "burgundy", "rose", "红"),
    "blue": ("blue", "navy", "azure", "cobalt", "sapphire", "indigo", "cyan",
             "teal", "蓝", "藍"),
}

# A model that says it cannot see is a different failure from one that invents a
# colour, and worth reporting differently: it is refusing, not hallucinating.
DECLINE_PHRASES = ("cannot see", "can't see", "unable to see", "cannot determine",
                   "can't determine", "cannot identify", "no image", "without an image",
                   "not able to see", "无法", "看不到", "没有图", "无法看到")


def _families(text: str) -> set[str]:
    lowered = text.lower()
    return {name for name, words in COLOUR_FAMILIES.items()
            if any(w in lowered for w in words)}


def judge(answers: dict[str, str]) -> tuple[bool, str]:
    """Decide whether a model that gave these answers is really looking.

    The test is DISCRIMINATION, not a colour name match. A model naming the right
    shade of both images is looking; a model that says "I see red and blue in this
    image" to both is not, however many correct words it used -- so an answer
    that touches both families counts for neither.
    """
    red_text = answers.get("red") or ""
    blue_text = answers.get("blue") or ""

    if (any(p in red_text.lower() for p in DECLINE_PHRASES)
            and any(p in blue_text.lower() for p in DECLINE_PHRASES)):
        return False, "两张图都拒答 —— 它知道自己没有视觉能力，诚实但没用"

    red_ok = _families(red_text) == {"red"}
    blue_ok = _families(blue_text) == {"blue"}

    if red_ok and blue_ok:
        return True, "能区分红色和蓝色 —— 它确实在看图"
    if not red_ok and not blue_ok:
        return False, ("两种颜色都没说对 —— 无论它返回什么，它都没有在看像素")
    which = "红色" if red_ok else "蓝色"
    other = "蓝色" if red_ok else "红色"
    return False, (f"{which}图说对了，但{other}图没说对 —— "
                   "更像是猜的，而不是真的看到了")
