#!/usr/bin/env python3
"""mihomo 节点可视化控制台。

替代 `sh proxy-switch.sh -l` + 序号切换的手工流程：在网页上筛选节点、看延迟、
点击切换，并显示订阅刷新状态、支持手动触发刷新。

只用 Python 3 标准库，不需要 pip 安装任何东西，和 proxy-nodes.py 一样可以在
LANG=C 的服务器上直接跑。

运行：
    python3 proxy-console.py

环境变量：
    CONSOLE_LISTEN            监听地址，默认 127.0.0.1:8787
    CONSOLE_USER              登录用户名，默认 admin
    CONSOLE_PASSWORD          登录密码，必填
    MIHOMO_CONTROLLER_URL     mihomo 控制接口，默认 http://127.0.0.1:9090
    MIHOMO_SELECTOR           默认分组，默认 "🚀 节点选择"
    SUBSCRIPTION_PROVIDER     订阅 provider 名，默认 subscription
    SUBSCRIPTION_INTERVAL     订阅刷新周期（秒），默认 86400
    HEALTHCHECK_URL           测速目标，默认 http://cp.cloudflare.com/generate_204
    RULESET_DIR               例外清单目录（可写），默认 /ruleset
    MIHOMO_CONFIG_PATH        mihomo 配置在 proxy 容器里的路径，降级重载时用
    CLIPROXY_MANAGEMENT_URL   CLIProxyAPI 管理接口，默认 http://127.0.0.1:8317/v0/management
    CLIPROXY_MANAGEMENT_KEY   CLIProxyAPI 管理密钥（由 compose 注入）
    CLIPROXY_BASE_URL         CLIProxyAPI 数据面地址，默认 http://127.0.0.1:8317/v1
    CLIPROXY_API_KEY          数据面 API key，只有「连通性自检」用（由 compose 注入）

关于鉴权：Clash API 本身没有鉴权，任何人能连上 9090 就能改你的节点。这里把
9090 留在 127.0.0.1，只把控制台暴露出去，所以控制台自己的 Basic 鉴权就是唯一
的一道门。密码请用 .env 里的 CONSOLE_PASSWORD，不要用默认值。
"""

import base64
import hmac
import ipaddress
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONTROLLER = os.environ.get("MIHOMO_CONTROLLER_URL", "http://127.0.0.1:9090").rstrip("/")
LISTEN = os.environ.get("CONSOLE_LISTEN", "127.0.0.1:8787")
AUTH_USER = os.environ.get("CONSOLE_USER", "admin")
AUTH_PASSWORD = os.environ.get("CONSOLE_PASSWORD", "")
DEFAULT_GROUP = os.environ.get("MIHOMO_SELECTOR", "🚀 节点选择")
PROVIDER_NAME = os.environ.get("SUBSCRIPTION_PROVIDER", "subscription")
try:
    PROVIDER_INTERVAL = int(os.environ.get("SUBSCRIPTION_INTERVAL", "86400"))
except ValueError:
    PROVIDER_INTERVAL = 86400
HEALTHCHECK_URL = os.environ.get(
    "HEALTHCHECK_URL", "http://cp.cloudflare.com/generate_204"
)
CLIPROXY_MANAGEMENT_URL = os.environ.get(
    "CLIPROXY_MANAGEMENT_URL", "http://127.0.0.1:8317/v0/management"
).rstrip("/")
CLIPROXY_MANAGEMENT_KEY = os.environ.get("CLIPROXY_MANAGEMENT_KEY", "")
# 数据面（OpenAI 兼容接口）。控制台只用它做连通性自检 —— 这是唯一能证明
# "凭据真的能用"的办法：/v1/models 只说明服务活着和认识哪些模型名。
CLIPROXY_BASE_URL = os.environ.get(
    "CLIPROXY_BASE_URL", "http://127.0.0.1:8317/v1"
).rstrip("/")
CLIPROXY_API_KEY = os.environ.get("CLIPROXY_API_KEY", "")
# 例外清单目录：compose 把 ./network/mihomo/ruleset 以可写方式挂到这里。
# 只有这个目录可写，控制台脚本本身仍然是只读挂载。
RULESET_DIR = os.environ.get("RULESET_DIR", "/ruleset")
# 这两个 provider 名必须和 mihomo/config.yaml 里 rule-providers 的键一致。
DIRECT_PROVIDER = os.environ.get("DIRECT_RULE_PROVIDER", "CustomDirect")
PROXY_PROVIDER = os.environ.get("PROXY_RULE_PROVIDER", "CustomProxy")
# 降级重载整份配置时，mihomo 自己去读这个路径 —— 是 proxy 容器里的路径。
MIHOMO_CONFIG_PATH = os.environ.get("MIHOMO_CONFIG_PATH", "/app/mihomo/config.yaml")
try:
    CLIPROXY_OAUTH_SESSION_TTL = int(
        os.environ.get("CLIPROXY_OAUTH_SESSION_TTL", "900")
    )
except ValueError:
    CLIPROXY_OAUTH_SESSION_TTL = 900

# 控制台永远直连 127.0.0.1 上的 mihomo，绝不能走 HTTP_PROXY —— 否则如果环境变量
# 泄漏进来，控制台会试图通过代理去访问本机控制接口，直接死锁。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_oauth_sessions = {}
_oauth_sessions_lock = threading.Lock()


class MihomoError(Exception):
    """mihomo 控制接口返回了错误，或者根本连不上。"""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class CLIProxyError(Exception):
    """CLIProxyAPI 管理接口返回错误，或控制台无法连接到它。"""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class RuleError(Exception):
    """例外清单的输入非法，或者清单文件读写失败。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def http_error_detail(exc, limit=500):
    """尽量从 HTTPError 的响应体里取出可读的错误说明。

    mihomo 的 503 会把真正的失败原因放在 `{"message": "..."}` 里，例如
    `open /app/mihomo/ruleset/CustomDirect.list: no such file or directory`。
    只留状态码等于把最有用的信息丢掉，页面上就只剩一个看不懂的 503。
    """
    try:
        raw = exc.read()
    except Exception:
        return ""
    if not raw:
        return ""
    text = raw.decode("utf-8", "replace").strip()
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        for key in ("message", "error", "msg", "detail", "reason"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
    return " ".join(text.split())[:limit]


def cliproxy(method, path, body=None, timeout=15, auth=True):
    """调用 CLIProxyAPI management API；管理密钥永远不返回给浏览器。"""
    if auth and not CLIPROXY_MANAGEMENT_KEY:
        raise CLIProxyError("未配置 CLIPROXY_MANAGEMENT_KEY")
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        CLIPROXY_MANAGEMENT_URL + path, data=data, method=method
    )
    request.add_header("Content-Type", "application/json")
    if auth:
        request.add_header("Authorization", "Bearer " + CLIPROXY_MANAGEMENT_KEY)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise CLIProxyError(
            "CLIProxyAPI returned HTTP %d for %s" % (exc.code, path),
            status=exc.code,
        )
    except urllib.error.URLError as exc:
        raise CLIProxyError(
            "cannot reach CLIProxyAPI at %s: %s" % (CLIPROXY_MANAGEMENT_URL, exc.reason)
        )
    except OSError as exc:
        raise CLIProxyError(
            "cannot reach CLIProxyAPI at %s: %s" % (CLIPROXY_MANAGEMENT_URL, exc)
        )

    if not raw.strip():
        return {}
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise CLIProxyError("CLIProxyAPI returned invalid JSON for %s" % path)


def _remember_oauth(provider, state):
    with _oauth_sessions_lock:
        _oauth_sessions[state] = {"provider": provider, "created": time.time()}


def _oauth_session(provider, state):
    if not state:
        raise CLIProxyError("OAuth 响应缺少 state")
    with _oauth_sessions_lock:
        item = _oauth_sessions.get(state)
        if item and time.time() - item["created"] > CLIPROXY_OAUTH_SESSION_TTL:
            _oauth_sessions.pop(state, None)
            item = None
    if not item or item["provider"] != provider:
        raise CLIProxyError("OAuth state 无效或已过期", status=400)
    return item


def cliproxy_start_login(provider):
    """生成 Codex/其他 provider 的 OAuth URL，并在服务端记录 state。"""
    if provider not in {"codex", "anthropic", "antigravity"}:
        raise CLIProxyError("不支持的 OAuth provider: %s" % provider, status=400)
    query = urllib.parse.urlencode({"is_webui": "true"})
    payload = cliproxy("GET", "/%s-auth-url?%s" % (provider, query), timeout=30)
    state = payload.get("state")
    url = payload.get("url")
    if not state or not url:
        raise CLIProxyError("CLIProxyAPI 未返回完整 OAuth URL/state")
    _remember_oauth(provider, state)
    return {"provider": provider, "state": state, "url": url}


def cliproxy_oauth_status(provider, state):
    _oauth_session(provider, state)
    query = urllib.parse.urlencode({"state": state})
    payload = cliproxy("GET", "/get-auth-status?%s" % query, timeout=15)
    status = payload.get("status")
    if status in {"ok", "error"}:
        with _oauth_sessions_lock:
            _oauth_sessions.pop(state, None)
    return payload


def _account_summary(item):
    """从 /auth-files 的单个凭据对象抽取用于展示的非敏感字段。

    绝不包含 access_token / refresh_token；只取健康状态、订阅、配额、
    已观测到的模型名这些用于判断"真实登录状态"的信息。
    """
    safe = {
        key: item.get(key)
        for key in ("name", "email", "type", "provider", "status")
        if item.get(key) is not None
    }
    for key in ("disabled", "unavailable"):
        if isinstance(item.get(key), bool):
            safe[key] = item[key]
    # 管理面记录的异常，不是本次自检响应；不能仅凭它判断当前请求失败。
    message = item.get("status_message")
    if isinstance(message, str) and message:
        safe["status_message"] = message[:300]
    # 订阅信息来自 id_token。
    id_token = item.get("id_token")
    if isinstance(id_token, dict):
        if id_token.get("plan_type"):
            safe["plan"] = id_token["plan_type"]
        until = id_token.get("chatgpt_subscription_active_until")
        if until:
            safe["subscription_until"] = until
    # model_quotas 的键是账号实际观测到的模型名（可能非完整可用目录）。
    quotas = item.get("model_quotas")
    if isinstance(quotas, dict) and quotas:
        safe["models"] = sorted(quotas.keys())
    # 主配额已用百分比。
    quota = item.get("quota")
    if isinstance(quota, dict) and isinstance(quota.get("signals"), dict):
        used = quota["signals"].get("X-Codex-Primary-Used-Percent")
        if used is not None:
            try:
                used_percent = float(used)
                if not isinstance(used, bool) and math.isfinite(used_percent) and 0 <= used_percent <= 100:
                    safe["used_percent"] = used_percent
                    safe["remaining_percent"] = round(100 - used_percent, 2)
            except (TypeError, ValueError):
                pass
    return safe


def cliproxy_accounts():
    payload = cliproxy("GET", "/auth-files")
    files = payload.get("files", payload)
    # 账号列表不含 token；仍只抽取用于展示的非敏感字段，避免把 JSON 原文泄漏到浏览器。
    safe = []
    if isinstance(files, list):
        for item in files:
            if isinstance(item, dict):
                safe.append(_account_summary(item))
    return {"files": safe}


def cliproxy_submit_callback(body):
    provider = str(body.get("provider") or "codex").strip().lower()
    raw_url = str(
        body.get("url") or body.get("redirect_url") or body.get("callback_url") or ""
    ).strip()
    parsed = urllib.parse.urlparse(raw_url)
    query = urllib.parse.parse_qs(parsed.query)
    state = str(body.get("state") or (query.get("state") or [""])[0]).strip()
    code = str(body.get("code") or (query.get("code") or [""])[0]).strip()
    error = str(body.get("error") or (query.get("error") or [""])[0]).strip()
    if not raw_url and not (state and (code or error)):
        raise CLIProxyError("请粘贴 OAuth 回调完整 URL，或同时提供 state/code", status=400)
    _oauth_session(provider, state)
    callback = {"provider": provider, "state": state}
    if raw_url:
        callback["redirect_url"] = raw_url
    if code:
        callback["code"] = code
    if error:
        callback["error"] = error
    result = cliproxy("POST", "/oauth-callback", callback, timeout=30, auth=False)
    return {"ok": True, "provider": provider, "state": state, "result": result}


# --------------------------------------------------------------------------
# CLIProxyAPI 连通性自检
#
# 网页显示"登录成功"只说明 OAuth 回调走完了，不说明凭据能用。分两档查：
#   一档 GET /v1/models           —— 服务活着、认识哪些模型名、key 对不对
#   二档 实际发一次最小生成        —— OAuth 凭据有效、proxy 出网、上游都通
# 一档通过二档失败是常见情况（凭据过期、节点挂了、账号没额度）。
#
# 每一步都把上游返回的原文带回来：排障时那句原始报错比任何猜测都值钱。
# --------------------------------------------------------------------------

# 原文可能很长（比如模型多），截断后再给浏览器。
_RAW_LIMIT = 1500


def _raw_request(url, method="GET", body=None, headers=None, timeout=20):
    """发请求并原样返回 (状态码, 响应文本)。绝不抛异常 —— 自检要的就是原文。

    连不上时状态码返回 None，文本里写清原因，调用方照样能展示。
    """
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        return None, "无法连接 %s：%s" % (url, exc.reason)
    except OSError as exc:
        return None, "无法连接 %s：%s" % (url, exc)


def _brief(payload):
    """把上游返回的 JSON 压成一句人能读的话；解析不了就原样返回。"""
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)
    if isinstance(error, str):
        return error
    if isinstance(data.get("message"), str):
        return data["message"]
    return None


def _models_from(payload):
    try:
        data = json.loads(payload)
    except ValueError:
        return []
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    models = []
    for item in items:
        if isinstance(item, dict) and item.get("id"):
            models.append(str(item["id"]))
        elif isinstance(item, str):
            models.append(item)
    return models


def _pick_model(models, wanted):
    """挑一个用来实际生成的模型：调用方指定的优先，否则挑带 codex 的。"""
    if wanted:
        return wanted
    for name in models:
        if "codex" in name.lower():
            return name
    return models[0] if models else ""


def cliproxy_selftest(probe=False, model=""):
    """按顺序自检整条链路，返回每一步的状态和原文。"""
    steps = []
    models = []

    if not CLIPROXY_API_KEY:
        steps.append(
            {
                "name": "数据面 API key",
                "ok": False,
                "detail": "控制台没拿到 CLIPROXY_API_KEY，无法调用 /v1/models。"
                "检查 compose 里 proxy-console 的环境变量。",
            }
        )
        return {"ok": False, "steps": steps, "models": models}

    auth = {"Authorization": "Bearer " + CLIPROXY_API_KEY}

    # 一档之一：模型列表。200 只说明服务活着、key 对、认识这些模型名。
    status, text = _raw_request(CLIPROXY_BASE_URL + "/models", headers=auth)
    models = _models_from(text)
    if status == 200 and models:
        detail = "发现 %d 个模型：%s" % (len(models), "、".join(models))
    elif status == 200:
        detail = "接口通了，但返回的模型列表是空的 —— CLIProxyAPI 还没认出可用模型"
    elif status == 401:
        detail = "401：CLIPROXY_API_KEY 和 CLIProxyAPI 的 api-keys 不一致"
    else:
        detail = _brief(text) or (("HTTP %s" % status) if status else text)
    steps.append(
        {
            "name": "GET %s/models" % CLIPROXY_BASE_URL,
            "ok": status == 200,
            "status": status,
            "detail": detail,
            "raw": text[:_RAW_LIMIT],
        }
    )

    # 一档之二：账号凭据文件。有文件才谈得上二档。
    accounts = []
    try:
        files = cliproxy_accounts().get("files") or []
        accounts = [
            str(item.get("email") or item.get("name") or item.get("provider") or "?")
            for item in files
        ]
        steps.append(
            {
                "name": "管理面 /auth-files",
                "ok": bool(files),
                "detail": (
                    "已保存账号 %d 个：%s" % (len(files), "、".join(accounts))
                    if files
                    else "没有已保存的账号 —— 先完成一次 Codex 登录"
                ),
            }
        )
    except CLIProxyError as exc:
        steps.append({"name": "管理面 /auth-files", "ok": False, "detail": str(exc)})

    # 二档：实际生成。这才是"真的能用"的证据。
    if not probe:
        steps.append(
            {
                "name": "实际生成一次",
                "ok": False,
                "skipped": True,
                "detail": "未执行。点「实际生成一次」才会真发一个请求 —— "
                "这一步才能证明 OAuth 凭据有效、proxy 和上游都通。",
            }
        )
        return {
            "ok": all(step.get("ok") for step in steps if not step.get("skipped")),
            "steps": steps,
            "models": models,
        }

    chosen = _pick_model(models, model)
    if not chosen:
        steps.append(
            {"name": "实际生成一次", "ok": False, "detail": "没有可用模型，跳过"}
        )
        return {"ok": False, "steps": steps, "models": models}

    status, text = _raw_request(
        CLIPROXY_BASE_URL + "/chat/completions",
        method="POST",
        headers=auth,
        body={
            "model": chosen,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
            "stream": False,
        },
        timeout=90,
    )

    answer = ""
    if status == 200:
        try:
            choices = json.loads(text).get("choices") or []
            answer = (choices[0].get("message") or {}).get("content") or ""
        except (ValueError, AttributeError, IndexError, TypeError):
            answer = ""
    if status == 200 and answer:
        detail = "生成成功，模型回复：%s" % answer.strip().replace("\n", " ")[:200]
    elif status == 200:
        detail = "HTTP 200 但没解析出回复内容，看下面的原文"
    else:
        detail = _brief(text) or (("HTTP %s" % status) if status else text)
    steps.append(
        {
            "name": "POST %s/chat/completions（model=%s）" % (CLIPROXY_BASE_URL, chosen),
            "ok": status == 200,
            "status": status,
            "detail": detail,
            "raw": text[:_RAW_LIMIT],
            "model": chosen,
        }
    )

    return {
        "ok": all(step.get("ok") for step in steps if not step.get("skipped")),
        "steps": steps,
        "models": models,
        "accounts": accounts,
    }


# --------------------------------------------------------------------------
# mihomo 控制接口
# --------------------------------------------------------------------------

def mihomo(method, path, body=None, timeout=10):
    """调用 mihomo 控制接口，返回解析后的 JSON。"""
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(CONTROLLER + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        # 404 通常是接口不存在（mihomo 版本差异），交给调用方降级处理。
        # 其余状态码把响应体里的 message 带出来：503 的真实原因（例如规则
        # 文件不存在）只写在 body 里，丢掉它页面上就只剩一个看不懂的 503。
        detail = http_error_detail(exc)
        message = "mihomo 返回 HTTP %d（%s）" % (exc.code, path)
        if detail:
            message += "：%s" % detail
        raise MihomoError(message, status=exc.code)
    except urllib.error.URLError as exc:
        raise MihomoError("cannot reach mihomo at %s: %s" % (CONTROLLER, exc.reason))
    except OSError as exc:
        raise MihomoError("cannot reach mihomo at %s: %s" % (CONTROLLER, exc))

    if not raw.strip():
        return {}
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise MihomoError("mihomo returned invalid JSON for %s" % path)


def get_proxies():
    payload = mihomo("GET", "/proxies")
    proxies = payload.get("proxies")
    if not isinstance(proxies, dict):
        raise MihomoError("mihomo /proxies response has no 'proxies' map")
    return proxies


def is_group(entry):
    """分组和普通节点的区别：分组带 `all` 列表。同 proxy-nodes.py。"""
    return isinstance(entry, dict) and isinstance(entry.get("all"), list)


def node_delay(entry):
    """取最近一次健康检查的延迟，单位毫秒。0 或缺失都视为未知。"""
    if not isinstance(entry, dict):
        return None
    history = entry.get("history") or []
    if not history:
        return None
    delay = history[-1].get("delay")
    if not delay:
        return None
    return delay


def parse_iso(value):
    """解析 mihomo 的 ISO8601 时间戳，容忍它几种不同的输出格式。"""
    if not value:
        return None
    text = value.strip()
    # mihomo 有时给出 "2026-09-14T10:44:00.000000+08:00"，有时是 Z 结尾。
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # 退回到手写解析，去掉可能存在的纳秒部分。
        try:
            head, _, tail = text.partition(".")
            if tail:
                offset = ""
                for sign in ("+", "-"):
                    if sign in tail:
                        offset = sign + tail.split(sign, 1)[1]
                        break
                text = head + offset if offset else head
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def provider_info():
    """订阅状态。mihomo 版本不同接口可能有差异，所以整体做成可降级的。"""
    try:
        payload = mihomo("GET", "/providers/proxies")
    except MihomoError as exc:
        if exc.status == 404:
            return {"supported": False, "reason": "接口不存在（mihomo 版本较旧）"}
        return {"supported": False, "reason": str(exc)}

    providers = payload.get("providers")
    if not isinstance(providers, dict):
        return {"supported": False, "reason": "响应里没有 providers 字段"}

    entry = providers.get(PROVIDER_NAME)
    if not isinstance(entry, dict):
        return {
            "supported": False,
            "reason": "找不到名为 %s 的 provider（现有：%s）"
            % (PROVIDER_NAME, ", ".join(sorted(providers)) or "无"),
        }

    updated = parse_iso(entry.get("updatedAt"))
    result = {
        "supported": True,
        "name": PROVIDER_NAME,
        "updated_at": updated.isoformat() if updated else None,
        "updated_at_raw": entry.get("updatedAt"),
        "interval": PROVIDER_INTERVAL,
        "next_refresh": (
            (updated + timedelta(seconds=PROVIDER_INTERVAL)).isoformat()
            if updated
            else None
        ),
        "node_count": len(entry.get("proxies") or []),
    }

    # 流量/到期信息：机场通常塞在 subscriptionInfo 里，不是所有订阅都有。
    info = entry.get("subscriptionInfo")
    if isinstance(info, dict) and info:
        result["subscription"] = {
            "upload": info.get("Upload"),
            "download": info.get("Download"),
            "total": info.get("Total"),
            "expire": info.get("Expire"),
        }
    return result


# --------------------------------------------------------------------------
# 代理例外清单
#
# 「哪些网址不走代理」最终只能落在 mihomo 规则上：CLIProxyAPI 的 proxy-url 是
# 全局的，没有按域名绕过的开关，所以内网域名、必须直连的域名都在这里放行。
# 两个清单是 mihomo config.yaml 里 type: file 的 rule-provider，控制台只负责
# 维护对应的 .list 文件，再让 mihomo 重新读取。
# --------------------------------------------------------------------------

# 只允许写入这几种匹配方式。网页可以往 mihomo 的规则里写东西，所以输入必须
# 白名单化 —— 否则一个手滑的逗号就能让整个 rule-provider 解析失败。
RULE_KINDS = (
    ("DOMAIN", "完整域名"),
    ("DOMAIN-SUFFIX", "域名后缀（含子域名）"),
    ("DOMAIN-KEYWORD", "域名关键字"),
    ("IP-CIDR", "IPv4 网段"),
    ("IP-CIDR6", "IPv6 网段"),
    ("GEOIP", "国家/地区代码"),
)
RULE_KIND_NAMES = dict(RULE_KINDS)

# 规则行里的修饰开关，解析时摘出来，写入时由 format_rule_line 按类型补。
_RULE_FLAGS = ("no-resolve", "src")

RULESETS = {
    "direct": {
        "provider": DIRECT_PROVIDER,
        "file": "CustomDirect.list",
        "label": "直连例外",
    },
    "proxy": {
        "provider": PROXY_PROVIDER,
        "file": "CustomProxy.list",
        "label": "强制走代理",
    },
}

# 容器自身的 NO_PROXY 至少要保留这些名字，否则容器之间互访会绕一圈进代理。
NO_PROXY_BASE = ("localhost", "127.0.0.1", "db", "proxy", "cli-proxy-api")

_DOMAIN_RE = re.compile(r"^[a-z0-9_.\-]+$")
_GEOIP_RE = re.compile(r"^[A-Z]{2}$")

_ruleset_lock = threading.Lock()
_ruleset_writable = None


def ruleset_writable():
    """例外面板到底能不能写。

    只问 os.access 不够：容器里以 root 运行时，它对只读挂载也会回答"能写"，
    要真写一次才知道。结果缓存下来，写失败时由 write_ruleset 置回 False。
    """
    global _ruleset_writable
    if _ruleset_writable is None:
        _ruleset_writable = os.path.isdir(RULESET_DIR) and os.access(
            RULESET_DIR, os.W_OK
        )
        if _ruleset_writable:
            probe = os.path.join(RULESET_DIR, ".write-probe")
            try:
                with open(probe, "w", encoding="utf-8") as handle:
                    handle.write("")
                os.unlink(probe)
            except OSError:
                _ruleset_writable = False
    return _ruleset_writable


def _host_from_input(text):
    """容忍粘贴整条 URL、带端口或 `*.` 前缀的写法，只取出域名部分。"""
    host = text.strip().lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0].split(":", 1)[0]
    if host.startswith("*."):
        host = host[2:]
    return host.lstrip(".").rstrip(".")


def normalize_value(kind, value):
    """校验并规范化网页提交的值，返回应该写进清单文件的形式。

    规范化很重要：mihomo 的 rule-provider 只要有一行解析不了，整个规则集就会
    加载失败，例外清单会静默失效。这里宁可拒绝，也不写进可疑内容。
    """
    text = str(value if value is not None else "").strip()
    if not text:
        raise RuleError("请填写域名或 IP 段")
    if len(text) > 255:
        raise RuleError("输入太长（超过 255 个字符）")
    if any(ch in text for ch in " \t\r\n,\"'"):
        raise RuleError("不能包含空格、逗号、引号或换行 —— 一行只能写一条规则")

    if kind in ("DOMAIN", "DOMAIN-SUFFIX", "DOMAIN-KEYWORD"):
        host = _host_from_input(text)
        # DOMAIN-KEYWORD 是子串匹配，值本来就常常不带点（比如 openai），
        # 不能按域名那样要求有点。
        need_dot = kind in ("DOMAIN", "DOMAIN-SUFFIX")
        if not host or not _DOMAIN_RE.match(host) or (need_dot and "." not in host):
            raise RuleError(
                "%s 只能包含字母、数字、点、连字符和下划线%s，例如 %s"
                % (kind, "，且至少有一段点" if need_dot else "",
                   "openai" if kind == "DOMAIN-KEYWORD" else "example.com")
            )
        return host

    if kind in ("IP-CIDR", "IP-CIDR6"):
        want = 4 if kind == "IP-CIDR" else 6
        try:
            network = ipaddress.ip_network(text, strict=False)
        except ValueError:
            raise RuleError("不是合法的网段，例如 10.0.0.0/8 或 2001:db8::/32")
        if network.version != want:
            raise RuleError("%s 需要 IPv%d 网段" % (kind, want))
        return str(network)

    if kind == "GEOIP":
        code = text.upper()
        if not _GEOIP_RE.match(code):
            raise RuleError("GEOIP 请填两位国家代码，例如 CN")
        return code

    raise RuleError("不支持的匹配方式：%s" % kind)


def format_rule_line(kind, value):
    """拼出一行 mihomo 规则。IP 段必须带 no-resolve，否则每个连接都会反查 DNS。"""
    line = "%s,%s" % (kind, value)
    if kind in ("IP-CIDR", "IP-CIDR6"):
        line += ",no-resolve"
    return line


def parse_rule_line(line):
    """解析清单文件里的一行。注释和空行返回 None。

    返回的 managed 表示这是控制台认识的类型；手工写进去的其它规则（比如
    GEOSITE,cn）会照样列出来、也能删掉，只是不能通过网页新增。
    """
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    kind, _, rest = text.partition(",")
    kind = kind.strip().upper()
    parts = [part.strip() for part in rest.split(",") if part.strip()]
    value = ",".join(
        part for part in parts if part.lower() not in _RULE_FLAGS
    )
    return {
        "kind": kind,
        "value": value,
        "line": text,
        "managed": kind in RULE_KIND_NAMES,
    }


def _ruleset_file(which):
    meta = RULESETS.get(which)
    if not meta:
        raise RuleError("未知的例外清单：%s" % which, status=404)
    return os.path.join(RULESET_DIR, meta["file"])


def read_ruleset(which):
    """读取一个清单，返回规则列表（不含注释行）。"""
    path = _ruleset_file(which)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise RuleError("读取 %s 失败：%s" % (path, exc), status=500)

    rules = []
    for line in lines:
        parsed = parse_rule_line(line)
        if parsed:
            rules.append(parsed)
    return rules


def write_ruleset(which, rules):
    """原子地写回清单，保留文件开头的注释说明，只替换规则行。

    规则行之后的注释会在重写时被丢掉，所以说明统一写在文件开头 —— 两个 .list
    的默认内容就是这么排的。
    """
    global _ruleset_writable
    path = _ruleset_file(which)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            existing = handle.read().splitlines()
    except (FileNotFoundError, OSError):
        existing = []

    header = []
    for line in existing:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            break
        header.append(line)

    body = [rule["line"] for rule in rules]
    text = "\n".join(header + body).rstrip("\n") + "\n"

    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        # 先写临时文件再 replace：mihomo 随时可能来读这个文件，不能让它读到半截。
        os.replace(tmp, path)
    except OSError as exc:
        # 目录其实不可写（比如 compose 里挂成了 :ro），让面板别再显示可编辑。
        _ruleset_writable = False
        raise RuleError("写入 %s 失败：%s" % (path, exc), status=500)


def add_rule(which, kind, value):
    kind = str(kind or "").strip().upper()
    if kind not in RULE_KIND_NAMES:
        raise RuleError("不支持的匹配方式：%s" % kind)
    normalized = normalize_value(kind, value)
    line = format_rule_line(kind, normalized)
    with _ruleset_lock:
        rules = read_ruleset(which)
        if any(rule["line"].lower() == line.lower() for rule in rules):
            raise RuleError("这条规则已经在列表里了：%s" % line)
        rules.append(
            {"kind": kind, "value": normalized, "line": line, "managed": True}
        )
        write_ruleset(which, rules)


def remove_rule(which, line):
    line = str(line or "").strip()
    if not line:
        raise RuleError("缺少要删除的规则")
    with _ruleset_lock:
        rules = read_ruleset(which)
        remaining = [rule for rule in rules if rule["line"] != line]
        if len(remaining) == len(rules):
            raise RuleError("列表里没有这条规则：%s" % line, status=404)
        write_ruleset(which, remaining)


def apply_ruleset():
    """让 mihomo 重新读取例外清单。

    优先只刷新 rule-provider：不重建连接，正在跑的请求不受影响。当前 mihomo
    版本没有这个接口（404）时，降级为整份配置重载 —— 那条路会重置节点选择，
    所以必须在页面上说清楚。

    503 也走降级重载：mihomo 刷新单条 provider 时如果读不到文件（例如
    `open /app/mihomo/ruleset/CustomDirect.list: no such file or directory`），
    返回的就是 503。情况可能是容器看到的目录不一致，也可能是文件刚被删掉；
    整份重载会强制执行一次完整校验，失败时把真实原因一起报给页面。
    """
    result = {"providers": {}, "reloaded_config": False, "errors": [], "notes": []}
    fallback_needed = False
    fallback_reasons = []

    for which, meta in RULESETS.items():
        path = "/providers/rules/" + urllib.parse.quote(meta["provider"], safe="")
        try:
            mihomo("PUT", path, timeout=20)
            result["providers"][which] = "reloaded"
        except MihomoError as exc:
            if exc.status in (404, 503):
                fallback_needed = True
                result["providers"][which] = "fallback"
                if exc.status == 503:
                    fallback_reasons.append(str(exc))
                    result["notes"].append(
                        "mihomo 单独刷新「%s」失败，已改用整份配置重载：%s"
                        % (meta["label"], exc)
                    )
            else:
                result["providers"][which] = "error"
                result["errors"].append("%s：%s" % (meta["label"], exc))

    if fallback_needed:
        try:
            mihomo(
                "PUT",
                "/configs?force=true",
                {"path": MIHOMO_CONFIG_PATH},
                timeout=30,
            )
            result["reloaded_config"] = True
        except MihomoError as exc:
            detail = "；".join([str(exc)] + fallback_reasons)
            result["errors"].append(
                "整份配置重载失败：%s。常见原因是 proxy 容器里的 "
                "/app/mihomo/ruleset 没有这两个 .list 文件，请检查 proxy 与 "
                "proxy-console 是否来自同一份 compose，并执行 "
                "`docker compose up -d --force-recreate proxy proxy-console`。"
                % detail
            )

    result["errors"].extend(verify_ruleset_counts())
    return result


def verify_ruleset_counts():
    """核对 mihomo 实际读到的规则条数是否和本地清单一致。

    这是唯一能发现「两个容器看到的 ruleset 不是同一个目录」的办法：控制台
    写文件一定成功（它写自己的挂载），但 mihomo 可能读的是另一个路径。条数
    对不上就说明两边不是同一份文件，页面必须明确告诉用户，而不是假报成功。
    """
    payload = ruleset_providers()
    if not payload.get("supported"):
        return []

    errors = []
    for which, meta in RULESETS.items():
        try:
            expected = len(read_ruleset(which))
        except RuleError:
            continue
        info = payload["providers"].get(which) or {}
        if not info.get("present"):
            errors.append(
                "「%s」在 mihomo 里没有加载（provider %s 不存在）。"
                "检查 network/mihomo/config.yaml 的 rule-providers，并重建 proxy。"
                % (meta["label"], meta["provider"])
            )
            continue
        actual = info.get("rule_count")
        if isinstance(actual, int) and actual != expected:
            errors.append(
                "「%s」本地清单 %d 条，mihomo 实际读到 %d 条：两个容器看到的 "
                "ruleset 目录不是同一份。请在部署机上执行 "
                "`docker compose up -d --force-recreate proxy proxy-console`"
                "（restart 不会重新挂载目录）。"
                % (meta["label"], expected, actual)
            )
    return errors


def ruleset_providers():
    """读 mihomo 的 rule-provider 状态，用来确认清单真的生效了。可降级。"""
    try:
        payload = mihomo("GET", "/providers/rules")
    except MihomoError as exc:
        return {"supported": False, "reason": str(exc)}

    providers = payload.get("providers")
    if not isinstance(providers, dict):
        return {"supported": False, "reason": "响应里没有 providers 字段"}

    detail = {}
    for which, meta in RULESETS.items():
        entry = providers.get(meta["provider"])
        if not isinstance(entry, dict):
            detail[which] = {"present": False}
            continue
        detail[which] = {
            "present": True,
            "rule_count": entry.get("ruleCount"),
        }
    return {"supported": True, "providers": detail}


def no_proxy_snippet(direct_rules):
    """把直连清单里的域名拼成 NO_PROXY 值，供粘进 docker-compose.yml。

    只有靠 extra_hosts / /etc/hosts 才能解析的域名才真正需要这一层 —— 那种域名
    交给 mihomo 反而解析不出来。其余域名写进去只是省掉一跳，无害。IP 段写进
    NO_PROXY 没有意义，所以不拼。
    """
    hosts = []
    for rule in direct_rules:
        if rule["kind"] not in ("DOMAIN", "DOMAIN-SUFFIX"):
            continue
        value = rule["value"].lstrip("*.")
        if value not in hosts:
            hosts.append(value)
    ordered = [name for name in NO_PROXY_BASE]
    ordered += [name for name in hosts if name not in ordered]
    return ",".join(ordered)


def rules_state(apply_result=None):
    """例外面板需要的全部状态：两个清单、可写性、生效条数、NO_PROXY 片段。"""
    lists = {}
    for which, meta in RULESETS.items():
        entry = {"label": meta["label"], "provider": meta["provider"], "rules": []}
        try:
            entry["rules"] = read_ruleset(which)
        except RuleError as exc:
            entry["error"] = str(exc)
        lists[which] = entry

    writable = ruleset_writable()
    note = None
    if not writable:
        note = (
            "%s 不可写，网页只能查看不能修改。检查 compose 里 proxy-console 是否"
            "把 ./network/mihomo/ruleset 挂到了 %s。" % (RULESET_DIR, RULESET_DIR)
        )

    return {
        "lists": lists,
        "kinds": [{"kind": kind, "label": label} for kind, label in RULE_KINDS],
        "providers": ruleset_providers(),
        "writable": writable,
        "note": note,
        "ruleset_dir": RULESET_DIR,
        "no_proxy": no_proxy_snippet(lists["direct"]["rules"]),
        "apply": apply_result,
    }


# --------------------------------------------------------------------------
# 状态组装
# --------------------------------------------------------------------------

def build_state(group_name):
    """把 /proxies 的原始响应整理成前端要的形状。"""
    proxies = get_proxies()

    groups = [name for name, entry in proxies.items() if is_group(entry)]
    if not groups:
        raise MihomoError("mihomo 没有返回任何代理分组")

    if group_name not in proxies or not is_group(proxies.get(group_name)):
        group_name = DEFAULT_GROUP if DEFAULT_GROUP in groups else groups[0]

    group = proxies[group_name]
    current = group.get("now") or ""

    nodes = []
    for index, member in enumerate(group["all"], 1):
        entry = proxies.get(member)
        nodes.append(
            {
                "index": index,
                "name": member,
                "type": entry.get("type", "?") if isinstance(entry, dict) else "?",
                "delay": node_delay(entry),
                "current": member == current,
                "is_group": is_group(entry),
            }
        )

    return {
        "group": group_name,
        "groups": groups,
        "current": current,
        "nodes": nodes,
        "provider": provider_info(),
        "healthcheck_url": HEALTHCHECK_URL,
    }


# --------------------------------------------------------------------------
# 前端页面
# --------------------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mihomo + CLIProxyAPI 节点控制台</title>
<style>
  :root {
    --bg: #f6f7f9; --panel: #ffffff; --border: #e3e6ea; --text: #1c1e21;
    --muted: #6b7280; --accent: #2563eb; --good: #15803d; --warn: #b45309;
    --bad: #b91c1c; --hover: #f0f4ff;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #16181d; --panel: #1e2128; --border: #2f333c; --text: #e6e8ec;
      --muted: #9aa1ad; --accent: #60a5fa; --good: #4ade80; --warn: #fbbf24;
      --bad: #f87171; --hover: #252a36;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px; background: var(--bg); color: var(--text);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC",
          "Microsoft YaHei", sans-serif;
  }
  .wrap { max-width: 1100px; margin: 0 auto; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: var(--muted); font-size: 13px; margin-bottom: 20px; }
  .panel {
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 16px; margin-bottom: 16px;
  }
  .row { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
  .grow { flex: 1 1 220px; }
  label { color: var(--muted); font-size: 12px; display: block; margin-bottom: 4px; }
  select, input[type=search], input[type=number], input[type=url], input[type=text] {
    width: 100%; padding: 8px 10px; border: 1px solid var(--border);
    border-radius: 7px; background: var(--bg); color: var(--text); font-size: 14px;
  }
  button {
    padding: 8px 14px; border: 1px solid var(--border); border-radius: 7px;
    background: var(--panel); color: var(--text); font-size: 14px; cursor: pointer;
  }
  button:hover { background: var(--hover); }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.primary:hover { filter: brightness(1.1); }
  button:disabled { opacity: .5; cursor: default; }
  button.small { padding: 4px 10px; font-size: 13px; }
  .current {
    display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
  }
  .current .name { font-size: 17px; font-weight: 600; }
  .meta { color: var(--muted); font-size: 12.5px; }
  .meta.warn { color: var(--warn); }
  .meta.bad { color: var(--bad); }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--border); }
  th {
    color: var(--muted); font-size: 12px; font-weight: 600; text-transform: none;
    position: sticky; top: 0; background: var(--panel); cursor: pointer; user-select: none;
  }
  tbody tr:hover { background: var(--hover); }
  tbody tr.is-current { background: var(--hover); }
  td.idx { color: var(--muted); width: 56px; font-variant-numeric: tabular-nums; }
  td.name { word-break: break-all; }
  td.delay { width: 92px; font-variant-numeric: tabular-nums; }
  td.act { width: 84px; text-align: right; }
  .d-good { color: var(--good); }
  .d-warn { color: var(--warn); }
  .d-bad { color: var(--bad); }
  .d-none { color: var(--muted); }
  .tag {
    display: inline-block; padding: 1px 7px; border-radius: 999px; font-size: 11px;
    border: 1px solid var(--border); color: var(--muted); margin-left: 6px;
  }
  .tag.on { border-color: var(--accent); color: var(--accent); }
  .scroll { max-height: 62vh; overflow: auto; }
  .toast {
    position: fixed; right: 20px; bottom: 20px; padding: 10px 16px; border-radius: 8px;
    background: #1f2937; color: #fff; font-size: 13px; opacity: 0;
    transition: opacity .2s; pointer-events: none; max-width: 60vw;
  }
  .toast.show { opacity: 1; }
  .toast.err { background: var(--bad); }
  .step {
    display: flex; gap: 10px; align-items: baseline;
    padding: 7px 0; border-top: 1px solid var(--border);
  }
  .step .dot { flex: 0 0 auto; font-weight: 700; }
  .step.ok .dot { color: var(--good); }
  .step.bad .dot { color: var(--bad); }
  .step.skip .dot { color: var(--muted); }
  .out {
    font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    background: var(--bg); border: 1px solid var(--border); border-radius: 7px;
    padding: 8px 10px; margin: 6px 0 2px; white-space: pre-wrap; word-break: break-all;
    max-height: 240px; overflow: auto; color: var(--muted);
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>mihomo + CLIProxyAPI 节点控制台</h1>
  <div class="sub" id="sub">加载中…</div>

  <div class="panel">
    <div class="current">
      <span class="meta">当前节点</span>
      <span class="name" id="cur">—</span>
    </div>
    <div class="meta" id="provider" style="margin-top:8px"></div>
    <div class="row" style="margin-top:12px">
      <button id="refresh">刷新订阅</button>
      <button id="speedtest">批量测速</button>
      <button id="reload">重新加载</button>
    </div>
  </div>

  <div class="panel">
    <div class="row">
      <div class="grow">
        <label for="group">分组</label>
        <select id="group"></select>
      </div>
      <div class="grow">
        <label for="q">搜索节点名（如 美国 / 新加坡 / 日本）</label>
        <input type="search" id="q" placeholder="留空显示全部">
      </div>
      <div style="width:150px">
        <label for="maxd">延迟上限 (ms)</label>
        <input type="number" id="maxd" min="0" step="50" placeholder="不限">
      </div>
      <div style="width:180px">
        <label for="sort">排序</label>
        <select id="sort">
          <option value="index">按原始序号</option>
          <option value="delay">按延迟（快的在前）</option>
          <option value="name">按名称</option>
        </select>
      </div>
      <div style="padding-top:18px">
        <label style="display:inline-flex;align-items:center;gap:6px;color:var(--text)">
          <input type="checkbox" id="hidebad" checked> 隐藏超时节点
        </label>
      </div>
    </div>
  </div>

  <div class="panel">
    <div class="current">
      <span class="meta">ChatGPT/Codex 订阅</span>
      <span class="name" id="codexStatus">未登录</span>
    </div>
    <div class="meta" id="codexAccounts" style="margin-top:8px">正在读取 CLIProxyAPI 账号…</div>
    <div class="meta" id="codexAccountsUpdated" style="margin-top:6px">
      每 30 秒读取账号快照；用量来自 CLIProxyAPI 记录，并非主动向上游查询实时额度。
    </div>
    <div class="row" style="margin-top:12px">
      <button class="primary" id="codexLogin">登录 Codex</button>
      <button id="codexRefresh">刷新账号/用量快照</button>
    </div>
    <div style="margin-top:12px">
      <label for="codexCallback">OAuth 回调地址（远程服务器登录时，把浏览器地址栏完整 URL 粘贴到这里）</label>
      <div class="row">
        <input class="grow" type="url" id="codexCallback" placeholder="http://localhost:1455/auth/callback?code=…&state=…">
        <button id="codexCallbackSubmit">提交回调</button>
      </div>
    </div>

    <div class="row" style="margin-top:12px">
      <div class="grow">
        <label for="selftestModel">实际生成用的模型（留空自动挑一个带 codex 的）</label>
        <input type="text" id="selftestModel" placeholder="留空自动挑" autocomplete="off">
      </div>
      <div style="padding-top:18px">
        <button id="selftest">连通性自检</button>
        <button class="primary" id="selftestProbe">实际生成一次</button>
      </div>
    </div>

    <div id="selftestOut"></div>
  </div>

  <div class="panel">
    <div class="current">
      <span class="meta">代理例外（哪些网址不走代理）</span>
      <span class="name" id="rulesStatus">读取中…</span>
    </div>
    <div class="meta" id="rulesHint" style="margin-top:8px"></div>

    <div class="row" style="margin-top:12px">
      <div style="width:190px">
        <label for="ruleList">清单</label>
        <select id="ruleList">
          <option value="direct">直连例外（不走代理）</option>
          <option value="proxy">强制走代理</option>
        </select>
      </div>
      <div style="width:210px">
        <label for="ruleKind">匹配方式</label>
        <select id="ruleKind"></select>
      </div>
      <div class="grow">
        <label for="ruleValue">域名 / IP 段</label>
        <input type="text" id="ruleValue" placeholder="example.com" autocomplete="off">
      </div>
      <div style="padding-top:18px">
        <button class="primary" id="ruleAdd">添加并生效</button>
      </div>
    </div>

    <div id="ruleLists" style="margin-top:14px"></div>

    <div style="margin-top:14px">
      <label for="noProxy">备用的 NO_PROXY（当前没有容器设 HTTP_PROXY，用不上；以后给某个容器重新启用代理时必须带上这些，否则容器互访会绕一圈进代理）</label>
      <div class="row">
        <input class="grow" type="text" id="noProxy" readonly>
        <button id="noProxyCopy">复制</button>
      </div>
    </div>

    <div class="row" style="margin-top:12px">
      <button id="rulesApply">重新应用</button>
    </div>
  </div>

  <div class="panel" style="padding:0">
    <div class="scroll">
      <table>
        <thead><tr><th>#</th><th>节点</th><th>延迟</th><th></th></tr></thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const $ = (id) => document.getElementById(id);
let state = null;
let busy = false;

function toast(msg, isError) {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast show' + (isError ? ' err' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.className = 'toast'; }, isError ? 6000 : 2600);
}

async function api(path, options) {
  const res = await fetch(path, Object.assign({ credentials: 'same-origin' }, options));
  let payload = null;
  try { payload = await res.json(); } catch (e) { /* 非 JSON 响应 */ }
  if (!res.ok) {
    throw new Error((payload && payload.error) || ('HTTP ' + res.status));
  }
  return payload;
}

function fmtDelay(ms) {
  if (ms === null || ms === undefined) return '<span class="d-none">—</span>';
  const cls = ms < 300 ? 'd-good' : (ms < 800 ? 'd-warn' : 'd-bad');
  return '<span class="' + cls + '">' + ms + ' ms</span>';
}

function fmtRelative(iso) {
  if (!iso) return '未知';
  const then = new Date(iso);
  if (isNaN(then)) return '未知';
  const diff = Date.now() - then.getTime();
  const mins = Math.round(diff / 60000);
  if (mins < 1) return '刚刚';
  if (mins < 60) return mins + ' 分钟前';
  const hours = Math.round(mins / 60);
  if (hours < 48) return hours + ' 小时前';
  return Math.round(hours / 24) + ' 天前';
}

function fmtCountdown(iso) {
  if (!iso) return '';
  const target = new Date(iso);
  if (isNaN(target)) return '';
  let secs = Math.round((target.getTime() - Date.now()) / 1000);
  if (secs <= 0) return '即将刷新';
  const h = Math.floor(secs / 3600), m = Math.round((secs % 3600) / 60);
  return h > 0 ? (h + ' 小时后自动刷新') : (m + ' 分钟后自动刷新');
}

function visibleNodes() {
  if (!state) return [];
  const q = $('q').value.trim().toLowerCase();
  const maxd = parseFloat($('maxd').value);
  const hasMax = !isNaN(maxd);
  const hideBad = $('hidebad').checked;
  const sort = $('sort').value;

  let nodes = state.nodes.filter((n) => {
    // 分组型节点（如「♻️ 自动选择」「DIRECT」）没有自身延迟，不参与超时过滤，
    // 否则用户一勾「隐藏超时」就看不到当前的自动选择项了。
    if (hideBad && !n.is_group && n.delay === null) return false;
    if (hasMax && !n.is_group && (n.delay === null || n.delay > maxd)) return false;
    if (q && !n.name.toLowerCase().includes(q)) return false;
    return true;
  });

  if (sort === 'delay') {
    nodes = nodes.slice().sort((a, b) => {
      const da = a.delay === null ? Infinity : a.delay;
      const db = b.delay === null ? Infinity : b.delay;
      return da - db || a.index - b.index;
    });
  } else if (sort === 'name') {
    nodes = nodes.slice().sort((a, b) => a.name.localeCompare(b.name, 'zh'));
  }
  return nodes;
}

function render() {
  if (!state) return;

  $('cur').innerHTML = state.current
    ? state.current.replace(/[<>&]/g, (c) => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))
    : '<span class="meta">未选择</span>';

  const p = state.provider || {};
  const box = $('provider');
  if (p.supported) {
    const next = fmtCountdown(p.next_refresh);
    box.className = 'meta';
    box.innerHTML = '订阅上次刷新：' + fmtRelative(p.updated_at)
      + '（周期 ' + Math.round(p.interval / 3600) + ' 小时'
      + (next ? '，' + next : '') + '）';
  } else if (p.reason) {
    box.className = 'meta warn';
    box.textContent = '订阅刷新状态不可用：' + p.reason + '（其余功能不受影响）';
  } else {
    box.className = 'meta warn';
    box.textContent = '订阅刷新状态不可用';
  }

  const groupSel = $('group');
  if (groupSel.options.length !== state.groups.length) {
    groupSel.innerHTML = state.groups.map((g) => {
      const o = document.createElement('option');
      o.value = g; o.textContent = g;
      return o.outerHTML;
    }).join('');
  }
  groupSel.value = state.group;

  const nodes = visibleNodes();
  $('rows').innerHTML = nodes.map((n) => {
    const esc = n.name.replace(/[<>&"]/g,
      (c) => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
    const tags = (n.current ? '<span class="tag on">当前</span>' : '')
      + (n.is_group ? '<span class="tag">分组</span>' : '');
    const btn = n.current
      ? '<button class="small" disabled>已选中</button>'
      : '<button class="small" data-switch="' + esc.replace(/"/g, '&quot;') + '">切换</button>';
    return '<tr class="' + (n.current ? 'is-current' : '') + '">'
      + '<td class="idx">' + n.index + '</td>'
      + '<td class="name">' + esc + tags + '</td>'
      + '<td class="delay">' + fmtDelay(n.delay) + '</td>'
      + '<td class="act">' + btn + '</td></tr>';
  }).join('');

  const total = state.nodes.length;
  $('sub').textContent = state.group + '：显示 ' + nodes.length + ' / ' + total
    + ' 个节点 · ' + new Date().toLocaleTimeString();
}

async function load(group) {
  const qs = group ? ('?group=' + encodeURIComponent(group)) : '';
  state = await api('/api/state' + qs);
  render();
}

let codexFlow = null;
let codexPollTimer = null;

function setCodexStatus(text, className) {
  const el = $('codexStatus');
  el.textContent = text;
  el.className = 'name' + (className ? ' ' + className : '');
}

function codexLabel(f) {
  return f.email || f.name || f.provider || f.type || 'unknown';
}

function codexHealth(f) {
  // 管理面快照与本次自检独立；保留告警，不用单次成功覆盖所有账号。
  if (f.disabled) return { rank: 4, text: '已禁用', cls: 'd-bad' };
  if (f.subscription_until && Date.parse(f.subscription_until) < Date.now()) {
    return { rank: 3, text: '订阅过期', cls: 'd-bad' };
  }
  if (f.status === 'error' || f.unavailable) {
    return { rank: 2, text: '凭据已保存·管理面有异常记录', cls: 'd-warn' };
  }
  return { rank: 1, text: '凭据已保存·管理面未报告异常', cls: 'd-good' };
}

function codexDetail(f) {
  const parts = [codexLabel(f)];
  if (f.plan) parts.push(f.plan);
  if (f.subscription_until) parts.push('订阅至 ' + String(f.subscription_until).slice(0, 10));
  if (f.used_percent !== undefined && f.used_percent !== null) {
    const remaining = f.remaining_percent !== undefined && f.remaining_percent !== null
      ? f.remaining_percent
      : Math.max(0, 100 - Number(f.used_percent));
    parts.push('主配额已用 ' + f.used_percent + '%（剩余 ' + remaining + '%）');
  } else {
    parts.push('主配额用量未知');
  }
  parts.push(codexHealth(f).text);
  let line = parts.join(' · ');
  if (Array.isArray(f.models) && f.models.length) line += '　模型：' + f.models.join('、');
  if (f.status === 'error' && f.status_message) {
    line += '　管理面记录的上游错误（非本次自检结果）：' + f.status_message;
  }
  return line;
}

let codexAccountsRefreshBusy = false;

async function refreshCodexAccounts(showToast) {
  if (codexAccountsRefreshBusy) return codexAccountsRefreshBusy.catch(() => {});
  codexAccountsRefreshBusy = loadCodexAccounts();
  const btn = $('codexRefresh');
  const oldText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '刷新中…';
  try {
    await codexAccountsRefreshBusy;
    $('codexAccountsUpdated').textContent =
      '每 30 秒读取管理面快照（非主动查询上游额度）。快照读取时间：'
      + new Date().toLocaleTimeString();
    if (showToast) toast('账号/用量快照已刷新');
  } catch (e) {
    $('codexAccountsUpdated').textContent =
      '快照刷新失败：' + e.message + '。页面可见时每 30 秒重试。';
    if (showToast) toast('账号/用量快照刷新失败：' + e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = oldText;
    codexAccountsRefreshBusy = false;
  }
}

async function loadCodexAccounts() {
  try {
    const result = await api('/api/cliproxy/accounts', { cache: 'no-store' });
    const files = result.files || [];
    if (!files.length) {
      setCodexStatus('未登录', 'd-none');
      $('codexAccounts').textContent = '尚未发现 Codex 登录账号';
      return;
    }
    // 头部状态取最严重的一档，避免把有问题的账号隐藏掉。
    let worst = null;
    for (const f of files) {
      const h = codexHealth(f);
      if (!worst || h.rank > worst.rank) worst = h;
    }
    setCodexStatus(worst.text, worst.cls);
    $('codexAccounts').textContent = files.map(codexDetail).join('；');
  } catch (e) {
    setCodexStatus('账号状态读取失败', 'd-warn');
    $('codexAccounts').textContent = 'CLIProxyAPI 账号状态暂不可用：' + e.message;
    throw e;
  }
}

async function pollCodexLogin() {
  if (!codexFlow) return;
  try {
    const result = await api('/api/cliproxy/status?provider=' + encodeURIComponent(codexFlow.provider)
      + '&state=' + encodeURIComponent(codexFlow.state));
    if (result.status === 'wait') {
      setCodexStatus('等待 OAuth 回调…', 'meta warn');
      return;
    }
    if (result.status === 'ok') {
      clearInterval(codexPollTimer);
      codexPollTimer = null;
      codexFlow = null;
      setCodexStatus('已登录', 'd-good');
      toast('Codex 登录成功');
      await refreshCodexAccounts(false);
      return;
    }
    clearInterval(codexPollTimer);
    codexPollTimer = null;
    codexFlow = null;
    setCodexStatus('登录失败', 'd-bad');
    toast(result.message || result.error || 'OAuth 登录失败', true);
  } catch (e) {
    setCodexStatus('等待回调…', 'meta warn');
  }
}

async function startCodexLogin() {
  const btn = $('codexLogin');
  btn.disabled = true;
  try {
    const result = await api('/api/cliproxy/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: 'codex' }),
    });
    codexFlow = result;
    setCodexStatus('等待 OAuth 回调…', 'meta warn');
    const popup = window.open(result.url, '_blank', 'noopener');
    if (!popup) toast('浏览器拦截了新窗口，请手动打开登录 URL', true);
    clearInterval(codexPollTimer);
    codexPollTimer = setInterval(pollCodexLogin, 2000);
    await pollCodexLogin();
  } catch (e) {
    toast('启动 Codex 登录失败：' + e.message, true);
    setCodexStatus('未登录');
  } finally {
    btn.disabled = false;
  }
}

async function submitCodexCallback() {
  const value = $('codexCallback').value.trim();
  if (!value) {
    toast('请先粘贴 OAuth 回调地址', true);
    return;
  }
  try {
    const result = await api('/api/cliproxy/callback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: 'codex', url: value }),
    });
    codexFlow = { provider: result.provider || 'codex', state: result.state };
    toast('回调已提交，正在等待 CLIProxyAPI 写入账号');
    await pollCodexLogin();
  } catch (e) {
    toast('提交 OAuth 回调失败：' + e.message, true);
  }
}

function renderSelftest(result) {
  const steps = result.steps || [];
  const head = '<div class="current" style="margin-top:14px">'
    + '<span class="meta">自检结果</span>'
    + '<span class="name ' + (result.ok ? 'd-good' : 'd-bad') + '">'
    + (result.ok ? '全部通过' : '有未通过项') + '</span>'
    + ((result.models || []).length
        ? '<span class="tag on">' + result.models.length + ' 个模型</span>' : '')
    + '</div>';

  const body = steps.map((step) => {
    const cls = step.skipped ? 'skip' : (step.ok ? 'ok' : 'bad');
    const mark = step.skipped ? '·' : (step.ok ? '✓' : '✗');
    return '<div class="step ' + cls + '">'
      + '<span class="dot">' + mark + '</span>'
      + '<div style="flex:1 1 auto;min-width:0">'
      + '<div>' + esc(step.name) + '</div>'
      + '<div class="meta">' + esc(step.detail || '') + '</div>'
      + (step.raw ? '<div class="out">' + esc(step.raw) + '</div>' : '')
      + '</div></div>';
  }).join('');

  $('selftestOut').innerHTML = head + body;

  // 把自动挑中的模型写回输入框，方便针对某个模型重试。
  if (!$('selftestModel').value) {
    const used = steps.map((step) => step.model).filter(Boolean)[0];
    if (used) $('selftestModel').value = used;
  }
}

async function runSelftest(probe) {
  const btn = $(probe ? 'selftestProbe' : 'selftest');
  const other = $(probe ? 'selftest' : 'selftestProbe');
  btn.disabled = true;
  other.disabled = true;
  $('selftestOut').innerHTML = '<div class="meta" style="margin-top:14px">'
    + (probe ? '正在实际生成一次，最长等 90 秒…' : '正在自检…') + '</div>';
  try {
    const result = await api('/api/cliproxy/selftest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        probe: probe, model: $('selftestModel').value.trim(),
      }),
    });
    renderSelftest(result);
    // 生成结束后重新读管理面，避免继续显示生成前的账号状态/用量。
    if (codexAccountsRefreshBusy) await codexAccountsRefreshBusy.catch(() => {});
    await refreshCodexAccounts(false);
  } catch (e) {
    $('selftestOut').innerHTML =
      '<div class="meta bad" style="margin-top:14px">自检失败：' + esc(e.message) + '</div>';
  } finally {
    btn.disabled = false;
    other.disabled = false;
  }
}

async function switchTo(name) {
  if (busy) return;
  busy = true;
  try {
    await api('/api/switch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group: state.group, name: name }),
    });
    toast('已切换到：' + name);
    await load(state.group);
  } catch (e) {
    toast('切换失败：' + e.message, true);
  } finally {
    busy = false;
  }
}

let rulesData = null;

function esc(text) {
  return String(text === null || text === undefined ? '' : text)
    .replace(/[<>&"]/g, (c) => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
}

function rulesHintLines() {
  const s = rulesData;
  const lines = [];
  if (!s.writable && s.note) lines.push(s.note);

  const p = s.providers || {};
  if (p.supported) {
    ['direct', 'proxy'].forEach((which) => {
      const info = (p.providers || {})[which] || {};
      if (info.present) return;
      lines.push(s.lists[which].label + '：mihomo 里没有 rule-provider '
        + s.lists[which].provider + '，这一列不会生效 —— 确认 mihomo/config.yaml '
        + '已声明它，并且已经重启过 proxy 容器。');
    });
  } else if (p.reason) {
    lines.push('生效状态读不到：' + p.reason + '（清单本身仍然可以编辑）');
  }
  return lines;
}

function renderRules() {
  if (!rulesData) return;
  const s = rulesData;

  const kindSel = $('ruleKind');
  if (kindSel.options.length !== s.kinds.length) {
    kindSel.innerHTML = s.kinds.map((k) =>
      '<option value="' + esc(k.kind) + '">' + esc(k.kind + ' · ' + k.label) + '</option>'
    ).join('');
  }

  const total = s.lists.direct.rules.length + s.lists.proxy.rules.length;
  $('rulesStatus').textContent = total ? ('共 ' + total + ' 条规则') : '暂无例外';

  const hints = rulesHintLines();
  $('rulesHint').className = hints.length ? 'meta warn' : 'meta';
  $('rulesHint').textContent = hints.length
    ? hints.join(' ')
    : '直连例外里的域名一律走真实 IP 直出；强制走代理里的域名即使命中「国内直连」规则也从节点走。改动立即生效，不需要重启容器。';

  $('ruleAdd').disabled = !s.writable;
  $('rulesApply').disabled = !s.writable;
  $('ruleList').disabled = !s.writable;

  const blocks = ['direct', 'proxy'].map((which) => {
    const list = s.lists[which];
    const info = ((s.providers || {}).providers || {})[which] || {};
    const head = '<div class="current" style="margin-bottom:6px">'
      + '<span class="name">' + esc(list.label) + '</span>'
      + '<span class="tag">' + esc(list.provider) + '</span>'
      + (info.present
          ? '<span class="tag on">已加载 ' + (info.rule_count ?? '?') + ' 条</span>'
          : '<span class="tag">未加载</span>')
      + '</div>';

    if (!list.rules.length) {
      return head + '<div class="meta">（空）</div>';
    }
    const rows = list.rules.map((rule) =>
      '<tr><td class="name">' + esc(rule.kind) + '</td>'
      + '<td class="name">' + esc(rule.value)
      + (rule.managed ? '' : ' <span class="tag">手工添加</span>') + '</td>'
      + '<td class="act"><button class="small" data-list="' + esc(which)
      + '" data-remove="' + esc(rule.line) + '"' + (s.writable ? '' : ' disabled')
      + '>删除</button></td></tr>'
    ).join('');
    return head + '<table><thead><tr><th>匹配方式</th><th>值</th><th></th></tr></thead>'
      + '<tbody>' + rows + '</tbody></table>';
  });

  $('ruleLists').className = 'scroll';
  $('ruleLists').innerHTML = blocks.join('<div style="height:14px"></div>');
  $('noProxy').value = s.no_proxy || '';
}

async function loadRules() {
  rulesData = await api('/api/rules');
  renderRules();
}

function reportApply(result) {
  if (!result) return;
  const errors = result.errors || [];
  const notes = result.notes || [];
  if (errors.length) {
    toast('规则已保存，但让 mihomo 生效时报错：' + errors.join('；'), true);
    return;
  }
  if (notes.length) {
    toast('规则已生效（' + notes.join('；') + '）', true);
    return;
  }
  if (result.reloaded_config) {
    toast('规则已生效。当前 mihomo 版本不支持单独刷新规则集，这次是整份重载配置，'
      + '节点选择可能被重置，请确认上方当前节点。', true);
    return;
  }
  toast('规则已生效');
}

async function addRule() {
  const value = $('ruleValue').value.trim();
  if (!value) {
    toast('请先填写域名或 IP 段', true);
    return;
  }
  const btn = $('ruleAdd');
  btn.disabled = true;
  try {
    const result = await api('/api/rules/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        list: $('ruleList').value, kind: $('ruleKind').value, value: value,
      }),
    });
    $('ruleValue').value = '';
    rulesData = result;
    renderRules();
    reportApply(result.apply);
  } catch (e) {
    toast('添加失败：' + e.message, true);
  } finally {
    btn.disabled = false;
  }
}

async function removeRule(list, line) {
  try {
    const result = await api('/api/rules/remove', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ list: list, rule: line }),
    });
    rulesData = result;
    renderRules();
    reportApply(result.apply);
  } catch (e) {
    toast('删除失败：' + e.message, true);
  }
}

async function applyRules() {
  const btn = $('rulesApply');
  btn.disabled = true;
  try {
    const result = await api('/api/rules/apply', { method: 'POST' });
    rulesData = result;
    renderRules();
    reportApply(result.apply);
  } catch (e) {
    toast('应用失败：' + e.message, true);
  } finally {
    btn.disabled = false;
  }
}

$('rows').addEventListener('click', (ev) => {
  const btn = ev.target.closest('button[data-switch]');
  if (btn) switchTo(btn.getAttribute('data-switch'));
});

$('ruleLists').addEventListener('click', (ev) => {
  const btn = ev.target.closest('button[data-remove]');
  if (btn) removeRule(btn.getAttribute('data-list'), btn.getAttribute('data-remove'));
});

$('ruleAdd').addEventListener('click', addRule);
$('rulesApply').addEventListener('click', applyRules);
$('ruleValue').addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter') addRule();
});
$('noProxyCopy').addEventListener('click', async () => {
  const field = $('noProxy');
  // 局域网 http 页面不是安全上下文，剪贴板接口常常被拒；先选中，失败时用户直接 Ctrl+C。
  field.select();
  try {
    await navigator.clipboard.writeText(field.value);
    toast('已复制 NO_PROXY');
  } catch (e) {
    toast('已选中全部内容，请按 Ctrl+C 复制', true);
  }
});

$('group').addEventListener('change', () => {
  load($('group').value).catch((e) => toast(e.message, true));
});
['q', 'maxd', 'sort', 'hidebad'].forEach((id) => {
  $(id).addEventListener('input', render);
  $(id).addEventListener('change', render);
});

$('reload').addEventListener('click', () => {
  load(state ? state.group : null).then(() => toast('已重新加载'))
    .catch((e) => toast(e.message, true));
});

$('refresh').addEventListener('click', async () => {
  const btn = $('refresh');
  btn.disabled = true;
  try {
    const r = await api('/api/refresh', { method: 'POST' });
    toast('订阅已刷新，节点数 ' + (r.node_count ?? '?'));
    await load(state ? state.group : null);
  } catch (e) {
    toast('刷新订阅失败：' + e.message, true);
  } finally {
    btn.disabled = false;
  }
});

$('speedtest').addEventListener('click', async () => {
  const btn = $('speedtest');
  btn.disabled = true;
  toast('测速中，' + state.nodes.length + ' 个节点，可能需要一两分钟…');
  try {
    const r = await api('/api/speedtest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group: state.group }),
    });
    const ok = Object.values(r.delays || {}).filter((d) => d > 0).length;
    toast('测速完成：' + ok + ' / ' + Object.keys(r.delays || {}).length + ' 个可用');
    await load(state.group);
  } catch (e) {
    toast('测速失败：' + e.message, true);
  } finally {
    btn.disabled = false;
  }
});

$('codexLogin').addEventListener('click', startCodexLogin);
$('codexRefresh').addEventListener('click', () => {
  refreshCodexAccounts(true);
});
$('codexCallbackSubmit').addEventListener('click', submitCodexCallback);
$('selftest').addEventListener('click', () => runSelftest(false));
$('selftestProbe').addEventListener('click', () => runSelftest(true));

load().catch((e) => toast('加载失败：' + e.message, true));
refreshCodexAccounts(false);
loadRules().catch((e) => {
  $('rulesStatus').textContent = '不可用';
  $('rulesHint').className = 'meta warn';
  $('rulesHint').textContent = '例外清单读取失败：' + e.message;
});
setInterval(() => {
  if (!busy && state) load(state.group).catch(() => {});
  if (!busy) loadRules().catch(() => {});
  if (!document.hidden && !codexFlow && !$('selftestProbe').disabled) refreshCodexAccounts(false);
}, 30000);
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "proxy-console"
    sys_version = ""

    # -- 响应工具 ---------------------------------------------------------

    def _send(self, status, body, content_type):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status=200):
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _error(self, status, message):
        self._json({"error": message}, status)

    # -- 鉴权 -------------------------------------------------------------

    def _authorized(self):
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        user, _, password = decoded.partition(":")
        # 常量时间比较，避免通过响应时间逐字符爆破。
        return hmac.compare_digest(user, AUTH_USER) and hmac.compare_digest(
            password, AUTH_PASSWORD
        )

    def _require_auth(self):
        if self._authorized():
            return True
        # 先把请求体读掉再拒绝。不读就回 401 的话，客户端可能还在写 body，
        # 它会看到连接被重置而不是 401（带 body 的 POST 在 Windows 上必现）。
        # 这里只是丢弃，不做任何解析。
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 0:
            self.rfile.read(length)
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="mihomo console"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    # -- 请求解析 ---------------------------------------------------------

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _query(self):
        _, _, raw = self.path.partition("?")
        return urllib.parse.parse_qs(raw)

    def log_message(self, fmt, *args):
        # 默认实现会把请求行写进 stderr，里面可能带 query，但不会有密码。
        sys.stderr.write("[console] %s - %s\n" % (self.address_string(), fmt % args))

    # -- 路由 -------------------------------------------------------------

    def do_GET(self):
        if not self._require_auth():
            return
        path = urllib.parse.urlparse(self.path).path

        if path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
            return
        if path == "/api/state":
            group = (self._query().get("group") or [None])[0]
            try:
                self._json(build_state(group))
            except MihomoError as exc:
                self._error(502, str(exc))
            return

        if path == "/api/rules":
            try:
                self._json(rules_state())
            except RuleError as exc:
                self._error(exc.status or 400, str(exc))
            return

        if path == "/api/cliproxy/accounts":
            try:
                self._json(cliproxy_accounts())
            except CLIProxyError as exc:
                self._error(502 if exc.status != 400 else 400, str(exc))
            return

        if path == "/api/cliproxy/status":
            query = self._query()
            provider = (query.get("provider") or ["codex"])[0]
            state = (query.get("state") or [""])[0]
            try:
                self._json(cliproxy_oauth_status(provider, state))
            except CLIProxyError as exc:
                self._error(502 if exc.status != 400 else 400, str(exc))
            return

        self._error(404, "未知路径：%s" % path)

    def do_POST(self):
        if not self._require_auth():
            return
        path = urllib.parse.urlparse(self.path).path
        body = self._body()

        try:
            if path == "/api/switch":
                self._switch(body)
            elif path == "/api/refresh":
                self._refresh()
            elif path == "/api/speedtest":
                self._speedtest(body)
            elif path == "/api/rules/add":
                add_rule(str(body.get("list") or "direct"), body.get("kind"), body.get("value"))
                self._json(rules_state(apply_ruleset()))
            elif path == "/api/rules/remove":
                remove_rule(str(body.get("list") or "direct"), body.get("rule"))
                self._json(rules_state(apply_ruleset()))
            elif path == "/api/rules/apply":
                self._json(rules_state(apply_ruleset()))
            elif path == "/api/cliproxy/selftest":
                self._json(
                    cliproxy_selftest(
                        probe=bool(body.get("probe")),
                        model=str(body.get("model") or "").strip(),
                    )
                )
            elif path == "/api/cliproxy/login":
                provider = str(body.get("provider") or "codex").strip().lower()
                self._json(cliproxy_start_login(provider))
            elif path == "/api/cliproxy/callback":
                self._json(cliproxy_submit_callback(body))
            else:
                self._error(404, "未知路径：%s" % path)
        except MihomoError as exc:
            self._error(502, str(exc))
        except RuleError as exc:
            self._error(exc.status or 400, str(exc))
        except CLIProxyError as exc:
            self._error(502 if exc.status != 400 else 400, str(exc))

    # -- 动作 -------------------------------------------------------------

    def _switch(self, body):
        group = body.get("group") or DEFAULT_GROUP
        name = body.get("name")
        if not name:
            self._error(400, "缺少 name")
            return

        # 先确认目标确实在这个分组的成员列表里。mihomo 对不在列表里的名字会静默
        # 接受，切换看起来成功但实际没生效，这里提前拦掉。
        proxies = get_proxies()
        entry = proxies.get(group)
        if not is_group(entry):
            self._error(400, "分组不存在：%s" % group)
            return
        if name not in entry["all"]:
            self._error(400, "节点 %s 不在分组 %s 里" % (name, group))
            return

        path = "/proxies/" + urllib.parse.quote(group, safe="")
        mihomo("PUT", path, {"name": name})

        # 回读一次，确认真的切过去了。
        proxies = get_proxies()
        now = (proxies.get(group) or {}).get("now")
        self._json({"ok": True, "group": group, "current": now})

    def _refresh(self):
        path = "/providers/proxies/" + urllib.parse.quote(PROVIDER_NAME, safe="")
        try:
            mihomo("PUT", path, timeout=60)
        except MihomoError as exc:
            if exc.status == 404:
                self._error(
                    501,
                    "这个 mihomo 版本不支持通过控制接口刷新订阅，"
                    "请重启代理容器：docker compose restart proxy",
                )
                return
            raise

        info = provider_info()
        self._json({"ok": True, "node_count": info.get("node_count")})

    def _speedtest(self, body):
        group = body.get("group") or DEFAULT_GROUP
        timeout_ms = 5000
        params = urllib.parse.urlencode(
            {"url": HEALTHCHECK_URL, "timeout": timeout_ms}
        )
        path = "/group/%s/delay?%s" % (urllib.parse.quote(group, safe=""), params)
        try:
            delays = mihomo("GET", path, timeout=120)
        except MihomoError as exc:
            if exc.status == 404:
                self._error(
                    501,
                    "这个 mihomo 版本不支持分组批量测速，请逐个切换后观察延迟",
                )
                return
            raise
        if not isinstance(delays, dict):
            delays = {}
        self._json({"ok": True, "delays": delays})


def main():
    if not AUTH_PASSWORD:
        print(
            "CONSOLE_PASSWORD 未设置，拒绝以空密码启动。\n"
            "请在 .env 里设置 CONSOLE_PASSWORD 后重试。",
            file=sys.stderr,
        )
        return 1

    host, _, port_text = LISTEN.rpartition(":")
    if not host:
        host = "127.0.0.1"
    try:
        port = int(port_text)
    except ValueError:
        print("CONSOLE_LISTEN 格式应为 host:port，当前为 %r" % LISTEN, file=sys.stderr)
        return 1

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True

    print("mihomo 控制台已启动： http://%s:%d" % (host, port))
    print("  控制接口: %s" % CONTROLLER)
    print("  默认分组: %s" % DEFAULT_GROUP)
    print("  登录用户: %s" % AUTH_USER)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
