"""Wire the shared SearXNG MCP server into Codex and Claude Code.

WHY SEARCH HAS TO BE CLIENT-SIDE
--------------------------------
Nothing on the gateway side can fix this. Codex's web search is a HOSTED tool:
the CLI puts `{"type":"web_search"}` into the Responses API `tools` array and
expects whatever is upstream to execute it. For the `custom_openai` on-prem
models the gateway silently drops that tool -- a live probe comes back HTTP 200
with `"tools":[]` -- and LiteLLM's own Search Tools (`/v1/search`, 20 providers)
are a separate endpoint family that Codex never calls. Claude Code's built-in
WebSearch is the same shape: a server-side tool, and this Claude Code talks to a
gateway serving non-Anthropic models.

So the search becomes a tool the model CALLS, served over MCP. All three
consumers (Linux codex CLI, the VSCode Codex plugin, VSCode Claude Code) are MCP
clients and neither knows nor cares what executes the tool.

ONE KEY, NO EXTRA SECRET
------------------------
The MCP server is reached THROUGH THE GATEWAY, at `<gateway>/searxng/mcp`, and
authenticates with the user's ordinary LiteLLM virtual key -- the same one this
toolkit already writes for models. There is no second key to distribute and
nothing extra for a user to paste.

The search backend's own bearer token still exists, but it is a SERVER-SIDE
credential now: it lives only in the gateway's registration record (written by
`searxng/register_mcp.py`) and is used for the gateway -> backend hop. Nobody on
a laptop ever sees it, exactly like `CLIPROXY_API_KEY` on the
litellm -> cli-proxy-api hop. That is also why the backend no longer publishes a
port: with no route to it, the gateway is the only way in.

ONE URL, TWO SPELLINGS
----------------------
Every client points at `<gateway>/searxng/mcp` with its LiteLLM key, but the two
config formats are NOT interchangeable, and each one fails quietly or
confusingly when you paste the other's shape:

  Codex        `[mcp_servers.<name>]` with `url = "..."`.  There is NO `type`
               field -- transport is inferred from `url` vs `command`. Auth goes
               in `http_headers`; `env`, `args`, `cwd` and `bearer_token` are
               explicitly REJECTED for streamable_http, and Codex says so with a
               "is not supported for streamable_http" error.

  Claude Code  `{"type": "http", "url": "..."}` under `mcpServers` in
               `~/.claude.json`. Here `type` is REQUIRED -- an entry with a
               `url` but no `type` is read as stdio and skipped.

TOOL NAMES ARRIVE PREFIXED
--------------------------
The gateway exposes upstream tools as `<server_name>-<tool>`, so the model calls
`searxng-web_url_read`, not `web_url_read`. This cannot be configured away:
`tool_name_to_display_name` was set to the full reverse mapping and `tools/list`
still returned the prefixed names (verified against LiteLLM v1.100.1), so that
field is display-only. Use `normalize_tool` / `missing_expected` rather than
comparing raw names.

Rename `DEFAULT_NAME` and both the URL and the prefixes follow; the one thing
that does not is the registration on the gateway, which pins the same name.

WHICH FILE CLAUDE CODE READS
----------------------------
`~/.claude.json` (user scope, machine-wide), NOT `~/.claude/settings.json`.
This was read off the shipped extension rather than guessed: the extension
builds its config paths from one helper that pairs
`globalConfig = <home>/.claude.json` with `userSettings = <home>/.claude/settings.json`,
and `mcpServers` is absent from `claude-code-settings.schema.json` -- so
settings.json is the wrong file and writing there is silently ignored.

WHY http_headers AND NOT bearer_token_env_var
---------------------------------------------
Codex offers `bearer_token_env_var`, which keeps the secret out of the file but
only works if the variable is present in the environment Codex inherits. One of
the three clients is the VSCode Codex plugin, launched by the editor, whose
environment we do not control -- there, the variable would simply be missing and
the requests would go out unauthenticated (401, for reasons the user cannot see
from the config). A literal `Authorization` header works identically in every
launch context, so it is the one we write.

This now costs nothing: the value written is the user's LiteLLM key, which is
already sitting in that same file for the model config. No new secret is
introduced, and there is no second file to keep in sync.

WHICH HALF EACH TOOLKIT WIRES
-----------------------------
This file is kept byte-identical in both toolkits, like `gateway.py`. The
`codex_*` half is wired by both (`codexcli/` drives the Linux Codex CLI,
`vscode/` the editor's Codex plugin); the `claude_*` half is wired only by
`vscode/`, because that is the only toolkit that configures Claude Code -- and
the only one that ships `jsonc.py`, which those functions import.

Deciding WHICH key to write is deliberately not done here: it differs per toolkit
(each resolves its own saved LiteLLM key from its own config shape) and doing it
here would need imports this file must stay free of to remain identical in both.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_NAME = "searxng"

# The MCP endpoint is served BY THE GATEWAY, so the URL is the gateway base plus
# one fixed path. `--api-base` has no hardcoded default anywhere in this toolkit,
# so callers pass what they already resolved (the plugin's saved base, or this
# fallback matching the address used throughout the docs).
DEFAULT_BASE = "http://10.18.219.156:4000"
MCP_PATH = f"/{DEFAULT_NAME}/mcp"
DEFAULT_URL = DEFAULT_BASE + MCP_PATH

# Kept as an explicit OVERRIDE for one release: users on an older drop may still
# have it exported, and the offline/self-hosted-server case still has a real
# shared token. It is no longer the normal path -- the token is the user's
# LiteLLM key now. See `resolve_token` in the toolkit for the precedence.
TOKEN_ENV = "MCP_SEARXNG_TOKEN"

# Codex budgets an MCP server's startup and then each tool call. A cold SearXNG
# search fans out to every enabled engine, and the server's `web_url_read` pulls
# a page through the proxy -- both need more headroom than the defaults.
STARTUP_TIMEOUT_SEC = 20
TOOL_TIMEOUT_SEC = 120

# What the operator is told to expect, in UPSTREAM spelling. Kept here so
# `--check-search` output and the docs cannot drift apart.
EXPECTED_TOOLS = ("searxng_web_search", "web_url_read")

# The gateway exposes upstream tools as `<server_name>-<tool>`, so the model
# calls `searxng-web_url_read`. This is NOT configurable away -- verified
# 2026-09-17: `tool_name_to_display_name` was set to the full reverse mapping and
# `tools/list` still returned the prefixed names, so the field is display-only.
GATEWAY_TOOL_PREFIX = f"{DEFAULT_NAME}-"


def mcp_url(base: str) -> str:
    """The MCP endpoint on a given gateway base.

    Strips a trailing `/v1` first, because the base this toolkit stores is the
    MODEL endpoint (`http://host:4000/v1`) while the MCP route hangs off the
    gateway root. Without the strip the URL picks up a bogus segment
    (`.../v1/searxng/mcp`) and 404s -- hit for real, hence doing it here rather
    than trusting every caller to normalise.
    """
    b = base.strip().rstrip("/")
    if b.endswith("/v1"):
        b = b[: -len("/v1")].rstrip("/")
    return b + MCP_PATH


def normalize_tool(name: str) -> str:
    """Map a tool name the gateway exposes back to its upstream spelling.

    Strips the leading `<server_name>-`, repeatedly, because a server whose name
    already prefixes its own tools gets doubled: the upstream `searxng_web_search`
    arrives as `searxng-searxng_web_search`. Only the gateway's hyphenated prefix
    is stripped -- `searxng_web_search` itself has an underscore and is left
    alone.
    """
    while name.startswith(GATEWAY_TOOL_PREFIX):
        name = name[len(GATEWAY_TOOL_PREFIX):]
    return name


def missing_expected(tools: list[tuple[str, str]]) -> list[str]:
    """Which EXPECTED_TOOLS are absent, comparing in upstream spelling.

    Comparing raw names here is the trap: it reports every tool missing on a
    perfectly healthy gateway, because what comes back is prefixed.
    """
    present = {normalize_tool(n) for n, _ in tools}
    return [t for t in EXPECTED_TOOLS if t not in present]


# --------------------------------------------------------------------------- #
# Config writing
# --------------------------------------------------------------------------- #

def codex_write(cfg: Path, url: str, token: str, name: str = DEFAULT_NAME) -> None:
    """Add (or update) the `[mcp_servers.<name>]` block in config.toml.

    Order matters: the parent table is written before the `http_headers`
    sub-table, because TOML forbids defining a table after one of its children.
    `TomlFile.set_table` appends missing tables at the end of the file, so
    calling them in this order produces a valid document.
    """
    from tomlpatch import TomlFile

    f = TomlFile(cfg)
    f.set_table(f"mcp_servers.{name}", {
        "url": url,
        "startup_timeout_sec": STARTUP_TIMEOUT_SEC,
        "tool_timeout_sec": TOOL_TIMEOUT_SEC,
    })
    f.set_table(f"mcp_servers.{name}.http_headers", {
        "Authorization": f"Bearer {token}",
    })
    f.save()


def codex_clear(cfg: Path, name: str = DEFAULT_NAME) -> list[str]:
    """Drop the block. Returns the tables actually removed."""
    from tomlpatch import TomlFile

    f = TomlFile(cfg)
    removed = [h for h in (f"mcp_servers.{name}.http_headers", f"mcp_servers.{name}")
               if f.remove_table(h)]
    if removed:
        f.save()
    return removed


def codex_configured(cfg: Path, name: str = DEFAULT_NAME) -> str | None:
    """The `url` Codex would use, unquoted, or None. For --status."""
    from tomlpatch import TomlFile

    if not cfg.exists():
        return None
    raw = TomlFile(cfg).get("url", table=f"mcp_servers.{name}")
    return raw.strip("'\"") if raw else None


def codex_saved_token(cfg: Path, name: str = DEFAULT_NAME) -> str | None:
    """The bearer token already written into config.toml, or None.

    Read back so `--check-search` can prove the chain with the token Codex will
    actually send, rather than whatever happens to be in this shell's
    environment -- the two disagreeing is exactly the failure being diagnosed.
    """
    from tomlpatch import TomlFile

    if not cfg.exists():
        return None
    raw = TomlFile(cfg).get("Authorization",
                            table=f"mcp_servers.{name}.http_headers")
    if not raw:
        return None
    value = raw.strip("'\"")
    prefix = "Bearer "
    return value[len(prefix):] if value.startswith(prefix) else value


def claude_global_config() -> Path:
    """`~/.claude.json`, where Claude Code keeps user-scope MCP servers.

    Deliberately not `~/.claude/settings.json` -- see the module docstring; a
    server written there is silently ignored. `home()` comes from `detect` so
    this agrees with every other path this toolkit resolves.
    """
    from detect import home

    return home() / ".claude.json"


def claude_write(path: Path, url: str, token: str, name: str = DEFAULT_NAME) -> None:
    """Add (or update) the entry under `mcpServers` in ~/.claude.json.

    Read-merge-write: `mcpServers` is shared with any server the user added
    themselves, and `JsoncFile.set` replaces the whole top-level key, so the
    existing members have to be carried across. The file keeps its comments.
    """
    from jsonc import JsoncFile

    f = JsoncFile(path)
    servers = f.get("mcpServers") or {}
    if not isinstance(servers, dict):
        raise ValueError(f"{path}: mcpServers 不是对象")
    servers[name] = {
        "type": "http",
        "url": url,
        "headers": {"Authorization": f"Bearer {token}"},
    }
    f.set("mcpServers", servers)
    f.save()


def claude_clear(path: Path, name: str = DEFAULT_NAME) -> bool:
    from jsonc import JsoncFile

    if not path.exists():
        return False
    f = JsoncFile(path)
    servers = f.get("mcpServers") or {}
    if not isinstance(servers, dict) or name not in servers:
        return False
    del servers[name]
    if servers:
        f.set("mcpServers", servers)
    else:
        # Do not leave an empty `mcpServers: {}` behind -- Claude Code treats a
        # present-but-empty map as configured and it shows up in /mcp listings.
        f.remove("mcpServers")
    f.save()
    return True


def claude_configured(path: Path, name: str = DEFAULT_NAME) -> str | None:
    from jsonc import JsoncFile

    if not path.exists():
        return None
    servers = JsoncFile(path).get("mcpServers") or {}
    if not isinstance(servers, dict):
        return None
    entry = servers.get(name)
    return entry.get("url") if isinstance(entry, dict) else None


def claude_saved_token(path: Path, name: str = DEFAULT_NAME) -> str | None:
    """The bearer token written into ~/.claude.json, or None. See
    `codex_saved_token` for why the check reads it back."""
    from jsonc import JsoncFile

    if not path.exists():
        return None
    servers = JsoncFile(path).get("mcpServers") or {}
    if not isinstance(servers, dict):
        return None
    entry = servers.get(name)
    if not isinstance(entry, dict):
        return None
    value = ((entry.get("headers") or {}).get("Authorization") or "")
    prefix = "Bearer "
    return value[len(prefix):] if value.startswith(prefix) else (value or None)


# --------------------------------------------------------------------------- #
# Live check
# --------------------------------------------------------------------------- #

@dataclass
class Probe:
    ok: bool
    detail: str = ""
    server: str = ""
    version: str = ""
    protocol: str = ""
    tools: list[tuple[str, str]] = field(default_factory=list)


def _rpc(url: str, payload: dict, headers: dict, timeout: float):
    """One JSON-RPC POST, unwrapping an SSE reply if that is what comes back.

    The MCP streamable-HTTP spec lets the server answer a request with either
    `application/json` or a `text/event-stream` carrying the same object, and the
    upstream server is free to pick per request. Both have to be understood or
    the diagnostic would report a working server as broken.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **headers,
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        ctype = r.headers.get("Content-Type", "")
        sid = r.headers.get("Mcp-Session-Id")
    if not raw.strip():
        return None, sid
    text = raw.decode("utf-8", "replace")
    if "text/event-stream" in ctype:
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip()), sid
        raise ValueError(f"SSE reply carried no data frame: {text[:200]!r}")
    return json.loads(text), sid


def _explain_http_error(e: urllib.error.HTTPError) -> str:
    """Turn the two failure codes that have non-obvious causes into advice."""
    body = ""
    try:
        body = e.read()[:300].decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        pass

    if e.code == 401:
        return ("401 未授权 —— 网关正常，但拒绝了该密钥。"
                "现在搜索和模型使用的是同一个密钥，常见原因是搜索步骤"
                "先于模型步骤执行（或使用了不同的 --api-key）。"
                "不存在单独的搜索令牌。"
                f"响应内容：{body}")
    if e.code == 403:
        return ("403 禁止访问 —— 这条链路里客户端只与网关通信，"
                "所以 403 是网关自身的策略（密钥范围 / 允许的模型），"
                "不是搜索后端的问题。如果搜索失败但网关能应答，"
                "常见原因是后端的 Host 允许列表，且表现为 TOOL 错误而非 403 —— "
                "请检查服务端的 `MCP_HTTP_ALLOWED_HOSTS`。响应内容：" + body)
    if e.code == 404:
        return ("404 未找到 —— 网关正常，但不认识这个 MCP 服务。"
                "新构建的服务需要先做一次注册：在网关主机上执行 "
                "`python searxng/register_mcp.py`。（注册前网关会返回 "
                f"`MCP server, toolset, or access group 'searxng' not found`。）"
                f"响应内容：{body}")
    return f"MCP 端点返回 HTTP {e.code}。响应内容：{body}"


def probe(url: str, token: str | None = None, timeout: float = 30.0) -> Probe:
    """Handshake with the MCP server and list its tools.

    This is the only check that proves the whole chain: DNS/route to the gateway
    box, the bearer token, the Host allowlist, the server's own health, and that
    the tool inventory the model will see is the expected one. A green /health
    proves none of that -- it is deliberately unauthenticated and never contacts
    SearXNG.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    try:
        init, sid = _rpc(url, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "private-api check", "version": "1"},
            },
        }, headers, timeout)
    except urllib.error.HTTPError as e:
        return Probe(False, _explain_http_error(e))
    except urllib.error.URLError as e:
        return Probe(False, f"无法访问 {url}（{e.reason}）。"
                            f"请确认主机已启动、端口已发布，且本机可以路由到该地址。")
    except Exception as e:  # noqa: BLE001
        return Probe(False, f"握手失败：{e!r}")

    if not init or "result" not in init:
        return Probe(False, f"initialize 返回了意外的响应：{json.dumps(init)[:300]}")

    res = init["result"]
    info = res.get("serverInfo") or {}
    p = Probe(True, server=info.get("name", "?"), version=info.get("version", "?"),
              protocol=res.get("protocolVersion", "?"))

    # Best-effort from here: a session id is optional and the server may be
    # stateless, so a failure in these two steps is reported on top of an
    # otherwise successful handshake rather than replacing it.
    if sid:
        headers["Mcp-Session-Id"] = sid
    try:
        _rpc(url, {"jsonrpc": "2.0", "method": "notifications/initialized"},
             headers, timeout)
        listed, _ = _rpc(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                               "params": {}}, headers, timeout)
        for t in ((listed or {}).get("result") or {}).get("tools") or []:
            desc = (t.get("description") or "").strip().split("\n")[0]
            p.tools.append((t.get("name", "?"), desc[:110]))
    except Exception as e:  # noqa: BLE001
        p.detail = f"握手成功，但获取工具列表失败：{e!r}"

    return p
