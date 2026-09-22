r"""Per-model capabilities, in a file the user owns instead of in this source tree.

WHY THIS MODULE EXISTS
----------------------
Three decisions used to be baked into Python source: whether a model accepts an
image (`modalities.MEASURED_MODALITIES`), how many thinking rungs it offers
(`reasoning.DEFAULT_EFFORTS`), and how long its context is
(`codex.DEFAULT_CONTEXT_WINDOW`). That made every one of them *code*, so changing
one meant editing source and shipping a new build -- and getting it wrong was
silent.

It got wrong in the worst way on 2026-09-17. The gateway renamed
`deepseek-v4.1-flash-test` to `deepseek-v4.1-flash`; the measured table still had
the old slug, so the new one fell through to `DEFAULT_MODALITIES = ("text",)` and
Codex refused every image paste **client-side**, before any request existed. New
and existing users alike, because what took effect was code. "This model does not
support image inputs." with no way to fix it but a new release.

So the values live in `model-config.jsonc` now -- beside `private_api.py`, in the
toolkit directory the user is already working in: generated on the first run from
the gateway's live model list crossed with a measured seed, editable by the user,
and synced into the real environment by `--apply-model-config`. Changing a
capability is a data edit, in a file that travels with the toolkit.

THE SHAPE, AND WHY IT IS FLAT
-----------------------------
One top-level key per model slug, plus `_`-prefixed reserved keys:

    {
      "_version": 1,
      "_defaults": { ... },        // toolkit-agnostic suggestion policy
      "_toolkits": { "vscode": {...} },   // per-toolkit divergence
      "deepseek-v4.1-flash": { "input_modalities": ["text", "image"], ... }
    }

Deliberately NOT nested under a `"models"` key, even though that reads better.
`jsonc.JsoncFile.set()` only handles TOP-LEVEL keys, and it re-encodes the whole
value it replaces -- so a nested `models` object would lose every comment inside
it on any write, including the measured-result notes this file exists to carry.
Flat means "append a newly-appeared model" is a pure append that cannot touch
what the user wrote.

`_defaults` and `_toolkits` are separate because the two toolkits' defaults are
DELIBERATELY different: `vscode`'s model menu cannot draw a `max` row (it shows
Light/High and silently drops anything above), so it stops at `xhigh`. One shared
default would hand vscode a rung it cannot render -- that is a real display bug,
not a tidiness argument.

FAIL-OPEN, ALWAYS
-----------------
This reads two files, one of which the user edits by hand, and one of its callers
(`codex`'s wrapper via `--sync`) runs on the critical path of every launch. So
every reader here returns {} / None on any problem rather than raising: a broken
config file must cost the user their overrides, never their editor. A value
outside the enums Codex accepts would make the catalog unparseable, which stops
Codex from STARTING -- so the enum filtering stays in the consumers
(`modalities._modalities`, `reasoning._levels`), which this module must not
import. That is also what keeps it import-free of them: modalities and reasoning
both import this, so importing either back would be a cycle.

LAYOUT
------
`private-api/modelconfig.py`     -- this file: file I/O, reserved keys, resolution.
`private-api/model-config.seed.jsonc` -- the measured knowledge, shipped, and kept
                                   byte-identical in both toolkits.
`model-config.jsonc`             -- THE USER'S FILE, generated beside the entry
                                   point on first run. This is the one to edit.
`$CODEX_HOME/gateway-models.json` -- the generated catalog. Output only: Codex
                                   reads it, nothing here reads it back.

`private_api.py` orchestrates generation rather than this module, because deciding
which models get an entry -- and reading the gateway's advertised context -- needs
the gateway and the OpenAI-official check, and this module must stay importable
from `modalities` / `reasoning` without a cycle.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import jsonc
from detect import codex_home

# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #

STORE_FILENAME = "model-config.jsonc"
SEED_FILENAME = "model-config.seed.jsonc"

# WHERE THE USER'S FILE LIVES, AND WHY IT IS NOT IN $CODEX_HOME
# ------------------------------------------------------------
# `model-config.jsonc` sits BESIDE `private_api.py` in the toolkit directory, not
# in Codex's home. It is the user's file, they are told to edit it, and the one
# thing they are already looking at is the toolkit they just ran -- sending them
# into a dot-directory to find it is how a config file goes unedited.
#
# So the flow is: run the toolkit -> the file appears next to the script with the
# defaults already in it -> the DEFAULTS ARE ALREADY APPLIED to the real
# environment (`$CODEX_HOME/gateway-models.json`) as part of that same run -> edit
# the file -> `--apply-model-config` syncs the edits into the real environment.
#
# The generated catalog is the only thing that lands in $CODEX_HOME. Two files,
# two roles: this one is source (portable, editable, travels with the toolkit),
# the catalog is output (consumed by Codex, regenerated at will).
TOOLKIT_ROOT = Path(__file__).resolve().parent.parent

# Which toolkit is asking. Only used to pick the default block -- see _toolkits.
TOOLKIT_CODEX = "codexcli"
TOOLKIT_VSCODE = "vscode"

# A version bump means "regenerate from the seed", not "migrate": there is nothing
# in the file that is expensive to recreate, and the seed is the authority on
# measured values.
VERSION = 1

VERSION_KEY = "_version"
DEFAULTS_KEY = "_defaults"
TOOLKITS_KEY = "_toolkits"

# `_`-prefixed so they can never collide with a gateway model id.
RESERVED_KEYS = (VERSION_KEY, DEFAULTS_KEY, TOOLKITS_KEY)

# The four editable fields. `context_window` is written out so the user can SEE
# and pin it; deleting it means "follow the gateway" again -- see suggested().
FIELD_MODALITIES = "input_modalities"
FIELD_LEVELS = "reasoning_levels"
FIELD_DEFAULT_LEVEL = "default_reasoning_level"
FIELD_CONTEXT = "context_window"

FIELDS = (FIELD_MODALITIES, FIELD_LEVELS, FIELD_DEFAULT_LEVEL, FIELD_CONTEXT)


def store_path() -> Path:
    """The user's editable config: `<toolkit>/model-config.jsonc`.

    Beside `private_api.py`, deliberately -- see TOOLKIT_ROOT above.
    """
    return TOOLKIT_ROOT / STORE_FILENAME


def seed_path() -> Path:
    """The shipped knowledge base, beside this module."""
    return Path(__file__).resolve().parent / SEED_FILENAME


def catalog_hint() -> str:
    """Where the applied result goes, for messages. Purely informational."""
    return str(codex_home() / "gateway-models.json")


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #

def _read(path: Path) -> dict[str, Any]:
    """Parse a JSONC file into a dict, or {} for any problem at all.

    Missing, unreadable, malformed, or not an object -- all the same answer.
    Refusing to continue would take the user's editor down with it, so the parse
    failure is not raised; it is REPORTED separately by `parse_error()`, which is
    how a broken file still produces a complaint instead of a silent downgrade.
    """
    try:
        data = jsonc.read_jsonc(path)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def parse_error() -> str | None:
    """Why the user's file could not be parsed, or None when it is fine.

    `load()` swallows this on purpose -- a broken config must degrade, not take
    Codex down. But degrading SILENTLY is the trap: the user edits a field, sees
    nothing change, and has no way to learn that the file was never read. This is
    the same failure shape as the slug rename that started all this (a capability
    that reverts to a default with no message), so the callers that face the user
    -- `--status` and `--apply-model-config` -- ask for it explicitly.
    """
    path = store_path()
    if not path.exists():
        return None
    try:
        jsonc.read_jsonc(path)
    except OSError as e:
        return f"读不了 {path}: {e}"
    except ValueError as e:
        return f"{path} 不是合法的 JSONC，整份配置被忽略（回落到 seed 与 _defaults）: {e}"
    return None


def load() -> dict[str, Any]:
    """The user's `model-config.jsonc`, or {}. Fail-open -- see `parse_error()`."""
    return _read(store_path())


def load_seed() -> dict[str, Any]:
    """The shipped `model-config.seed.jsonc`, or {}."""
    return _read(seed_path())


def is_reserved(key: str) -> bool:
    """True for `_version` / `_defaults` / `_toolkits` and any other `_`-prefixed key."""
    return key.startswith("_")


def _clean(entry: Any) -> dict[str, Any]:
    """One model's entry with the reserved/annotation keys stripped.

    `_note` is a comment for the generated file, not config -- it must not leak
    into resolution, and a `_`-prefixed key is how the seed says so.
    """
    if not isinstance(entry, dict):
        return {}
    return {str(k): v for k, v in entry.items() if not is_reserved(str(k))}


def entry(slug: str) -> dict[str, Any]:
    """The configured entry for a slug: the user's file, else the seed, else {}.

    Used by generation and by `--status`; resolution goes through `configured()`
    and `suggested_value()` separately, because the two sit at different points in
    the precedence chain (a user value outranks what the gateway advertises, a
    seed value does not).
    """
    mine = load().get(slug)
    if isinstance(mine, dict):
        return _clean(mine)
    return _clean(load_seed().get(slug))


def configured(slug: str, name: str) -> Any | None:
    """Layer 1: what the USER's file says for one field, or None.

    None means "not configured", which is not the same as "configured to the
    empty value" -- the distinction is what lets a deleted key fall back down the
    chain instead of silently meaning "nothing".
    """
    value = _clean(load().get(slug)).get(name)
    return value if value is not None else None


def defaults(toolkit: str = TOOLKIT_CODEX) -> dict[str, Any]:
    """`_defaults` from the user's file (else the seed), overlaid with `_toolkits`.

    The per-toolkit block is applied LAST so a toolkit can override any default --
    that is the whole reason it exists.
    """
    base = load().get(DEFAULTS_KEY)
    if not isinstance(base, dict):
        base = load_seed().get(DEFAULTS_KEY)
    base = dict(base) if isinstance(base, dict) else {}

    overrides = load().get(TOOLKITS_KEY)
    if not isinstance(overrides, dict):
        overrides = load_seed().get(TOOLKITS_KEY)
    if isinstance(overrides, dict):
        mine = overrides.get(toolkit)
        if isinstance(mine, dict):
            base.update({str(k): v for k, v in mine.items() if not is_reserved(str(k))})
    return base


def suggested_value(slug: str, name: str, toolkit: str = TOOLKIT_CODEX) -> Any | None:
    """Layers 5-6: the seed's measurement for this slug, else the toolkit default.

    Sits BELOW what the gateway advertises, so a backend that starts reporting its
    own `input_modalities` wins over our 2026 measurement of it.
    """
    seeded = _clean(load_seed().get(slug)).get(name)
    if seeded is not None:
        return seeded
    return defaults(toolkit).get(name)


# --------------------------------------------------------------------------- #
# what to write for a model nobody has configured
# --------------------------------------------------------------------------- #

def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def suggested(slug: str, toolkit: str = TOOLKIT_CODEX,
              advertised_context: Any = None) -> dict[str, Any]:
    """The entry to generate for a model that is new to the file.

    Every field is written out explicitly rather than left to `_defaults`, so the
    generated file SHOWS the user what is in effect and where to change it. The
    cost of that transparency is stickiness: once written, an entry is never
    rewritten, so a later change on the gateway does not move it. Deleting the key
    (or the whole entry) is what goes back to following the gateway -- documented
    in the generated file's own header.
    """
    out: dict[str, Any] = {}

    modalities = suggested_value(slug, FIELD_MODALITIES, toolkit)
    if isinstance(modalities, (list, tuple)) and modalities:
        out[FIELD_MODALITIES] = [str(m) for m in modalities]

    levels = suggested_value(slug, FIELD_LEVELS, toolkit)
    levels = [str(e) for e in levels] if isinstance(levels, (list, tuple)) else []
    if levels:
        out[FIELD_LEVELS] = levels
        default = suggested_value(slug, FIELD_DEFAULT_LEVEL, toolkit)
        # A default outside the menu would open the picker on a value the user
        # cannot see, so it is coerced rather than trusted.
        out[FIELD_DEFAULT_LEVEL] = str(default) if str(default) in levels else levels[0]

    # The gateway's own number beats every fallback, but a user's explicit value
    # beats the gateway -- that ordering is what makes "pin it here" work.
    context = _as_int(advertised_context)
    if context is None:
        context = _as_int(suggested_value(slug, FIELD_CONTEXT, toolkit))
    if context is not None:
        out[FIELD_CONTEXT] = context

    return out


def note_for(slug: str) -> str:
    """The seed's `_note` for a slug -- the measured verdict, as a comment."""
    seeded = load_seed().get(slug)
    if isinstance(seeded, dict):
        note = seeded.get("_note")
        if note:
            return str(note)
    return ""


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #

HEADER = """\
// 模型能力配置 —— 每个模型「能不能贴图 / 有几档思考 / 上下文多长」。
//
// 这份文件由 private_api.py 生成，就放在脚本旁边，**归你所有**：
//   * 首次运行（安装 / --refresh-models）会生成它，并把里面的默认值**立即应用**到
//     真实环境（$CODEX_HOME/gateway-models.json，Codex 实际读的那份目录），
//     所以装完就能用，不需要额外一步。
//   * 之后每次运行只会把网关**新出现**的模型追加进来，绝不改动你已经写下的条目，
//     注释也不动。
//
// 改完之后执行，把改动同步进真实环境：
//     python3 private_api.py --apply-model-config
//
// 想恢复成「跟着网关和实测值走」：删掉对应的那一行，或删掉整个模型条目，
// 重新执行 --apply-model-config 即可。逐字段解析，删哪个字段就回落哪个字段。
//
// 解析优先级（高 → 低）：
//   1. 本文件里该模型的字段        ← 你写的东西
//   2. 旧的 private-modalities.json / private-reasoning.json（只读，兼容老装机）
//   3. OpenAI 官方模型：models_cache.json（它们的值不归我们管，见下方说明）
//   4. 网关 GET /v1/models 主动广告的字段
//   5. model-config.seed.jsonc 里该模型的实测结论
//   6. _defaults ⊕ _toolkits[<工具包>]
//
// ★ 网关改了模型 id 就是一次**重测**，不是 find-and-replace。表按网关实际 serve
//   的 slug 记：`--probe-modalities` 给出结论，改这里而不是改 Python 源码。
//
// 注：OpenAI 官方模型（gpt-* / o*）不在这份文件里 —— 它们的 input_modalities 和
// 思考档位由 OpenAI 声明，本工具从 models_cache.json 原样抄。目录是替换式的，我们
// 给 gpt-5.6-sol 写什么就会盖掉 OpenAI 自己的定义，所以刻意不写。
"""


def _dump(value: Any, indent: int) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def _reindent(block: str, pad: str) -> str:
    return block.replace("\n", "\n" + pad)


def _render(entries: dict[str, dict[str, Any]], toolkit: str) -> str:
    """The FIRST generation: a hand-laid-out template with comments.

    Hand-built rather than pushed through `JsoncFile.set` because the comments are
    the point -- they are what tells the user which line to change and why a model
    that cannot see is recorded as `["text"]` instead of being left out.
    """
    seed = load_seed()
    lines: list[str] = [HEADER, "{\n", f'  "{VERSION_KEY}": {VERSION},\n']

    lines.append("\n  // 没有单独配置的模型套用这里\n")
    block = _reindent(_dump(suggested_defaults_block(toolkit), 2), "  ")
    lines.append(f'  "{DEFAULTS_KEY}": {block},\n')

    toolkits = seed.get(TOOLKITS_KEY)
    if isinstance(toolkits, dict) and toolkits:
        lines.append("\n  // 某些工具包的默认值与上面不同，写在这里；会在 _defaults 之上覆盖\n")
        block = _reindent(_dump(toolkits, 2), "  ")
        lines.append(f'  "{TOOLKITS_KEY}": {block},\n')

    if entries:
        lines.append("\n  // ── 以下是逐个模型的能力，可手改 ──\n")
        for i, (slug, entry) in enumerate(entries.items()):
            note = note_for(slug)
            block = _reindent(_dump(entry, 2), "  ")
            comma = "" if i == len(entries) - 1 else ","
            tail = f"  // {note}" if note else ""
            lines.append(f'  {json.dumps(slug, ensure_ascii=False)}: {block}{comma}{tail}\n')
    else:
        # Trailing comma is legal JSONC, but leaving one on the last-written key
        # would make the "append a model later" splice start from a messier file.
        if lines[-1].rstrip().endswith(","):
            lines[-1] = lines[-1].rstrip()[:-1] + "\n"

    lines.append("}\n")
    return "".join(lines)


def suggested_defaults_block(toolkit: str) -> dict[str, Any]:
    """The `_defaults` to write on first generation: the seed's, guaranteeing keys.

    `context_window` and `input_modalities` are always present so the file always
    documents both fallbacks even if the seed is edited down.
    """
    seed_defaults = load_seed().get(DEFAULTS_KEY)
    block: dict[str, Any] = dict(seed_defaults) if isinstance(seed_defaults, dict) else {}
    block.pop(VERSION_KEY, None)
    for key, value in (
        (FIELD_MODALITIES, ["text"]),
        (FIELD_CONTEXT, 128_000),
        (FIELD_LEVELS, ["low", "high", "max"]),
        (FIELD_DEFAULT_LEVEL, "high"),
    ):
        block.setdefault(key, value)
    return block


def ensure(suggested_entries: dict[str, dict[str, Any]],
           toolkit: str = TOOLKIT_CODEX) -> tuple[Path, list[str], list[str]]:
    """Create the file if absent, else append only the slugs it is missing.

    Returns `(path, added, kept)`. NEVER rewrites an existing entry: a user's edit
    -- including a comment -- is theirs, and `--refresh-models` runs often enough
    that clobbering it would be indistinguishable from the toolkit being broken.
    """
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        written = {slug: e for slug, e in suggested_entries.items() if e}
        path.write_text(_render(written, toolkit), encoding="utf-8")
        return path, list(written), []

    doc = jsonc.JsoncFile(path)
    kept: list[str] = []
    added: list[str] = []
    for slug, entry in suggested_entries.items():
        if not entry:
            continue
        if slug in doc.data:
            kept.append(slug)
            continue
        doc.set(slug, entry)
        added.append(slug)
    if added:
        doc.save()
    return path, added, kept


# --------------------------------------------------------------------------- #
# validation (used by --apply-model-config to explain, not to gate)
# --------------------------------------------------------------------------- #

def structural_problems() -> list[str]:
    """Complaints about the file's SHAPE, so `--apply` can name the bad line.

    Only shape: an unknown modality or effort is reported by the consumers that
    own those enums, and is dropped rather than propagated either way.
    """
    problems: list[str] = []
    broken = parse_error()
    if broken:
        # Nothing below means anything if the file never parsed: `load()` returned
        # {} and every "missing field" would be an artefact of the syntax error.
        return [broken]
    data = load()
    if not data:
        return problems

    defaults = data.get(DEFAULTS_KEY)
    if defaults is not None and not isinstance(defaults, dict):
        problems.append(f"{DEFAULTS_KEY} 不是对象")

    for slug, entry in data.items():
        if is_reserved(slug):
            continue
        if not isinstance(entry, dict):
            problems.append(f"{slug}: 不是对象")
            continue
        for field in (FIELD_MODALITIES, FIELD_LEVELS):
            value = entry.get(field)
            if value is not None and not isinstance(value, list):
                problems.append(f"{slug}.{field}: 应该是数组")
        context = entry.get(FIELD_CONTEXT)
        if context is not None and _as_int(context) is None:
            problems.append(f"{slug}.{FIELD_CONTEXT}: 应该是整数")
        levels = entry.get(FIELD_LEVELS)
        default = entry.get(FIELD_DEFAULT_LEVEL)
        if isinstance(levels, list) and default is not None and str(default) not in {
                str(e) for e in levels}:
            problems.append(
                f"{slug}.{FIELD_DEFAULT_LEVEL}: {default!r} 不在 {FIELD_LEVELS} 里"
            )
    return problems


def slugs() -> Iterable[str]:
    """Every non-reserved key in the file, in file order."""
    return [k for k in load() if not is_reserved(str(k))]
