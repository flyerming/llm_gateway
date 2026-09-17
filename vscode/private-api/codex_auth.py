r"""Stop Codex from demanding a ChatGPT login it can never complete.

THE SYMPTOM
-----------
With `model_provider` pointed at a private gateway and
`requires_openai_auth = false`, Codex still opens a login gate, and

    $ codex login status
    Not logged in

The expectation is that a private provider needs no account -- the gateway holds
the credentials. It does not work out that way: Codex decides whether it is
signed in by looking for `$CODEX_HOME/auth.json`, independently of which provider
a turn will use. No file, no login, whatever the provider table says.

WHY AN API KEY IS THE RIGHT FIX
-------------------------------
It was Codex writing this file for a *private* gateway that the user asked to
avoid -- not the file itself. Codex already has an API-key mode; string evidence
from `codex.exe`:

    key below. It will be stored locally in auth.json. Detected OPENAI_API_KEY
    environment v...

so the file simply records an API key instead of OAuth tokens, and Codex stops
asking. The value we write is the GATEWAY's key: it is what the private provider
table already authenticates with, so this introduces no new secret -- it moves
one that is already on this machine (and already in `config.toml` or the
environment) into the file Codex actually consults.

WHAT IT COSTS
-------------
`auth.json` is plaintext on disk, like the `env_key` config it duplicates. That
is the deliberate trade for not needing a ChatGPT account. `--restore` puts any
previous file back from `auth.json.bak`.
"""

from __future__ import annotations

import json
from pathlib import Path

from detect import codex_auth


def auth_path() -> Path:
    return codex_auth()


def status() -> tuple[str, str]:
    """(state, detail). Never returns or prints any part of the credential."""
    path = auth_path()
    if not path.exists():
        return "MISSING", f"no {path} -- Codex will show a login prompt"

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return "UNREADABLE", f"{path} is not valid JSON ({e})"
    if not isinstance(data, dict):
        return "UNREADABLE", f"{path} is not a JSON object"

    if data.get("OPENAI_API_KEY"):
        return "API_KEY", f"signed in with an API key ({path})"
    if data.get("tokens"):
        return "CHATGPT", f"signed in with a ChatGPT account ({path})"
    return "EMPTY", f"{path} has neither an API key nor tokens"


def write_api_key_auth(api_key: str) -> tuple[Path, str | None]:
    """Record `api_key` so Codex considers itself signed in.

    Returns (path, backup). The backup is None when there was no file to keep.
    Every field is written explicitly: Codex deserialises this into a struct, and
    omitting an optional field is untested territory for no benefit.
    """
    if not api_key.strip():
        raise ValueError("refusing to write an empty API key")

    path = auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    backup: str | None = None
    if path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        if not bak.exists():  # only ever snapshot the original
            bak.write_bytes(path.read_bytes())
        backup = str(bak)

    payload = {
        "OPENAI_API_KEY": api_key,
        "tokens": None,
        "last_refresh": None,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path, backup


def restore_from_backup() -> bool:
    """Put the previous auth.json back; True when there was one."""
    path = auth_path()
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
