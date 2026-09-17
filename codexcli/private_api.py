#!/usr/bin/env python3
r"""Point the Codex CLI at a private gateway: no login, live `/model` list.

    python3 private_api.py                              # interactive wizard
    python3 private_api.py --api-base http://10.18.219.156:4000 --api-key sk-xxx
    python3 private_api.py --list-models                # what the gateway serves
    python3 private_api.py --switch-model               # pin a different model
    python3 private_api.py --sync                       # refresh the catalog (wrapper)
    python3 private_api.py --status                     # what is configured right now
    python3 private_api.py --install-wrapper            # auto-refresh on every launch
    python3 private_api.py --restore                    # undo

WHAT IT TOUCHES, ALL UNDER `$CODEX_HOME` (default `~/.codex`)

  * `config.toml`  -> `[model_providers.private]` + `model` + `model_catalog_json`
                      A `[model_providers.<id>]` table pointing at the gateway,
                      and `requires_openai_auth = false` so Codex stops asking
                      for a ChatGPT account it can never use.
  * `gateway-models.json` -> the generated model catalog. This is what makes
                      `/model` list the gateway's models and nothing else: the
                      file REPLACES Codex's built-in OpenAI lineup, every entry
                      of which the gateway would fail to serve.
  * `auth.json`    -> the gateway key, so the login gate never appears. Codex
                      decides "signed in" by this file's presence, whatever
                      `model_provider` says -- see private-api/codex_auth.py.
  * `private-reasoning.json` / `private-modalities.json` -> per-model thinking
                      levels and image support, written only when overridden.
                      Same file names as the VSCode toolkit, so a machine that
                      uses both shares one set of overrides.
  * `~/.local/bin/codex` -> only with `--install-wrapper`. A two-line shell
                      wrapper that refreshes `gateway-models.json` (throttled,
                      and never fatal) before exec'ing the real codex.

THE GATEWAY ITSELF (only with `--apply-gateway-config`)

  `litellm_params.allowed_openai_params` and `additional_drop_params` on the
  `custom_openai` models. Codex only speaks `/v1/responses`, and every turn
  carries `reasoning.effort` (LiteLLM maps it to `reasoning_effort`, which a
  `custom_openai` definition refuses -> HTTP 400) and `client_metadata` (which
  LiteLLM forwards into the OpenAI SDK, which has no such kwarg -> HTTP 500).
  Without both, EVERY turn against a private model fails. Check the state with
  `--emit-gateway-config`, which only prints; applying needs the LiteLLM master
  key and changes the gateway for everyone using it.

Every file is backed up to `<file>.bak` before it is written.

LAYOUT
------
This is the entry point and lives at the top of `codexcli/`. The modules it
drives are in `codexcli/private-api/`; nothing there is invoked directly.
`codexcli/README.md` is the full write-up (Chinese).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE / "private-api"
sys.path.insert(0, str(LIB))

import codex as codex_mod
import codex_auth
import detect
import gateway
import litellm_admin
import modalities
import reasoning
from tomlpatch import TomlFile

# --------------------------------------------------------------------------- #

EXAMPLES = """\
  --api-base examples
      http://10.18.219.156:4000          the LiteLLM gateway on the LAN
      http://127.0.0.1:4000              same box, docker-compose default
      https://llm.corp.example.com       behind an ingress (a trailing /v1 is OK)

  --api-key examples
      sk-xxxxxxxxxxxxxxxxxxxxxxxx        LiteLLM master key / virtual key
      <your gateway's sk-... token>

  full invocation
      python3 private_api.py \\
          --api-base http://10.18.219.156:4000 \\
          --api-key sk-XXXXXXXX
"""

# How long --sync waits for the gateway before giving up and letting codex start.
# It runs on the critical path of every `codex` launch, so it is deliberately
# impatient: a slow gateway costs the user a stale model list, not a hang.
SYNC_TIMEOUT = 8.0


def self_cmd() -> str:
    """How to re-invoke this script: relative to cwd when that is meaningful."""
    me = Path(__file__).resolve()
    try:
        return str(me.relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(me)


def out(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str = "") -> None:
    out()
    out(f"--- {title} " + "-" * max(0, 66 - len(title)))


def note(msg: str) -> None:
    """A line that --sync/--quiet suppresses. Errors never go through this."""
    if not QUIET:
        out(msg)


# Set from --quiet in main(). A module global rather than a parameter because it
# is read by helpers several call layers down (fetch_models, warn_gateway_params).
QUIET = False


def _mask(secret: str) -> str:
    return f"{secret[:5]}...{secret[-4:]}" if len(secret) > 12 else "***"


# --------------------------------------------------------------------------- #
# interaction
# --------------------------------------------------------------------------- #

def is_tty() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:  # noqa: BLE001
        return False


def ask(prompt: str, default: str | None = None, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            if secret:
                val = getpass.getpass(f"{prompt}{suffix}: ")
            else:
                val = input(f"{prompt}{suffix}: ")
        except (EOFError, KeyboardInterrupt):
            out()
            raise SystemExit("aborted")
        val = val.strip() or (default or "")
        if val:
            return val
        out("  (required -- press Ctrl+C to abort)")


def confirm(prompt: str, default: bool = True) -> bool:
    if not is_tty():
        return default
    d = "Y/n" if default else "y/N"
    try:
        ans = input(f"{prompt} [{d}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        out()
        raise SystemExit("aborted")
    if not ans:
        return default
    return ans in ("y", "yes")


def pick_model(models: list[gateway.Model],
               title: str = "pick a model") -> str:
    """Numbered picker over the live gateway list. Returns the chosen id."""
    chat = [m for m in models if m.is_chat_capable]
    other = [m for m in models if not m.is_chat_capable]

    out()
    out(f"{title} (from the gateway, {len(models)} served):")
    for i, m in enumerate(chat, 1):
        out(f"  {i:>3}. {m.id}")
    for i, m in enumerate(other, len(chat) + 1):
        out(f"  {i:>3}. {m.id}   <- not a chat model, will not work")

    # Deliberately no isatty() gate: piped input (`printf '3\n' | ...`) is a
    # legitimate way to script this, and `ask()` turns EOF into a clear abort.
    ordered = chat + other
    while True:
        raw = ask("number, or a substring to filter, or the exact model id").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(ordered):
            return ordered[int(raw) - 1].id
        matches = [m for m in models if raw.lower() in m.id.lower()]
        if len(matches) == 1:
            return matches[0].id
        if len(matches) > 1:
            out("  ambiguous:")
            for m in matches:
                out(f"    {m.id}")
            continue
        if any(m.id == raw for m in models):
            return raw
        out("  no model serves that id, try again")


# --------------------------------------------------------------------------- #
# shared setup
# --------------------------------------------------------------------------- #

def snapshot(paths: list[Path]) -> None:
    """Copy each existing file to `<name>.bak` once, before we touch it."""
    for p in paths:
        if not p.exists():
            continue
        bak = p.with_suffix(p.suffix + ".bak")
        if not bak.exists():  # only ever snapshot the ORIGINAL
            bak.write_bytes(p.read_bytes())


def resolve_credentials(args, saved_base: str | None,
                        saved_key: str | None = None) -> tuple[str, str]:
    """Fill in --api-base / --api-key, prompting or showing examples as needed."""
    base, key = args.api_base, args.api_key

    # The gateway in config.toml is one this tool wrote on an earlier run, so
    # reusing it is the obvious default -- asking every time is just noise.
    if not base and saved_base:
        base = saved_base
        note(f"  found an existing gateway in config.toml: {saved_base}")

    # The key for that gateway is usually already there too. Reusing it is what
    # makes `--refresh-models` a one-liner -- but only when the base is the same
    # one, or it would silently point an old key at a different gateway.
    if (not key and saved_key and saved_base and base
            and gateway.normalize_base(base, keep_v1=False)
            == gateway.normalize_base(saved_base, keep_v1=False)):
        key = saved_key
        note(f"  reusing the API key already in config.toml ({_mask(key)})")

    if not base:
        out()
        out("No --api-base given.")
        out(EXAMPLES)
        if not is_tty() or args.yes:
            raise SystemExit("--api-base is required (see the examples above)")
        base = ask("API base URL", default=saved_base)

    if not key:
        out()
        out("No --api-key given.")
        out(EXAMPLES)
        if not is_tty() or args.yes:
            raise SystemExit("--api-key is required (see the examples above)")
        key = ask("API key", secret=True)

    return gateway.normalize_base(base, keep_v1=False), key


def fetch_models(base: str, key: str, *, required: bool,
                 timeout: float = 30.0) -> list[gateway.Model]:
    try:
        models = gateway.list_models(base, key, timeout=timeout)
    except gateway.GatewayError as e:
        if required:
            raise SystemExit(f"could not list models: {e}")
        note(f"  ! could not list models ({e})")
        note("    continuing -- you can point Codex at the gateway anyway.")
        return []
    note(f"  gateway OK: {len(models)} models served")
    return models


def gateway_credentials(args) -> tuple[str, str]:
    """Base + key, reusing whatever a previous run stored.

    `/model/info` and `/model/update` are master-key endpoints, so this is the
    same credential the rest of the tool already persists.
    """
    cfg = detect.codex_config()
    return resolve_credentials(args, codex_mod.saved_base(cfg),
                               codex_mod.saved_key(cfg))


# --------------------------------------------------------------------------- #
# the main setup flow
# --------------------------------------------------------------------------- #

def report_version(rep: detect.Report, *, quiet_ok: bool = False) -> None:
    """Print which codex this toolkit is talking to, and whether it fits.

    The catalog format is version-sensitive -- the 0.145 rename of
    `supports_reasoning_summaries` is what broke a 0.144.1 box -- so the version
    is part of every run's output rather than something to dig out later.
    """
    ok, note = codex_mod.version_note(rep.codex_version)
    if not rep.codex_version:
        if not quiet_ok:
            note(f"  ! {note}")
        return
    if ok and quiet_ok:
        return
    out(f"  {'' if ok else '! '}codex: {note}")


def run_setup(args, rep: detect.Report) -> int:
    cfg = rep.codex_config or detect.codex_config()

    rule("Codex CLI")
    report_version(rep)

    base, key = resolve_credentials(args, codex_mod.saved_base(cfg),
                                    codex_mod.saved_key(cfg))
    base_v1 = gateway.normalize_base(base, keep_v1=True)

    models = fetch_models(base, key, required=args.list_models or args.switch_model)

    if args.list_models:
        for m in models:
            flag = "" if m.is_chat_capable else "   <- not chat-capable"
            out(f"    {m.id}{flag}")
        if not args.switch_model:
            return 0

    current = codex_mod.saved_model(cfg)
    if current:
        out(f"  current model in config.toml: {current}")

    model = codex_mod.clean_model_id(args.model) if args.model else None
    if not model and models:
        if args.switch_model or (is_tty() and not args.yes):
            model = pick_model(models, "pick the model Codex should use")
        else:
            # No terminal and no --model: keep whatever is pinned, else take the
            # first model the gateway serves. A wizard that fails because it
            # could not ask a question is worse than one that picks sensibly and
            # says so.
            model = current or next((m.id for m in models if m.is_chat_capable), None)
            if model:
                note(f"  no --model given; using {model!r}")
    if not model:
        out("  ! no model chosen; pass --model <id> or run --switch-model")
        return 1

    if models and model not in {m.id for m in models}:
        out(f"  ! the gateway does not serve {model!r}; listing what it does:")
        for m in models:
            out(f"      {m.id}")
        return 1

    # Codex 0.150 only speaks /responses. Verify the gateway serves it before
    # writing a config that would fail on the first prompt.
    if not args.skip_probe:
        out(f"  probing /v1/responses with {model} ...")
        try:
            result = gateway.probe_wire_api(base_v1, key, model)
        except gateway.GatewayError as e:
            out(f"  ! probe failed: {e}")
        else:
            if result.get("responses"):
                out("  /v1/responses  OK")
            else:
                out("  ! /v1/responses NOT served by this gateway")
                if result.get("chat"):
                    out("    /v1/chat/completions works, but Codex 0.150 dropped")
                    out('    wire_api = "chat" -- Codex cannot drive this gateway')
                    out("    until it proxies /responses (LiteLLM does).")
                return 1

    detect.ensure_home()
    inline_key = None if args.use_env_key else key
    snapshot([cfg])
    summary = codex_mod.apply_config(
        cfg, base_v1, model,
        pid=args.provider_id, env_key=args.env_key, inline_key=inline_key,
        profile=args.profile,
    )

    out()
    out(f"  wrote {cfg}")
    out(f"    [{summary['provider_table']}]")
    out(f"      base_url = {base_v1}")
    out(f'      wire_api = "{codex_mod.WIRE_API}"')
    out("      requires_openai_auth = false")
    if inline_key:
        out(f"      experimental_bearer_token = {_mask(key)}   (inline)")
    else:
        name = args.env_key or codex_mod.env_key_name(args.provider_id)
        out(f"      env_key  = {name}")
    if summary.get("profile"):
        out(f"    [profiles.{summary['profile']}]  "
            f"-> use with `codex --profile {summary['profile']}`")
    elif model:
        out(f'    model = "{model}"')
        out(f'    model_provider = "{args.provider_id}"')

    if not inline_key:
        name = args.env_key or codex_mod.env_key_name(args.provider_id)
        where = codex_mod.set_env_var(name, key)
        out()
        out(f"  api key: {where}")

    if models:
        write_catalog(cfg, models)
        strip_blocking_keys(cfg)
        warn_gateway_params(base, key)

    # The login gate is independent of everything above: without auth.json Codex
    # asks for a ChatGPT account no matter what the provider table says.
    if not args.skip_login:
        # A real ChatGPT sign-in already satisfies that gate, and a private
        # provider never reads the tokens it holds -- so overwriting it would
        # throw away a working account for nothing. Everything else (missing,
        # empty, or an API key an earlier run of this tool left behind) is ours
        # to write.
        state, _ = codex_auth.status()
        write_login(key, quiet_ok=True, force=state != "CHATGPT")

    if args.install_wrapper:
        install_wrapper(args)

    write_helpers()
    out()
    out("  next: run `codex`, then `/model` to switch between the gateway's models.")
    return 0


def write_helpers() -> list[Path]:
    """Write `codexcli/bin/codex-model`, the "switch model later" shortcut.

    Best-effort: a read-only checkout must not fail the setup that already
    succeeded, so a write error is reported and swallowed.
    """
    try:
        paths = codex_mod.write_switch_helpers(HERE)
    except OSError as e:
        note(f"  ! could not write the helper scripts ({e})")
        return []
    for p in paths:
        note(f"  wrote {p}")
    return paths


def write_catalog(cfg: Path, models: list[gateway.Model]) -> list[str]:
    """Generate the catalog JSON and point config.toml at it."""
    out()
    path = catalog_target(cfg)
    try:
        slugs = codex_mod.write_catalog(path, models)
    except ValueError as e:
        out(f"  ! {e}; leaving Codex's own model list in place")
        return []

    codex_mod.apply_catalog(cfg, path)
    codex_mod.mark_refreshed(models, time.time())

    out(f"  wrote {path}  (model catalog -- what `/model` lists)")
    out(f"    {len(slugs)} model(s), and nothing else:")
    for s in slugs:
        raw = next((m.raw or {} for m in models if m.id == s), {})
        levels, default = reasoning.levels_for(s, raw)
        shown = ",".join(lv["effort"] for lv in levels) or "no reasoning menu"
        out(f"      {s:28} reasoning: {shown}")
    hidden = [m.id for m in models if not m.is_chat_capable]
    if hidden:
        out(f"    ({len(hidden)} non-chat endpoint(s) omitted: {', '.join(hidden)})")
    return slugs


def catalog_target(cfg: Path) -> Path:
    """Where the catalog JSON goes.

    A catalog a previous run of this tool wrote is reused so the file stays where
    the user last saw it; otherwise we fall back to `$CODEX_HOME/gateway-models.json`.
    A `model_catalog_json` pointing anywhere ELSE is left alone -- that one is the
    user's, and overwriting it would be a surprise.
    """
    if codex_mod.catalog_is_ours(cfg):
        previous = codex_mod.existing_catalog_path(cfg)
        if previous:
            return Path(previous)
    return codex_mod.catalog_path()


def strip_blocking_keys(cfg: Path) -> None:
    """Remove root-level keys that stop the catalog's reasoning from working.

    Two root-level keys in config.toml silently override the per-model reasoning
    levels we just wrote into the catalog:

      * `model_reasoning_effort` -- a global default that outranks every model's
        `default_reasoning_level`. When it is set (often to ``"none"`` by an
        older OpenAI-only config), the reasoning menu disappears for *every*
        model, not just the one the user intended.
      * `service_tier` -- an OpenAI-only concept a private gateway may reject.

    Deleting them here means the catalog we just wrote actually takes effect.
    """
    removed_reasoning = codex_mod.strip_reasoning_override(cfg)
    if removed_reasoning is not None:
        out()
        out(f'  removed root-level model_reasoning_effort = "{removed_reasoning}"')
        out("    it was masking every model's default_reasoning_level in the catalog,")
        out("    so the reasoning menu was hidden or stuck at one level for all models.")

    for k in codex_mod.strip_openai_only_keys(cfg):
        out()
        out(f"  removed root-level {k} (OpenAI-only; a private gateway may reject it)")


def warn_gateway_params(base: str, key: str) -> None:
    """Non-fatal heads-up when the gateway would 400 every Codex turn.

    Worth doing immediately after writing the config: otherwise the failure
    surfaces as an opaque error inside codex, and the fix is a different command
    that nothing has pointed at yet.
    """
    try:
        report = litellm_admin.build_report(litellm_admin.fetch_model_info(base, key))
    except gateway.GatewayError:
        return  # never fatal here; --status reports this properly
    todo = report.todo
    if not todo:
        return
    out()
    out(f"  ! {len(todo)} private model(s) lack gateway parameters Codex needs, so")
    out("    turns against them fail (HTTP 400 on reasoning, HTTP 500 on metadata):")
    for p in todo:
        out(f"      {p.model_name:28} {litellm_admin.describe(p)}")
    out(f"    fix: python3 {self_cmd()} --emit-gateway-config")


# --------------------------------------------------------------------------- #
# login (auth.json)
# --------------------------------------------------------------------------- #

def write_login(key: str, *, quiet_ok: bool = False, force: bool = True) -> bool:
    """Write `auth.json` so Codex stops asking for a ChatGPT account.

    The value is the GATEWAY's key: it is what the private provider table already
    authenticates with, so this introduces no new secret -- it moves one that is
    already on this machine (in config.toml) into the file Codex actually
    consults for "am I signed in".
    """
    state, detail = codex_auth.status()
    if state == "API_KEY" and not force:
        note(f"  sign-in: already an API key ({detail})")
        return False
    if state == "CHATGPT" and not force:
        note(f"  sign-in: a real ChatGPT login is present; leaving it alone ({detail})")
        return False

    path, backup = codex_auth.write_api_key_auth(key)
    if not quiet_ok:
        out()
    out(f"  wrote {path}  (sign-in, so Codex never shows a login prompt)")
    out(f"    OPENAI_API_KEY = {_mask(key)}   (the gateway key, not an OpenAI one)")
    if backup:
        out(f"    previous file kept at {backup}")
    return True


def run_fix_login(args, rep: detect.Report) -> int:
    rule("Codex sign-in")

    if args.restore:
        if codex_auth.restore_from_backup():
            out(f"  restored {codex_auth.auth_path()} from its .bak")
        else:
            out("  no auth.json.bak to restore -- nothing changed")
        return 0

    state, detail = codex_auth.status()
    out(f"  before: {state}  ({detail})")

    base, key = gateway_credentials(args)

    # Writing a key the gateway rejects would be worse than writing nothing:
    # Codex would look signed in and then fail every turn. Prove it works first.
    out()
    out(f"  checking the key against {base} ...")
    try:
        models = gateway.list_models(base, key)
    except gateway.GatewayError as e:
        raise SystemExit(
            f"  ! the key was refused, so auth.json was NOT written:\n      {e}"
        )
    out(f"  key OK: the gateway serves {len(models)} model(s)")

    write_login(key, force=args.force)
    state, detail = codex_auth.status()
    out()
    out(f"  after: {state}  ({detail})")
    return 0


# --------------------------------------------------------------------------- #
# the wrapper: refresh the catalog on every launch
# --------------------------------------------------------------------------- #

def install_wrapper(args) -> tuple[Path, Path] | None:
    """Write `~/.local/bin/codex`. Returns (wrapper, real binary), or None."""
    real = codex_mod.find_real_codex()
    out()
    if not real:
        out("  ! could not find the real `codex` binary, so no wrapper was installed.")
        out("    Install codex first (npm i -g @openai/codex), or run")
        out(f"    `python3 {self_cmd()} --install-wrapper --real-codex /path/to/codex`.")
        return None

    target = codex_mod.wrapper_path()
    if target.exists() and not codex_mod.wrapper_is_ours(target):
        out(f"  ! {target} exists and was not written by this tool; refusing to")
        out("    overwrite it. Move it aside or pass --wrapper-dir to install")
        out("    somewhere else.")
        return None

    target = codex_mod.install_wrapper(real, ttl=args.refresh_ttl)
    out(f"  wrote {target}")
    out(f"    exec's {real}")
    out(f"    refreshes the catalog at most every {args.refresh_ttl}s, and never")
    out("    blocks codex if the gateway is slow or down")

    if not codex_mod.path_has_wrapper_dir():
        out()
        out(f"  ! {codex_mod.wrapper_dir()} is not on your PATH, so `codex` still")
        out("    runs the real binary and the catalog will not auto-refresh. Add it:")
        out(f'      echo \'export PATH="$HOME/.local/bin:$PATH"\' >> ~/.bashrc')
    return target, real


def run_install_wrapper(args, rep: detect.Report) -> int:
    rule("codex wrapper (auto-refresh)")
    if args.uninstall_wrapper:
        if codex_mod.uninstall_wrapper():
            out(f"  removed {codex_mod.wrapper_path()}")
        else:
            out(f"  no wrapper written by this tool at {codex_mod.wrapper_path()}")
        return 0

    if args.real_codex:
        real = Path(args.real_codex).expanduser()
        # `is_file()` is False for a symlink in some Windows toolchains and for
        # a script with no read bit; what actually matters is that exec works.
        if not os.access(real, os.X_OK):
            raise SystemExit(f"  ! {real} is not executable")
        codex_mod.wrapper_path().parent.mkdir(parents=True, exist_ok=True)
        target = codex_mod.install_wrapper(real, ttl=args.refresh_ttl)
        out(f"  wrote {target}")
        out(f"    exec's {real}")
        return 0

    result = install_wrapper(args)
    return 0 if result else 1


# --------------------------------------------------------------------------- #
# --sync: the fast path the wrapper calls
# --------------------------------------------------------------------------- #

def run_sync(args, rep: detect.Report) -> int:
    """Refresh the catalog if it is stale. Always returns 0, never raises.

    This runs before every `codex` launch, so the contract is: leave the machine
    in a usable state and get out of the way. A failure here means the user gets
    the catalog from last time, which is exactly what should happen -- there is
    no version of "the gateway is unreachable" that should stop codex starting.
    """
    cfg = rep.codex_config or detect.codex_config()
    base = codex_mod.saved_base(cfg)
    key = codex_mod.saved_key(cfg)
    if not base or not key:
        # Not configured yet -- `--setup` has not been run. Nothing to do, and
        # saying so on every launch would be noise.
        return 0

    age = codex_mod.last_refresh_age(time.time())
    ttl = 0 if args.force else args.refresh_ttl
    if age is not None and age < ttl:
        return 0

    try:
        models = gateway.list_models(base, key, timeout=SYNC_TIMEOUT)
    except gateway.GatewayError as e:
        note(f"  ! catalog not refreshed ({e}); using the one from last time")
        return 0

    if not models:
        return 0

    path = catalog_target(cfg)
    try:
        slugs = codex_mod.write_catalog(path, models)
    except (ValueError, OSError) as e:
        note(f"  ! could not write the catalog: {e}")
        return 0

    codex_mod.apply_catalog(cfg, path)
    codex_mod.mark_refreshed(models, time.time())

    # A pinned model that the gateway no longer serves would fail on the first
    # prompt with no explanation. Repoint it at something that exists.
    current = codex_mod.saved_model(cfg)
    if current and current not in slugs:
        fallback = next((s for s in slugs), None)
        note(f"  ! {current!r} is no longer served; switching to {fallback!r}")
        if fallback:
            codex_mod.apply_config(cfg, base, fallback)

    note(f"  catalog refreshed: {len(slugs)} model(s)")

    # Only reached when a refresh was actually due (the wrapper short-circuits
    # the throttled case before we are ever exec'd), so paying for one
    # `codex --version` here is cheap. A binary older than the supported range
    # writes a catalog its own parser will reject, and the user would otherwise
    # meet that as an unexplained startup failure.
    ok, version_note = codex_mod.version_note(detect.codex_version())
    if not ok:
        note(f"  ! {version_note}")
    return 0


# --------------------------------------------------------------------------- #
# --status
# --------------------------------------------------------------------------- #

def run_status(rep: detect.Report, args) -> int:
    out(rep.render())
    cfg = rep.codex_config or detect.codex_config()

    rule("config.toml")
    if not cfg.exists():
        out(f"  {cfg} does not exist -- run the wizard first")
        return 0
    out(f"  {cfg}")
    table = codex_mod.provider_table()
    doc = TomlFile(cfg)
    for key in ("model", "model_provider", codex_mod.CATALOG_KEY):
        value = doc.get(key)
        out(f"    {key:22} {value if value is not None else '(unset)'}")
    for key in ("name", "base_url", "wire_api", "requires_openai_auth",
                "env_key", "experimental_bearer_token"):
        value = doc.get(key, table=table)
        if value is None:
            continue
        if key == "experimental_bearer_token":
            value = _mask(value.strip("'\""))
        out(f"    [{table}] {key} = {value}")

    override = doc.get(codex_mod.REASONING_OVERRIDE_KEY)
    if override is not None:
        out()
        out(f"  ! root-level {codex_mod.REASONING_OVERRIDE_KEY} = {override}")
        out("    it overrides every catalog entry's default_reasoning_level.")

    rule("model catalog")
    cat = codex_mod.existing_catalog_path(cfg)
    if not cat:
        out("  model_catalog_json is unset -- `/model` will list OpenAI's models,")
        out("  none of which this gateway serves.")
        return 0
    slugs = codex_mod.read_catalog(Path(cat))
    out(f"  {cat}")
    out(f"    {len(slugs)} model(s): {', '.join(slugs) if slugs else '(empty)'}")
    age = codex_mod.last_refresh_age(time.time())
    out(f"    last refreshed: "
        f"{f'{int(age)}s ago' if age is not None else 'never by this tool'}")

    rule("sign-in")
    state, detail = codex_auth.status()
    out(f"  {state}  ({detail})")

    rule("per-model overrides")
    levels = reasoning.load()
    mods = modalities.load()
    if not levels and not mods:
        out("  none -- every model uses the measured/default levels")
    for m, entry in sorted(levels.items()):
        names = ", ".join(lv["effort"] for lv in (entry.get("levels") or [])) or "(none)"
        out(f"  reasoning   {m:28} {names}  default={entry.get('default') or '-'}")
    for m, entry in sorted(mods.items()):
        out(f"  modalities  {m:28} {', '.join(modalities.normalize_entries(entry.get('input_modalities')))}")

    rule("codex wrapper")
    w = codex_mod.wrapper_path()
    if codex_mod.wrapper_is_ours(w):
        out(f"  {w}  (installed)")
        out(f"  on PATH: {'yes' if codex_mod.path_has_wrapper_dir() else 'NO -- see --install-wrapper'}")
    else:
        out("  not installed -- the catalog only updates when you run --sync")
    return 0


def run_restore(rep: detect.Report) -> int:
    rule("restore")
    cfg = rep.codex_config or detect.codex_config()

    # Read the catalog facts BEFORE restoring: restore_from_backup rewrites
    # config.toml, so afterwards the key is usually gone.
    owns_catalog = codex_mod.catalog_is_ours(cfg)
    catalog_file = codex_mod.existing_catalog_path(cfg)

    if codex_mod.restore_from_backup(cfg):
        out(f"  restored {cfg} from {cfg.name}.bak")
    else:
        out(f"  no backup for {cfg} -- leaving it as is")

    if cfg.exists() and codex_mod.clear_catalog(cfg):
        out(f"  dropped {codex_mod.CATALOG_KEY} from {cfg}")
    if owns_catalog and catalog_file and codex_mod.remove_catalog_file(Path(catalog_file)):
        out(f"  removed generated catalog {catalog_file}")

    if codex_auth.restore_from_backup():
        out(f"  restored {codex_auth.auth_path()} from its .bak")

    if codex_mod.uninstall_wrapper():
        out(f"  removed wrapper {codex_mod.wrapper_path()}")

    out("  (per-model reasoning/modalities overrides are kept -- drop them with")
    out("   --reasoning-clear all / --modalities-clear all)")
    return 0


# --------------------------------------------------------------------------- #
# gateway-side model parameters
# --------------------------------------------------------------------------- #

def render_report(rep: litellm_admin.Report) -> None:
    if rep.ok:
        out()
        out(f"  already accept Codex's Responses fields ({len(rep.ok)}):")
        for p in rep.ok:
            out(f"      {p.model_name}")
    if rep.skipped:
        out()
        out(f"  not applicable ({len(rep.skipped)}):")
        for p in rep.skipped:
            out(f"      {p.model_name:28} {p.reason}")
    if rep.todo:
        out()
        out(f"  NEED THE FIX ({len(rep.todo)}) -- Codex turns fail against these:")
        for p in rep.todo:
            out(f"      {p.model_name:28} {litellm_admin.describe(p)}")


def run_gateway_config(args, rep: detect.Report, *, apply: bool) -> int:
    rule("gateway model parameters")
    out("  Codex only speaks /v1/responses, and every turn carries two fields a")
    out("  custom_openai model definition rejects by default:")
    out(f"    reasoning.effort  -> {litellm_admin.REASONING_PARAM}, refused as an")
    out("                         unsupported parameter -> HTTP 400")
    out(f"    {litellm_admin.CLIENT_METADATA_PARAM}   -> forwarded into the OpenAI SDK,")
    out("                         which has no such kwarg   -> HTTP 500")
    out()
    out("  Both are answered with litellm_params: allowed_openai_params and")
    out(f"  {litellm_admin.DROP_PARAMS_KEY}. Codex's binary cannot send either, so")
    out("  it has to live on the gateway.")
    out()

    base, key = gateway_credentials(args)
    endpoint = gateway.normalize_base(base, keep_v1=False) + litellm_admin.INFO_PATH
    out(f"  model definitions: {endpoint}")
    try:
        entries = litellm_admin.fetch_model_info(base, key)
    except gateway.GatewayError as e:
        raise SystemExit(f"  ! {e}")
    report = litellm_admin.build_report(entries)
    out(f"  gateway OK: {len(entries)} model definition(s)")
    render_report(report)

    if not report.todo:
        out()
        out("  nothing to do -- every private model already accepts Codex's fields.")
        return 0

    if not apply:
        out()
        out("  Not changing anything. Run these where you can reach the gateway:")
        out()
        out(litellm_admin.emit_commands(base, report.todo))
        out()
        out("  ...or re-run with --apply-gateway-config to send them from here.")
        out("  (POST /model/update REPLACES litellm_params, so each command above")
        out("   resends the model's full parameter set with the one key added.)")
        out()
        out("  NOTE: this is a shared gateway -- applying it changes every user's")
        out("  requests, not just yours.")
        return 0

    if not args.yes and not confirm(
            f"  update {len(report.todo)} model definition(s) on {base}?", default=False):
        out("  aborted -- nothing sent")
        return 1

    out()
    out(f"  applying to {len(report.todo)} model(s) ...")
    failures = 0
    for p in report.todo:
        try:
            litellm_admin.apply_plan(base, key, p)
        except gateway.GatewayError as e:
            failures += 1
            out(f"      ! {p.model_name}: {e}")
        else:
            out(f"      ok {p.model_name}")
    out()
    if failures:
        out(f"  {failures} model(s) could NOT be updated; see above.")
        return 1
    out("  done. Verify with: --emit-gateway-config (it should report nothing to do).")
    return 0


# --------------------------------------------------------------------------- #
# reasoning levels
# --------------------------------------------------------------------------- #

def _configured_models(args, what: str) -> tuple[list[gateway.Model], str] | None:
    """Fetch the model list and resolve which model the user means."""
    models = fetch_models(*gateway_credentials(args), required=True)
    model = args.reasoning_model if what == "reasoning" else args.modalities_model
    if not model:
        if not is_tty():
            raise SystemExit(f"--{what}-model is required (or run it on a terminal)")
        model = pick_model(models, f"which model's {what}?")
    if model not in {m.id for m in models}:
        out(f"  ! the gateway does not serve {model!r}; listing what it does:")
        for m in models:
            out(f"      {m.id}")
        return None
    return models, model


def run_configure_reasoning(args, rep: detect.Report) -> int:
    rule("per-model thinking levels")

    if args.reasoning_clear:
        store = reasoning.load()
        if args.reasoning_clear == "all":
            n = reasoning.clear_all()
            out(f"  cleared {n} override(s)")
            return 0
        m = args.reasoning_clear
        out(f"  {'cleared ' + m if reasoning.clear(m) else 'no override for ' + m}")
        return 0

    picked = _configured_models(args, "reasoning")
    if not picked:
        return 1
    models, model = picked

    raw = next((m.raw or {} for m in models if m.id == model), {})
    current_levels, current_default = reasoning.levels_for(model, raw)
    out()
    out(f"  {model}")
    if reasoning.is_openai_official(model):
        out("  this is an OpenAI model -- its levels come from Codex's own catalog")
        out("  and are not user-configurable. Showing them anyway:")
        for lv in current_levels:
            out(f"      {lv['effort']:12} {lv['description']}")
        return 0
    out(f"  now: {', '.join(lv['effort'] for lv in current_levels) or '(none)'}"
        f"  default={current_default or '-'}")
    out(f"  shorthand profiles: private={','.join(reasoning.PROFILES['private'])}"
        f" (default)  xhigh={','.join(reasoning.PROFILES['xhigh'])}  none=(no menu)")

    if args.reasoning_levels is not None:
        wanted = list(reasoning.resolve_profile(args.reasoning_levels))
    else:
        out()
        out(f"  available efforts: {', '.join(reasoning.KNOWN_EFFORTS)}")
        out("  (comma-separated; `none` or an empty answer means no reasoning menu)")
        answer = ask("levels",
                     default=",".join(lv["effort"] for lv in current_levels)
                     or ",".join(reasoning.DEFAULT_EFFORTS))
        wanted = list(reasoning.resolve_profile(answer))

    unknown = [x for x in wanted if x not in reasoning.KNOWN_EFFORTS]
    if unknown:
        out(f"  ! not valid Codex reasoning levels: {', '.join(unknown)}")
        out(f"    valid: {', '.join(reasoning.KNOWN_EFFORTS)}")
        return 1

    default = args.reasoning_default
    if wanted and not default:
        if is_tty() and not args.yes:
            default = ask("default level", default=current_default or reasoning.DEFAULT_EFFORT
                          if (current_default in wanted or reasoning.DEFAULT_EFFORT in wanted)
                          else wanted[0])
        else:
            default = reasoning.DEFAULT_EFFORT if reasoning.DEFAULT_EFFORT in wanted else wanted[0]

    try:
        entry = reasoning.configure(model, wanted, default)
    except ValueError as e:
        out(f"  ! {e}")
        return 1

    levels = entry.get("levels") or []
    out()
    out(f"  wrote {reasoning.store_path()}")
    if not levels:
        out(f"    {model}: no reasoning menu")
    else:
        out(f"    {model}: {', '.join(lv['effort'] for lv in levels)}"
            f"  default={entry.get('default')}")
    out()
    out("  next: --refresh-models to regenerate the catalog (or just run codex,")
    out("        which refreshes it for you once --install-wrapper is in place).")
    return 0


# --------------------------------------------------------------------------- #
# input modalities
# --------------------------------------------------------------------------- #

def run_configure_modalities(args, rep: detect.Report) -> int:
    rule("per-model input types (images)")

    if args.modalities_clear:
        if args.modalities_clear == "all":
            store = modalities.load()
            for m in list(store):
                modalities.clear(m)
            out(f"  cleared {len(store)} override(s)")
            out("  (cleared models fall back to the measured table, not to text-only)")
            return 0
        m = args.modalities_clear
        out(f"  {'cleared ' + m if modalities.clear(m) else 'no override for ' + m}")
        out("  (cleared models fall back to the measured table, not to text-only)")
        return 0

    picked = _configured_models(args, "modalities")
    if not picked:
        return 1
    models, model = picked

    raw = next((m.raw or {} for m in models if m.id == model), {})
    current = modalities.modalities_for(model, raw)
    out()
    out(f"  {model}")
    if modalities.is_openai_official(model):
        out("  this is an OpenAI model -- its input types come from Codex's own")
        out("  catalog and are not user-configurable. Showing them anyway:")
        out(f"      {', '.join(current)}")
        return 0
    out(f"  now: {', '.join(current)}")

    if args.modalities is not None:
        wanted = args.modalities
    else:
        out()
        out(f"  available: {', '.join(modalities.KNOWN_MODALITIES)}")
        out("  add 'image' only if the model really reads images -- accepting the")
        out("  request is not enough; verify with --probe-modalities")
        wanted = ask("input types", default=",".join(current))

    try:
        chosen = modalities.configure(model, wanted)
    except ValueError as e:
        out(f"  ! {e}")
        return 1

    out()
    out(f"  wrote {modalities.store_path()}")
    out(f"    {model}: {', '.join(chosen)}")
    out()
    out("  next: --refresh-models to regenerate the catalog.")
    return 0


# Vision inference is slower than text, and these backends queue.
PROBE_TIMEOUT = 180.0


def run_probe_modalities(args, rep: detect.Report) -> int:
    rule("measure which models can actually see images")
    out("  Sends a solid red and a solid blue image and asks each model for the")
    out("  colour. Accepting the request proves nothing -- a model can return 200")
    out("  and describe a colour that is not there -- so the test is whether it")
    out("  tells the two apart.")
    out()

    base, key = gateway_credentials(args)
    models = fetch_models(base, key, required=True)
    names = args.probe_models.split(",") if args.probe_models else None
    targets = [m.id for m in models
               if m.is_chat_capable and (not names or m.id in names)]
    if not targets:
        out("  ! no chat models to probe")
        return 1

    images = modalities.probe_images()
    url = gateway.normalize_base(base, keep_v1=True) + "/chat/completions"

    verdicts: dict[str, bool] = {}
    for name in targets:
        answers: dict[str, str] = {}
        failure = ""
        for colour, b64 in images.items():
            body = {"model": name, "max_tokens": 300, "messages": [{"role": "user",
                    "content": [
                        {"type": "text", "text": modalities.PROBE_QUESTION},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{b64}"}}]}]}
            try:
                status, raw = gateway._request(url, key, method="POST", body=body,
                                               timeout=PROBE_TIMEOUT)
                payload = json.loads(raw.decode("utf-8", "replace"))
            except Exception as e:  # noqa: BLE001
                failure = f"{type(e).__name__}: {e}"
                break
            if status != 200:
                message = ""
                if isinstance(payload, dict):
                    message = str((payload.get("error") or {}).get("message") or "")
                # The upstream's own words are the useful part; the gateway wraps
                # them in a paragraph of LiteLLM context.
                failure = f"HTTP {status} {message.split('Fallbacks=')[0].strip()[:120]}"
                break
            message = payload["choices"][0]["message"]
            answers[colour] = (message.get("content") or "").strip()

        if failure:
            verdicts[name] = False
            out(f"  {name:26} FAILED   {failure}")
            continue

        can_see, why = modalities.judge(answers)
        verdicts[name] = can_see
        said = " / ".join(f"{c}={answers.get(c, '')[:24]!r}" for c in ("red", "blue"))
        out(f"  {name:26} {'IMAGE' if can_see else 'TEXT ':6} {said}")
        out(f"  {'':26} {why}")

    out()
    can = [n for n, v in verdicts.items() if v]
    cannot = [n for n, v in verdicts.items() if not v]
    out(f"  can see images ({len(can)}): {', '.join(can) or '(none)'}")
    out(f"  cannot ({len(cannot)}): {', '.join(cannot) or '(none)'}")
    if can:
        out()
        out("  Enable per model with:")
        for n in can:
            out(f"      python3 {self_cmd()} --configure-modalities "
                f"--modalities-model {n} --modalities text,image")
    return 0


# --------------------------------------------------------------------------- #
# refresh / switch
# --------------------------------------------------------------------------- #

def run_refresh(args, rep: detect.Report) -> int:
    """Re-fetch the gateway list and rewrite only the catalog JSON."""
    rule("refresh model catalog")
    cfg = rep.codex_config or detect.codex_config()
    base, key = gateway_credentials(args)
    models = fetch_models(base, key, required=True)

    if not cfg.exists():
        out(f"  ! no {cfg} -- run the wizard first to create it")
        return 1
    snapshot([cfg])
    write_catalog(cfg, models)
    strip_blocking_keys(cfg)
    out()
    out("  next: run `codex` -- `/model` picks this up.")
    return 0


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    global QUIET

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )
    ap.add_argument("--api-base", help="gateway base URL, e.g. http://10.18.219.156:4000")
    ap.add_argument("--api-key", help="gateway API key, e.g. sk-XXXXXXXX")
    ap.add_argument("--model", help="model id to pin (see --list-models)")
    ap.add_argument("--list-models", action="store_true",
                    help="print every model the gateway serves and exit")
    ap.add_argument("--switch-model", action="store_true",
                    help="pick a model interactively and write it to config.toml")
    ap.add_argument("--refresh-models", action="store_true",
                    help="re-fetch the gateway model list and rewrite only the catalog")
    ap.add_argument("--sync", action="store_true",
                    help="refresh the catalog if stale, quietly; never fails "
                         "(this is what the installed codex wrapper runs)")
    ap.add_argument("--detect", action="store_true",
                    help="show config file locations and exit")
    ap.add_argument("--status", action="store_true",
                    help="show current configuration and exit")
    ap.add_argument("--restore", action="store_true",
                    help="put the .bak files back, drop the catalog, remove the wrapper")
    ap.add_argument("--yes", "-y", action="store_true", help="never prompt; use defaults")
    ap.add_argument("--quiet", "-q", action="store_true",
                    help="suppress progress output (errors still print)")

    ap.add_argument("--use-env-key", action="store_true",
                    help="put the key in an environment variable instead of inline in "
                         "config.toml. Inline is the default here: a non-interactive "
                         "shell (docker exec, cron) never sources ~/.bashrc and would "
                         "see no variable at all")
    ap.add_argument("--env-key", help="name of the env var holding the key")
    ap.add_argument("--provider-id", default=codex_mod.DEFAULT_PROVIDER_ID,
                    help="provider table id (default: private)")
    ap.add_argument("--profile",
                    help="write a [profiles.<name>] instead of the root config")
    ap.add_argument("--skip-probe", action="store_true",
                    help="skip the /v1/responses reachability probe")
    ap.add_argument("--skip-login", action="store_true",
                    help="do not write auth.json (Codex will ask for a login)")
    ap.add_argument("--strip-openai-keys", action="store_true",
                    help="delete OpenAI-only keys such as service_tier from config.toml")

    ap.add_argument("--fix-login", action="store_true",
                    help="write auth.json so Codex never shows a login prompt "
                         "(use with --restore to undo, --force to rewrite)")
    ap.add_argument("--force", action="store_true",
                    help="rewrite auth.json even if a sign-in already exists; also "
                         "makes --sync ignore its throttle")

    ap.add_argument("--install-wrapper", action="store_true",
                    help="install ~/.local/bin/codex, which refreshes the catalog "
                         "before each launch")
    ap.add_argument("--uninstall-wrapper", action="store_true",
                    help="remove the wrapper installed by --install-wrapper")
    ap.add_argument("--real-codex",
                    help="the codex binary the wrapper should exec, when it cannot "
                         "be found automatically")
    ap.add_argument("--refresh-ttl", type=int, default=codex_mod.REFRESH_TTL_SECONDS,
                    help=f"seconds a refreshed catalog is trusted for "
                         f"(default: {codex_mod.REFRESH_TTL_SECONDS})")

    ap.add_argument("--emit-gateway-config", action="store_true",
                    help="print the /model/update calls that let private models accept "
                         "Codex's reasoning_effort and drop client_metadata")
    ap.add_argument("--apply-gateway-config", action="store_true",
                    help="same, but actually send them (needs the LiteLLM master key)")

    ap.add_argument("--configure-reasoning", action="store_true",
                    help="set which thinking levels a private model offers")
    ap.add_argument("--reasoning-model", help="model id to configure thinking levels for")
    ap.add_argument("--reasoning-levels",
                    help="comma-separated efforts (e.g. low,high,max), or a shorthand: "
                         "private / three / xhigh / none; an empty string means "
                         "no reasoning menu")
    ap.add_argument("--reasoning-default",
                    help="default effort (must be one of --reasoning-levels)")
    ap.add_argument("--reasoning-clear", metavar="MODEL",
                    help="drop the override for MODEL, or `all` for every model")

    ap.add_argument("--configure-modalities", action="store_true",
                    help="set which input types (text/image) a private model accepts")
    ap.add_argument("--modalities-model", help="model id to configure input types for")
    ap.add_argument("--modalities",
                    help="comma-separated types, e.g. text,image; `text` alone blocks "
                         "attachments for that model")
    ap.add_argument("--modalities-clear", metavar="MODEL",
                    help="drop the override for MODEL, or `all` for every model")
    ap.add_argument("--probe-modalities", action="store_true",
                    help="measure which gateway models can really read an image, by "
                         "asking them to tell red from blue")
    ap.add_argument("--probe-models",
                    help="comma-separated model ids to probe (default: every chat model)")

    args = ap.parse_args(argv)
    # --sync is what the `codex` wrapper runs, so it is silent by default -- but
    # `--sync --force` is how a human debugs it, and that one should say what it
    # did.
    QUIET = args.quiet or (args.sync and not args.force)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    rep = detect.discover(probe_version=not args.sync)

    # --sync is the only command that must never raise: it is on the critical
    # path of `codex` starting. Everything else is allowed to fail loudly.
    if args.sync:
        try:
            return run_sync(args, rep)
        except Exception as e:  # noqa: BLE001
            note(f"  ! catalog sync skipped ({type(e).__name__}: {e})")
            return 0

    if args.detect:
        out(rep.render())
        return 0
    if args.status:
        return run_status(rep, args)
    if args.restore:
        return run_restore(rep)
    if args.strip_openai_keys:
        cfg = rep.codex_config or detect.codex_config()
        removed = codex_mod.strip_openai_only_keys(cfg)
        out(f"  removed from {cfg}: {', '.join(removed) if removed else '(nothing)'}")
        return 0
    if args.install_wrapper or args.uninstall_wrapper:
        return run_install_wrapper(args, rep)

    # These touch no client file, so they must not drag in the wizard.
    if args.emit_gateway_config or args.apply_gateway_config:
        return run_gateway_config(args, rep, apply=args.apply_gateway_config)
    if args.fix_login:
        return run_fix_login(args, rep)
    if args.configure_reasoning or args.reasoning_clear:
        return run_configure_reasoning(args, rep)
    if args.configure_modalities or args.modalities_clear:
        return run_configure_modalities(args, rep)
    if args.probe_modalities:
        return run_probe_modalities(args, rep)
    if args.refresh_models:
        return run_refresh(args, rep)

    return run_setup(args, rep)


if __name__ == "__main__":
    sys.exit(main())
