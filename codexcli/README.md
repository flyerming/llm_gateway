# codexcli —— 让 Codex CLI 免登录直连私有化网关，`/model` 直接列出网关模型

一条命令，把 Linux 上的 Codex CLI 从官方 API 切到你们自己的 LiteLLM 网关：

```bash
python3 private_api.py \
    --api-base http://10.18.219.156:4000 \
    --api-key  sk-XXXXXXXX
```

跑完之后 `codex` 打开就是**网关的模型**，**不弹登录**，`/model` 里能直接切换。

不带 `--api-base` 在终端里跑，会进入交互式向导。

**web search 也在这条命令里配好了**，不用再跑第二条：脚本会给 Codex CLI 注册一个指向网关的 MCP 端点，凭据复用你刚填的那把 LiteLLM key —— 全程只有**一把 key**，没有第二个 token 要申请。细节见[搜索（web search）](#搜索web-search)。

只用 Python 标准库，**不需要 `pip install`**（`from __future__ import annotations` 已开，Python 3.8+ 都能跑）。
入口脚本是 `codexcli/private_api.py`，下面所有命令都假设你在 `codexcli/` 目录里执行。

> 这套工具**只在 Linux 上跑**（你的 codex 装在 Linux）。Windows 上那份对应的实现是 [`vscode/`](../vscode/README.md)，两者共用同一套 `private-reasoning.json` / `private-modalities.json` 覆盖文件，同一台机器两套都配过也不会打架。

> **适配版本：codex-cli 0.154.0**（`private-api/codex.py` 的 `TARGET_VERSION`）。配置项、`wire_api`、模型目录的字段集都是照着 0.154.0 的源码写的。0.143 起的版本目录仍然兼容（本工具会把新旧两种字段名都写上），比 0.154.0 新的版本每次运行都会提示需要重新适配。详见[版本兼容](#版本兼容codex-的版本敏感字段)。

---

## 目录

- [做了什么](#做了什么)
- [自动检索哪些位置](#自动检索哪些位置)
- [四个部件，缺一不可](#四个部件缺一不可)
  - [1. `config.toml`：指向网关](#1-configtoml指向网关)
  - [2. `model_catalog_json`：`/model` 列表的来源](#2-model_catalog_jsonmodel-列表的来源)
    - [版本兼容：codex 的版本敏感字段](#版本兼容codex-的版本敏感字段)
  - [3. `auth.json`：不写它就一直弹登录](#3-authjson不写它就一直弹登录)
  - [4. 自动刷新：`codex` 包装脚本](#4-自动刷新codex-包装脚本)
- [模型能力配置：`model-config.jsonc`](#模型能力配置model-configjsonc)
- [思考档位（reasoning）](#思考档位reasoning)
- [上下文窗口（context_window）](#上下文窗口context_window)
- [能不能贴图（input_modalities）](#能不能贴图input_modalities)
- [搜索（web search）](#搜索web-search)
- [网关（LiteLLM）侧改动](#网关litellm侧改动)
- [命令速查](#命令速查)
- [常见问题](#常见问题)
- [目录结构](#目录结构)

---

## 做了什么

| 改的东西 | 说明 |
|---|---|
| `$CODEX_HOME/config.toml` | 写 `[model_providers.private]` + `model` + `model_provider` |
| `$CODEX_HOME/config.toml` 的 `model_catalog_json` | **把 `/model` 列表换成网关模型**，不再显示 GPT-5.6 Sol/Terra/Luna 这些网关上没有的（见下） |
| `$CODEX_HOME/gateway-models.json` | 生成的模型目录，就是上一行指向的文件 |
| `$CODEX_HOME/auth.json` | **不写这个 Codex 永远弹登录**（见下） |
| `$CODEX_HOME/private-reasoning.json` | 仅 `--configure-reasoning`。每个私有模型暴露哪些思考档位 |
| `$CODEX_HOME/private-modalities.json` | 仅 `--configure-modalities`。哪些模型能贴图；默认取实测表，只在覆盖时才产生该文件 |
| `$CODEX_HOME/config.toml` 的 `[mcp_servers.searxng]` | 向导最后一步自动写，也可用 `--configure-search` 单独重配。**让模型能搜索**——Codex 自带的 web search 是托管工具，网关执行不了（见下） |
| `$CODEX_HOME/.gateway-models.stamp` | 上次刷新目录的时间戳，包装脚本靠它做节流 |
| `~/.local/bin/codex` | 仅 `--install-wrapper`。两行 shell 包装脚本，启动前刷新目录 |
| `codexcli/bin/codex-model` | 切模型的小脚本，跑配置向导时自动生成 |
| 网关 | 仅 `--apply-gateway-config`。私有模型的 `litellm_params`，**不改这个 Codex 每轮请求都 400/500** |

**每个文件在写入前都会备份成 `<文件名>.bak`**，`--restore` 可以一键还原。
`.bak` 里存的**永远是最初的原始版本**：反复跑脚本不会把备份覆盖成上一次的输出，所以 `--restore` 一定回到你没动过的状态。还原后 `.bak` 会被删掉，下次再配置时重新快照。

`config.toml` 采用**"文本手术"**而不是「解析→重新序列化」（`private-api/tomlpatch.py`）：这个文件是你自己手工维护的，里面有几百行 `[projects.'e:\...']` 反斜杠字面量、`[mcp_servers.*]`、`[plugins.*]`，重新序列化会把它们全搞乱。脚本只动它该动的那几行，**其余字节原样复制**。

---

## 自动检索哪些位置

| 类型 | 位置 |
|---|---|
| 配置 | `$CODEX_HOME/config.toml`，默认 `~/.codex/config.toml` |
| 登录态 | `$CODEX_HOME/auth.json` |
| Codex 自己拉的模型缓存 | `$CODEX_HOME/models_cache.json` |
| 二进制 | `which codex` 优先，找不到再扫 `~/.local/bin`、`~/.npm-global/bin`、`~/.cargo/bin`、`/usr/local/bin`、linuxbrew 等 |

想先看一眼结果、不做任何改动：

```bash
python3 private_api.py --detect     # 只列出找到的文件和版本
python3 private_api.py --status     # 文件 + 当前配置状态（推荐先跑这个）
```

`--status` 会告诉你：`config.toml` 里现在 pin 的模型、目录里有几个模型、**是否已登录**、有没有 per-model 覆盖、包装脚本装没装。

---

## 四个部件，缺一不可

### 1. `config.toml`：指向网关

```toml
model          = "deepseek-v4.1-flash"
model_provider = "private"
model_catalog_json = '/home/you/.codex/gateway-models.json'

[model_providers.private]
name = "Private Gateway"
base_url = "http://10.18.219.156:4000/v1"
wire_api = "responses"
requires_openai_auth = false
experimental_bearer_token = "sk-XXXXXXXX"
```

几个关键点：

**`base_url` 要带 `/v1`。** Codex 拿到 `base_url` 后自己拼 `/responses`。`--api-base` 你写带不带 `/v1` 都行，脚本会归一化。

**`requires_openai_auth = false`。** 这个值的默认本来就是 `false`，但显式写出来，免得将来某次合并配置时被别的 profile 带成 `true`——一旦是 `true`，Codex 会去找 ChatGPT 账号，而私有网关永远给不了。

**`wire_api` 只能是 `"responses"`。** Codex 0.150 **删掉了** `wire_api = "chat"`，还留着会**直接硬报错**：

```
`wire_api = "chat"` is no longer supported.
How to fix: set `wire_api = "responses"` in your provider config.
```

也就是说**网关必须能代理 `POST /v1/responses`**（LiteLLM 可以）。脚本在写配置前会先探一下这个端点，不通就当场告诉你，而不是让你等到第一句话才炸。

**key 放哪。** 默认是**内联**写进 `experimental_bearer_token`。为什么不默认用环境变量：Linux 服务器上你多半是 `docker exec` 或 ssh 上来跑，**非交互 shell 不会 source `~/.bashrc`**，环境变量根本不存在，Codex 会报找不到 `env_key`。想要环境变量就加 `--use-env-key`（脚本会把 `export PRIVATE_API_KEY=...` 追加进 `~/.bashrc`/`~/.zshrc`/`~/.profile`）。

安全性上两种都一样是明文落盘——`auth.json` 里无论如何都要存一份。

### 2. `model_catalog_json`：`/model` 列表的来源

**这是整个工具存在的理由。**

Codex **不会**像 Claude Code 那样去拉 `${base_url}/models`。它的模型列表来自一张**内置目录**，默认是 OpenAI 自家那套（GPT-5.6 Sol/Terra/Luna、GPT-5.5…），网关上**一个都没有**，选中了第一句话就失败。

`model_catalog_json` 指向一个 JSON 文件，**整份替换**掉内置目录。脚本从 `GET /v1/models` 拉到网关的模型，生成这份文件，所以 `/model` 里出现并且只出现网关的模型。

生成的目录长这样（`$CODEX_HOME/gateway-models.json`）：

```json
{
  "models": [
    {
      "slug": "deepseek-v4.1-flash",
      "display_name": "deepseek-v4.1-flash",
      "description": "Private gateway model · 128K context",
      "priority": 1,
      "visibility": "list",
      "supported_reasoning_levels": [
        { "effort": "low",   "description": "Fast responses with lighter reasoning" },
        { "effort": "high",  "description": "Greater reasoning depth for complex problems" },
        { "effort": "max",   "description": "Maximum reasoning depth for the hardest problems" }
      ],
      "default_reasoning_level": "high",
      "shell_type": "unified_exec",
      "supported_in_api": true,
      "support_verbosity": false,
      "truncation_policy": { "mode": "tokens", "limit": 10000 },
      "experimental_supported_tools": [],
      "base_instructions": "You are Codex, a coding agent. ...",
      "supports_reasoning_summaries": false,
      "supports_reasoning_summary_parameter": false,
      "supports_parallel_tool_calls": false,
      "context_window": 128000,
      "max_context_window": 128000,
      "apply_patch_tool_type": "freeform",
      "supports_search_tool": false,
      "input_modalities": ["text", "image"]
    }
  ]
}
```

几个字段值得单独说：

| 字段 | 为什么这么填 |
|---|---|
| `slug` | 用网关的 model id **原样**。Codex 把它当 `model` 发出去，LiteLLM 就按这个字符串路由 |
| `visibility: "list"` | 只有 `list` 才进 `/model` 选择器。Codex 自家的 `gpt-reserve` 用的是 `hide` |
| `base_instructions` | 必填二选一（另一个是 `model_messages.instructions_template`）。0.147 起这个顶层写法是靠兼容层映射进去的，但**照样必填**，不填整个目录解析失败 |
| `apply_patch_tool_type: "freeform"` | **不填 Codex 就不提供 `apply_patch` 工具，agent 改不了文件** |
| `supports_reasoning_summaries`<br>`supports_reasoning_summary_parameter` | **同一个字段改名前后两种写法，两个都发**——见[版本兼容](#版本兼容codex-的版本敏感字段)。都填 `false`，理由见下 |
| `supports_parallel_tool_calls: false` | 0.147 及以前必填，0.148 起已从 `ModelInfo` 删除（发了会被忽略）。关掉是因为没有证据表明后端支持并行工具调用，填错只在任务中途才暴露 |
| `supports_search_tool: false` | 联网搜索会被代理回 OpenAI 后端，不是网关，所以关掉 |
| `context_window` | 网关照实报 `max_input_tokens` 时用它，没报就退回 **128K**。实测 `deepseek-v4.1-flash`、`glm-5.3-flash`、`qwen3-5-397b`、`xinghai-ultra` 这四个网关没报，都落在 128K |

> **两个 summary 开关为什么必须是 `false`**：它决定 Codex 会不会往请求里塞 `reasoning.summary`。2026-09-17 实测，网关对这个参数直接 **400**：
> ```
> litellm.BadRequestError: Custom_openaiException - Invalid OpenAI-compatible chat request:
> invalid type: map, expected a string
> ```
> 这和当初 `reasoning_effort` 那个坑是同一类。更要命的是 **0.145+ 这个字段的默认值是 `true`**——不显式写 `false`，新版本 codex 每轮都会带上 `reasoning.summary` 然后 400。

目录里**只放能聊天的模型**：`gpt-image-*` 那 4 个图像端点会被跳过——Codex 会愉快地接受这个 slug，然后在你第一句话时失败。

`base_instructions` 和这些字段是**必填的**：`serde` 缺字段会一行一行报 `missing field \`x\``，`models` 是空数组则报 `must contain at least one model`。

### 版本兼容：codex 的版本敏感字段

**目录格式在 0.14x 期间改过名，写错一边就会让 codex 起不来。** 这就是本工具要发三个"多余"字段的原因。下表每一格都来自对应版本的 `openai_models.rs` 源码：

| 版本 | `supports_reasoning_summaries` | `supports_reasoning_summary_parameter` | `supports_parallel_tool_calls` | 顶层 `base_instructions` |
|---|---|---|---|---|
| 0.143 ~ 0.144 | **必填** | —— | **必填** | **必填** |
| 0.145 ~ 0.146 | 已改名，忽略 | 选填，**默认 `true`** | **必填** | **必填** |
| 0.147 | 忽略 | 选填，默认 `true` | **必填** | 已移出 `ModelInfo` |
| 0.148 ~ **0.154** | 忽略 | 选填，默认 `true` | （已删除） | 靠 legacy shim 兼容 |
| 0.155+ | （已删除） | 选填，默认 `true` | （已删除） | 靠 legacy shim 兼容 |

本工具**三个都发**。这是安全的：从 0.143 到 main，`ModelInfo` 都没有 `#[serde(deny_unknown_fields)]`，**不认识的 key 直接忽略**，所以同一份目录在所有版本上都能加载。

> **实际踩过的坑**：Linux 那台 `codex-cli 0.144.1` 启动时报
> `missing field \`supports_reasoning_summaries\` at line 40 column 5`
> ——0.144 要求这个字段，而当时的目录没发。修完它之后还会连着报 `supports_parallel_tool_calls`，因为 serde **一次只报一个**。

> **`base_instructions` 现在为什么还能填**：0.147 把它从 `ModelInfo` 里拿掉了，但 0.147+ 的目录反序列化走的是 `deserialize_model_infos_with_legacy_base()`——一个兼容层，会把顶层的 `base_instructions` 提升成 `model_messages.instructions_template`；两者都没有就报
> `model \`x\` is missing both \`base_instructions\` and \`model_messages.instructions_template\``。
> 所以 0.154.0 上它**照样是必填**，只是换了条路进去。

**本工具适配的版本**（`private-api/codex.py` 顶部三个常量）：

| 常量 | 值 | 含义 |
|---|---|---|
| `TARGET_VERSION` | **0.154.0** | **本工具就是照着这个版本改的**。配置项、`wire_api`、目录字段集都取自 0.154.0 源码 |
| `MIN_SUPPORTED_VERSION` | **0.143** | 再往下的版本没有证据，工具会明确说"太旧"而不是瞎猜 |
| `TESTED_THROUGH_VERSION` | **0.153.4** | 真正被二进制解析过的最高版本（`codex debug models` 干净退出）。0.154.0 的字段集是从源码核对的，没有 0.154.0 的二进制跑过 |

**每次运行都会报版本**，`--setup` / `--status` / `--detect` / `codex --help` 都会带一行：

```
  codex: codex-cli 0.154.0 -- the version this toolkit is adapted to
```

版本对不上时同样明说。比目标旧但在支持范围内：

```
  codex: codex-cli 0.144.1 is older than the adapted version (0.154.0), but still in
    the supported range (0.143+); the catalog covers the older key set too
```

比目标新（**升级 codex 后看到这行就要重新适配**）：

```
  codex: codex-cli 0.160.0 is NEWER than the adapted version (0.154.0). Config and
    catalog keys have moved before now, so if `codex` fails to start or ignores these
    models, re-adapt the toolkit against 0.160.0.
```

低于下限才前缀 `!`（这种情况才真的可能起不来）。另外 `--sync` 只在**真正重写了目录**的那次才提示——节流跳过的那次不跑 `codex --version`，避免拖慢每次启动：

```
  ! codex-cli 0.142.0 is OLDER than the oldest supported release (0.143). The catalog
    it writes may be rejected at startup -- upgrade codex, or expect `missing field`
    errors from its model catalog parser.
```

升级/降级 codex 后，先跑 `python3 private_api.py --detect` 看这一行，再跑 `codex debug models` 做权威确认。

**要适配新版本时改哪里**：`private-api/codex.py` 的 `TARGET_VERSION`，加上模块 docstring 里那段字段说明；如果新版本又挪了字段，就照上面的办法再发一份"多余的写法"，然后更新本节表格。

**证据一**：目录里只列网关模型时，`codex -m gpt-5.6-sol` 会打印 `Model metadata for 'gpt-5.6-sol' not found. Defaulting to fallback metadata`——说明这份目录是**替换**而不是**追加**。

**证据二（推荐随时自己验）**：`codex debug models` 让 **codex 二进制自己**把当前生效的目录 dump 成 JSON。目录写得对不对，这是唯一的权威答案——serde 缺字段会在这里报 `missing field`，而我们这份它是干净退出的：

```bash
codex debug models            # 当前生效的目录（会读 model_catalog_json）
codex debug models --bundled  # 对比：codex 内置的那份，不联网
```

实测 `codex-cli 0.153.4` + 本工具生成的目录：

```
count: 10
  deepseek-v4-flash        vis=list  efforts=low,high,max                mods=text        ctx=1000000
  deepseek-v4.1-flash       vis=list  efforts=low,high,max                mods=text,image  ctx=128000
  glm-5.3-flash            vis=list  efforts=low,high,max                mods=text,image  ctx=128000
  gpt-5.5                  vis=list  efforts=low,medium,high,xhigh       mods=text,image  ctx=1050000
  gpt-5.6-sol              vis=list  efforts=low,medium,high,xhigh,max,ultra  mods=text,image  ctx=922000
  qwen3-5-397b             vis=list  efforts=low,high,max                mods=text,image  ctx=128000
  xinghai-ultra            vis=list  efforts=low,high,max                mods=text        ctx=128000
  ...
```

10 个模型、全部 `vis=list`（都会出现在 `/model` 里）、档位/图片能力与配置一致，且**没有任何网络请求**（`model_catalog_json` 一设，Codex 就改用静态目录，不再去后端拉 `/models`）。

> 注意 `codex debug models` 会**优先从后端拉一次**（`OnlineIfUncached`），只有设了 `model_catalog_json` 才走静态目录。想完全离线用 `--bundled` 那种对比方式。

### 3. `auth.json`：不写它就一直弹登录

**这是最反直觉的一条。**

即使 `model_provider` 已经指向私有网关、`requires_openai_auth = false`，Codex **照样**会弹登录界面：

```bash
$ codex login status
Not logged in
```

因为 Codex 判断"是否已登录"的方法就是**看 `$CODEX_HOME/auth.json` 存不存在**，和这一轮的请求会走哪个 provider 无关。文件不在，就要登录——provider 表里写什么都没用。

修法：往里写一个 **API key**（Codex 本来就有 apikey 模式）：

```json
{
  "OPENAI_API_KEY": "sk-XXXXXXXX",
  "tokens": null,
  "last_refresh": null
}
```

写进去的值就是**网关的 key**——provider 表本来就用它认证，所以这没有引入新密钥，只是把一个**已经在这台机器上的**密钥（`config.toml` 里）挪到 Codex 真正会读的那个文件里。

单独处理登录（会**先验证 key 真能用**，被网关拒绝就不写，免得看起来登录了实际每轮都失败）：

```bash
python3 private_api.py --fix-login          # 已有登录态时不覆盖
python3 private_api.py --fix-login --force  # 强制重写
```

⚠️ **已经用 ChatGPT 账号登录过的话，配置向导不会覆盖它**。一个真的 ChatGPT 登录**已经满足**了登录闸门，而私有 provider 根本不会读它那份 token，覆盖掉纯属白扔一个能用的账号。要强行覆盖用 `--fix-login --force`。

### 4. 自动刷新：`codex` 包装脚本

网关加了模型，`/model` 得能看到。两种办法：

```bash
python3 private_api.py --refresh-models     # 手动刷新一次
python3 private_api.py --install-wrapper    # 装包装脚本，每次启动自动刷
```

`--install-wrapper` 会在 `~/.local/bin/codex` 写一个 shell 脚本，exec 真正的 codex 之前**先刷一次目录**。它的设计约束是**绝不能拖慢或挡住 codex 启动**：

- **节流**：`$CODEX_HOME/.gateway-models.stamp` 在 300 秒内就不刷（`--refresh-ttl` 可调）；
- **永远退出 0**：网关慢、挂了、token 过期，都只是"继续用上次那份目录"，codex 照常启动。`--sync` 里所有异常都被吞掉；
- **超时 8 秒**：故意不耐烦。宁可让你拿一份旧列表，也不要卡住。

```bash
python3 private_api.py --sync --force   # 手动跑一次看它干了什么（不加 --force 是静默的）
python3 private_api.py --uninstall-wrapper
```

⚠️ **包装脚本要生效，`~/.local/bin` 必须在 `PATH` 里**，而且要比真实 codex 所在的目录靠前。脚本装完会检查并提示：

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```

没装包装脚本也完全能用，只是目录不会自动更新（`/model` 里看不到新加的模型）。

---

## 模型能力配置：`model-config.jsonc`

下面三节（思考档位、上下文窗口、能不能贴图）说的是**同样三件事**——每个模型能不能贴图、有几档思考、上下文多长。这三项**不再是 Python 常量**，而是一份归你所有的数据文件：

```
codexcli/                          ← 解开 zip 就是这里
├── private_api.py
├── model-config.jsonc             ← 首次运行生成，之后归你改
└── private-api/
    └── model-config.seed.jsonc    ← 随包发的实测知识库（别改，会被下次解压盖掉）
```

**为什么放在脚本旁边而不是 `$CODEX_HOME` 里。** 放 `~/.codex/` 更"规范"，但用户找不到的配置文件等于没人改的配置文件。就放在你刚解压的目录里，`--status` 也会把它全路径打出来。

### 一次配置，两步走

```bash
python3 private_api.py --refresh-models        # 第 1 步：生成/追加，并【立即应用】
python3 private_api.py --apply-model-config    # 第 2 步：你改完之后，同步进真实环境
```

1. **首次运行（安装向导 / `--refresh-models`）**生成 `model-config.jsonc`，内容 = `model-config.seed.jsonc` 的实测结论 ⊕ 网关当前模型列表，然后**立刻**写进 `$CODEX_HOME/gateway-models.json`。**装完就能用**，第 2 步不是前置条件。
2. 之后每次运行只做一件事：把网关**新出现**的 slug 追加进去。**绝不重写你已经写下的条目，注释也不动**——所以手改是安全的，不会被下一次 `--refresh-models` 抹掉。
3. 你改完 → `--apply-model-config` 校验、重新解析、重写目录。

### 生成的是什么

每条模型一条，字段就这四项（`_defaults` 是没单独配置时套用的兜底）：

```jsonc
"deepseek-v4.1-flash": {
  "input_modalities": ["text", "image"],   // 实测 2026-09-17：能分辨红/蓝
  "reasoning_levels": ["low", "high", "max"],
  "default_reasoning_level": "high",
  "context_window": 128000
}
```

**建议值就是设计需求里那三条**：`input_modalities` 默认只有 `text`；`context_window` 网关报了 `max_input_tokens` 就用网关的、没报就 **128K**；思考档位非 OpenAI 模型默认 **三档** `low/high/max`。实测能看图的那些模型（见下表）已经预填成 `["text","image"]`，不用你手点。

### 解析优先级（逐字段独立，高 → 低）

1. `model-config.jsonc` 里该模型的这个字段 ← **你写的**
2. 旧的 `private-modalities.json` / `private-reasoning.json`（**只读**，老装机沿用旧选择）
3. OpenAI 官方模型（`gpt-*`/`o*`）→ `models_cache.json`，**不覆盖官方声明**
4. 网关 `GET /v1/models` 主动广告的字段
5. `model-config.seed.jsonc` 里该模型的实测值
6. `_defaults` ⊕ `_toolkits[<工具包>]`

**删一个字段就回落一个字段**，不需要什么"恢复默认"命令。

### `_toolkits` 不是装饰

两个工具包的默认思考档位**故意不同**：codexcli 是 `low/high/max`，vscode 是 `low/high/xhigh`——vscode 的 webview 画不出 `max` 那一行（画不出就整行不显示，看着像功能丢了）。共用一份 `_defaults` 会把那个已修好的显示 bug 带回来，所以按工具包覆盖：

```jsonc
"_toolkits": { "vscode": { "reasoning_levels": ["low", "high", "xhigh"] } }
```

### 为什么是平铺，不是嵌套在 `"models"` 下

因为 `private-api/jsonc.py` 的 `set()` **只支持顶层键**，且会用它重编码整个被替换的值。一旦嵌套，任何一次写入都会**抹掉 `models` 里所有注释**——而"追加新模型"恰恰是最常发生的写入。

### 写坏了会怎样（fail-open）

文件语法错、值非法 → **目录照样生成，Codex 照样启动**，坏值被丢掉、回落到 seed 与 `_defaults`。但**不会静默**：`--status` 和 `--apply-model-config` 会把问题打出来，语法错还会给出精确的行列号。

```console
! 3 problem(s) -- the bad values are ignored, the rest still applies
    deepseek-v4.1-flash.input_modalities: 'hologram' 不是 Codex 认的值（只有 text/image/audio）
    deepseek-v4.1-flash.context_window: 应该是整数
```

这条是**必须**的：值非法会让整个目录解析失败 → **Codex 直接起不来**（不是降级）。所以枚举校验留在代码里（`KNOWN_MODALITIES` / `KNOWN_EFFORTS`），配置只提供值。

> **网关改了模型 id 就是一次重测，不是 find-and-replace。** 这个坑踩过一次：`deepseek-v4.1-flash-test` 被网关改名成 `deepseek-v4.1-flash`，实测表里还是旧 id → 新 id 落进默认的 `("text",)` → **Codex 在客户端就拒掉贴图**，提示 "This model does not support image inputs."。新老用户一律中招，因为生效的是**代码**，本地文件里没有可改的东西。
> 这件事本身就是这次改动的起因：**能力写在代码里，网关改个名就等于让所有人丢失能力，而且修复只能靠改源码 + 重新打包分发。** 现在改数据文件即可，`--probe-modalities` 给结论。

---

## 思考档位（reasoning）

Codex 的思考强度菜单**完全由我们生成的目录决定**，两个字段：

- `supported_reasoning_levels` —— 菜单里有哪几档（**是对象数组，不是字符串数组**，写 `["low","high"]` 会让整个目录解析失败、Codex 起不来）
- `default_reasoning_level` —— 打开时停在哪一档

**默认给私有模型的是 `low / high / max` 三档**（对应设计需求里"custom-openai 仅支持三级"）。

哪些档位"有意义"是实测出来的——对着本部署的后端逐个模型、逐个档位打 `POST /v1/responses`：

| 模型 | 实测结果 |
|---|---|
| `deepseek-v4.1-flash` | `low high xhigh max` → 200；`minimal medium` → **400**（"reasoning_effort must be low, high, xhigh, max, or an integer within [1, 10]"） |
| `deepseek-v4-flash`、`glm-5.3-flash`、`qwen3-5-397b`、`xinghai-ultra` | 所有值都 200——**但后端根本不校验**，`medium` 收下就悄悄忽略，**所以 200 什么都证明不了** |

注意严格后端实际接受的是**四档**，`xhigh` 是 `max` 下面紧邻的一档，并不是 `max` 的同义词。它不在默认值里（需求要三档），但**一个参数就能加回来**：

```bash
python3 private_api.py --configure-reasoning \
    --reasoning-model deepseek-v4.1-flash \
    --reasoning-levels xhigh          # low,high,xhigh,max —— 当需要那额外一档时
```

**`minimal` 和 `medium` 故意不给**：严格后端上它们每轮都是 400，宽松后端上它们和 `high` 没区别——一个毫无意义却看着像选项的档位。

> ⚠️ **去掉 `xhigh` 的代价是 TUI 上的一个细节**：Codex 把 `max`（和 `ultra`）归到 **"Advanced Reasoning"** 子菜单里（带 `⚠ Consumes usage limits faster` 警告），所以普通思考菜单现在只显示 `low` / `high`，**选 `max` 要按快捷键进高级子菜单**。留着 `xhigh` 的话它本来能在普通菜单里直接选到。

简写档位方案：

| 简写 | 展开 |
|---|---|
| `private` | `low,high,max`（默认） |
| `three` | `low,high,max`（同上，别名） |
| `xhigh` | `low,high,xhigh,max`（严格后端的完整集合） |
| `none` | 空——**该模型没有思考菜单** |

也可以直接写逗号分隔的档位，或交互式选：

```bash
python3 private_api.py --configure-reasoning --reasoning-model glm-5.3-flash
python3 private_api.py --configure-reasoning --reasoning-model glm-5.3-flash \
    --reasoning-levels low,high,max --reasoning-default high
python3 private_api.py --reasoning-clear glm-5.3-flash   # 清单个
python3 private_api.py --reasoning-clear all             # 清全部
```

**档位现在写在 `model-config.jsonc` 里**（见上一节），`--configure-reasoning` 写的是旧的 `$CODEX_HOME/private-reasoning.json`——仍然读，但**配置文件的优先级更高**。日常直接改配置文件即可：

```jsonc
"glm-5.3-flash": { "reasoning_levels": ["low", "high", "max"], "default_reasoning_level": "high" }
```

旧 store 和 `vscode/` 那份**同名同格式**，两边互通；改完任一处都要跑一次 `--apply-model-config` 才会生效。

**OpenAI 自家模型（`gpt-*`/`o*`）的档位是只读的**，从 `$CODEX_HOME/models_cache.json` 原样抄——因为我们的目录会**替换**掉 OpenAI 那份，不抄一遍就等于把官方模型的能力覆盖没了。没登录过的机器没有这个缓存，退回保守的 `low/medium/high/xhigh`。

⚠️ **如果 `config.toml` 根上有 `model_reasoning_effort`，上面全都不生效。** 那是个全局默认值，**优先级高于每个模型的 `default_reasoning_level`**——菜单要么整份消失，要么所有模型都卡在同一档。脚本在写入目录时会**自动删掉这一行**并告诉你。

---

## 上下文窗口（context_window）

目录里的 `context_window` / `max_context_window` 决定 Codex **什么时候压缩历史**，而**不是**一个客户端硬上限。这一点容易搞反，所以在源码里核实过（`codex-rs/protocol/src/openai_models.rs` + `codex-rs/core/src/session/`）：

| 量 | 算法 | 默认 |
|---|---|---|
| 有效窗口 | `context_window`，缺省时退回 `max_context_window` | 本工具填网关的 `max_input_tokens` |
| 压缩触发线 | `auto_compact_token_limit()` = `min(配置值, 有效窗口 × 90%)` | 窗口的 **90%** |
| 硬上限线 | 有效窗口 × `effective_context_window_percent` | 窗口的 **95%** |

两条线任意一条被触到 → `token_limit_reached` → `run_auto_compact(..., CompactionReason::ContextLimit)`。**是压缩（roll over 到新上下文窗口），不是拒绝请求。** 客户端**不会**因为 `context_window` 而硬报错。

唯一会硬报错的是**后端**拒了这次请求——上游返回 `context_length_exceeded` 时，Codex 才抛 `ContextWindowExceeded`（`codex-api/src/sse/responses.rs` 解析该错误码）。

**所以 `context_window` 填高了比填低了危险：**

- **填低** → 压缩偏早，浪费一点窗口，但**安全**。
- **填高** → Codex 放心发出超长请求，由**网关** 400 掉，用户看到的是任务中途失败。

取值顺序：**`model-config.jsonc` 里你写的 `context_window`** → 网关 `GET /v1/models` 报的 `max_input_tokens` → seed 里的实测值 → **128K**（`DEFAULT_CONTEXT_WINDOW`，[`private-api/codex.py`](private-api/codex.py)，只在配置文件读不出来时兜底）。LiteLLM 只给它有元数据的模型填这个字段，实测本部署有四个模型没报、都落在 128K。

要让某个模型更贴合真实能力，两条路：**在网关侧补 `max_input_tokens` 元数据**（一次改好所有人），或者在 `model-config.jsonc` 里给那个模型写死 `context_window`（只影响你自己）。

---

## 能不能贴图（input_modalities）

Codex **从不问模型能不能看图**，它问目录，然后**在客户端就拒掉附件**。所以 `input_modalities` 填错：

- 填宽了 → 请求发出去，上游失败（或者更糟，模型对着图**胡说**）
- 填窄了 → 用户根本贴不进图

所以这张表是**测出来的，不是声明的**：发一张纯红、一张纯蓝的 PNG，看模型能不能分辨。

> **表是按网关实际 serve 的 slug 记的，改名要重测。** 这个坑踩过一次：严格那个后端最早以
> `deepseek-v4.1-flash-test` 的 slug 测出能看图，后来网关改成 serve `deepseek-v4.1-flash`，
> 表里还是旧 id → 新 id 落进 `DEFAULT_MODALITIES`（`("text",)`）→ **Codex 在客户端就拒掉贴图**，
> 提示 "This model does not support image inputs."。2026-09-17 用新 slug 重测，仍是 Red / Blue。
> 所以"上游改名"不是 find-and-replace，而是一次重测——`--probe-modalities` 会给出结论。

| 模型 | 结论 | 依据 |
|---|---|---|
| `deepseek-v4.1-flash` | **image** | 正确答出 Red / Blue |
| `glm-5.3-flash` | **image** | 正确答出 Red / Blue |
| `qwen3-5-397b` | **image** | 正确答出 Red / Blue |
| `deepseek-v4-flash` | text | **HTTP 400** "Model only supports text input; received unsupported content type 'image_url'"——上游直接拒，开了每轮都炸 |
| `xinghai-ultra` | text | **200，但没在看图**：一次答 "Black"/"Orange"，另一次直接拒答 "I am unable to determine"。**没有报错可察觉**，这正是要实测的原因 |

重新测量（后端换版本后建议跑一次）：

```bash
python3 private_api.py --probe-modalities
python3 private_api.py --probe-modalities --probe-models glm-5.3-flash   # 只测一个
```

**先让上面的表替你把能看图的都开好**——实测结论已经预填进 `model-config.jsonc`，所以正常情况下你不需要做任何事。要改的话：

```jsonc
// model-config.jsonc
"glm-5.3-flash": { "input_modalities": ["text", "image"] }
```

```bash
python3 private_api.py --apply-model-config
```

`--probe-modalities` 不改文件，它把**可以直接粘贴的片段**打出来给你抄（写回要重编码嵌套条目、会抹注释，收益不抵风险）：

```console
To enable, paste these entries into ...\codexcli\model-config.jsonc
(or edit the ones already there) and run: python3 private_api.py --apply-model-config

    "glm-5.3-flash": { "input_modalities": ["text", "image"] },
```

`--configure-modalities` 写的是旧的 `$CODEX_HOME/private-modalities.json`，仍然读、但**配置文件优先级更高**——留着是为了老装机不改行为。

**OpenAI 自家模型的 input types 是只读的**，从 `models_cache.json` 抄——这条以前出过事：目录里给所有模型硬编码 `["text"]`，把官方模型的贴图能力也一起没收了。所以 `model-config.jsonc` **刻意不放** `gpt-*`/`o*` 条目：目录是替换式的，我们写什么就盖掉 OpenAI 自己的定义。

---

## 搜索（web search）

### 为什么 Codex 自带的搜索用不了

Codex 的 web search 是**托管工具**：它只是把 `{"type":"web_search"}` 塞进 Responses API 的 `tools`
数组，指望**上游**去执行。网关对 `custom_openai` 自有模型会把这个工具直接丢掉——实测响应里
`tools` 是空的。**服务端没有开关可配**，这不是配置问题。

出路是把搜索放到**客户端侧**，做成一个模型能主动调用的工具，也就是 MCP。
Codex 是 MCP 客户端，它不关心背后是谁在执行搜索。

### ★ 你不用为搜索做任何额外的事

搜索配置是主流程的**最后一步**，自动完成，而且**复用的就是刚写进 `config.toml` 的那个
LiteLLM key**。所以仍然是那一条命令：

```bash
python3 private_api.py --api-base http://<网关>:4000 --api-key sk-你自己的
```

**没有第二个 key。** 搜索端点挂在网关后面（`<网关>:4000/searxng/mcp`），鉴权用的就是你自己的
LiteLLM virtual key——和模型调用是同一把。服务端有一个 `MCP_SEARXNG_TOKEN`，但那是**服务端内部
凭据**，只用于「网关 → searxng-mcp」那一跳，用户永远看不到。

放在最后一步是因为它要复用前面刚写的 key；拿不到 key 时它只打印一行提示、**不影响退出码**
（配模型才是主任务，不该被附加项拖失败）。`--no-search` 可以跳过。

### 它往 `config.toml` 写什么

```toml
[mcp_servers.searxng]
url = "http://<网关>:4000/searxng/mcp"
startup_timeout_sec = 20
tool_timeout_sec = 120

[mcp_servers.searxng.http_headers]
Authorization = "Bearer sk-<你自己的 LiteLLM key>"
```

两个刻意的选择，写错任何一个都会**静默失效**：

- **没有 `type` 字段。** Codex 从 `url` 还是 `command` 推断传输方式，这里没有 `type` 这一项。
  从 Claude Code 的配置（那边 `type` 是必需的）抄过来会出错。
- **token 走 `http_headers`，不用 `bearer_token_env_var`。** 后者把密钥留在环境里更干净，但它
  只在那个变量存在于 **Codex 继承到的环境**里时才有效。而三个客户端里有一个是 VSCode 的 Codex
  插件——由编辑器启动，它的环境我们控制不了。变量缺失不会有任何提示，请求会直接裸奔成 401，
  而用户从配置里看不出原因。字面 header 在所有启动方式下行为一致。
  这一点现在几乎没有代价：写进去的就是你自己那把 LiteLLM key，本来就在这个文件里。

`startup_timeout_sec` / `tool_timeout_sec` 特意调大了：冷启动的 SearXNG 搜索要扇出到所有启用的
引擎，`web_url_read` 还要经代理抓整页，默认值不够。

写盘同样是"文本手术"（`private-api/search.py` → `tomlpatch.py`），你已有的 `[mcp_servers.*]`
和注释都不会被动。

### 工具名会带 `searxng-` 前缀，这是正常的

网关暴露的是 `<服务名>-<工具名>`，所以模型看到的是 `searxng-web_url_read`、
`searxng-searxng_web_search`（后者双重，因为服务名和工具名本身都叫 searxng）。

**这个前缀改不掉。** 实测把 `tool_name_to_display_name` 设成完整的反向映射后，`tools/list`
返回的**仍然是**前缀名——那个字段只影响显示层。所以别去调它，看到前缀就当没看见。

### 命令

```bash
# 平时不需要单独跑：向导最后一步已经包含搜索
python3 private_api.py --api-base http://<网关>:4000 --api-key sk-xxx

# 以下是单独重配 / 排查用的
python3 private_api.py --configure-search
python3 private_api.py --configure-search --search-token sk-...
python3 private_api.py --check-search     # 握手 + 列出工具，401/403/404 会翻译成人话
python3 private_api.py --search-clear
python3 private_api.py --no-search         # 配模型但跳过搜索
```

token 的取值顺序是 `--search-token` → **`config.toml` 里的 LiteLLM key** →
`$MCP_SEARXNG_TOKEN`。环境变量**故意排在最后**：一个残留在 shell 里的旧 `MCP_SEARXNG_TOKEN`
不应该悄悄遮蔽掉正常路径。`--search-token` 保留是为了「后端直连、不走网关」这种排错场景。

配完**重启 Codex**（它在启动时读 `config.toml`），`/mcp` 里应该能看到 `searxng`，
模型随后拿到 `searxng-web_url_read` 等工具。

> **搜索服务端本身的部署与分层验证见 [`../searxng/README.md`](../searxng/README.md)。**
> 客户端配好了不代表服务端是好的——那边第 4 层（用**普通用户 key**走网关真握手）跑通之前，
> 不要认为这套东西是好的。

---

## 网关（LiteLLM）侧改动

**不改这里，Codex 对着私有模型每一轮请求都失败。** 两个根因，都必须在网关上解决——因为 Codex 二进制没法告诉它别发这两个字段。

**① `reasoning.effort` → HTTP 400**

Codex 每轮都带 `reasoning: {"effort": ...}`，LiteLLM 把它映射成 `reasoning_effort`，而 `custom_openai` 没声明这个参数，**代理在请求离开之前就拒了**：

```
litellm.UnsupportedParamsError: custom_openai does not support parameters:
['reasoning_effort'], for model=deepseek-v4-flash
```

**② `client_metadata` → HTTP 500**

Codex 每轮都挂一个 `client_metadata` 对象（session/turn id、沙箱模式、install id）。LiteLLM 1.100.1 **原样转发给 OpenAI SDK**，而 `create()` 没这个关键字参数：

```
Custom_openaiException - AsyncCompletions.create() got an unexpected
keyword argument 'client_metadata'
```

**修法**（两条都进模型定义的 `litellm_params`）：

```json
"allowed_openai_params": ["...", "reasoning_effort"],
"additional_drop_params": ["client_metadata"]
```

丢 `client_metadata` 没有任何代价——那是 Codex 自己的遥测信封，不是模型输入。

```bash
# 1) 先看 —— 只打印 curl，一个字节都不改网关
python3 private_api.py --emit-gateway-config

# 2) 确认无误再改。POST /model/update REPLACES litellm_params，
#    所以每条命令都会重发该模型的完整参数集 + 新增的那个 key。
#    需要 LiteLLM MASTER key（不是虚拟 key），且会影响网关上的所有用户
python3 private_api.py --apply-gateway-config
```

脚本**拒绝**发送缺 `api_base` 的请求体——那会把 `api_base` 和 `custom_llm_provider` 一起抹掉，把模型对所有人搞坏。

**当前状态（上次核对）：5 个私有模型全部已修好，无需再动。**

```
already accept Codex's Responses fields (5):
    deepseek-v4-flash  deepseek-v4.1-flash  glm-5.3-flash
    qwen3-5-397b       xinghai-ultra
not applicable (9): gpt-* / gpt-image-*  provider openai is out of scope
```

用 `--emit-gateway-config` 随时复验，输出 "nothing to do" 就是好的。

---

## 命令速查

```bash
# 交互式向导（推荐第一次用）
python3 private_api.py

# 一次配好
python3 private_api.py --api-base http://10.18.219.156:4000 --api-key sk-XXX
python3 private_api.py --api-base ... --api-key sk-XXX --model deepseek-v4.1-flash
python3 private_api.py --api-base ... --api-key sk-XXX --install-wrapper   # 顺带装自动刷新

# 只看不动
python3 private_api.py --detect        # 找到哪些文件
python3 private_api.py --status        # 当前配置状态（先跑这个）
python3 private_api.py --list-models   # 网关到底提供哪些模型

# 模型
python3 private_api.py --switch-model            # 交互式挑
python3 private_api.py --switch-model --model gpt-5.6-sol
./bin/codex-model                                # 上一条的快捷方式
python3 private_api.py --refresh-models          # 刷新目录（并追加新模型到 model-config.jsonc）
python3 private_api.py --sync --force            # 手动跑一次"启动时刷新"
python3 private_api.py --apply-model-config      # 改完 model-config.jsonc 后，同步进真实环境

# 登录
python3 private_api.py --fix-login               # 写 auth.json，不再弹登录
python3 private_api.py --fix-login --force

# 模型能力（能不能贴图 / 几档思考 / 上下文多长）—— 直接改 model-config.jsonc
# 大部分情况不用动：实测结论已预填，网关新模型会自动追加进来
$EDITOR model-config.jsonc
python3 private_api.py --apply-model-config      # 改完同步进真实环境

# 旧的按模型覆盖（仍读取，但 model-config.jsonc 优先级更高）
python3 private_api.py --configure-reasoning --reasoning-model <id> --reasoning-levels private
python3 private_api.py --configure-reasoning --reasoning-model <id> --reasoning-levels xhigh
python3 private_api.py --reasoning-clear all

# 能否贴图：测量不改文件，给结论 + 可粘贴片段
python3 private_api.py --probe-modalities
python3 private_api.py --configure-modalities --modalities-model <id> --modalities text,image

# 搜索（让模型能搜网页）。平时不用单独跑：向导最后一步已包含，用的是同一个 LiteLLM key
python3 private_api.py --configure-search  # 只重配搜索
python3 private_api.py --check-search      # 握手 + 列工具；连不上会说明是哪一层的问题
python3 private_api.py --search-clear
python3 private_api.py --no-search         # 配模型但跳过搜索

# 网关侧参数（400 + 500 的两个根因）
python3 private_api.py --emit-gateway-config      # 只打印
python3 private_api.py --apply-gateway-config     # 真发送，需要 master key

# 包装脚本
python3 private_api.py --install-wrapper
python3 private_api.py --uninstall-wrapper

# 还原
python3 private_api.py --restore
```

---

## 常见问题

**Q：`codex` 里 `/model` 还是显示 GPT-5.6 Sol/Luna/Terra，没有网关的模型？**
`model_catalog_json` 没写进去，或者文件路径不对。先 `--status` 看 `model catalog` 那一段。另外根上的 `model_reasoning_effort` 会让档位显示不对（见[思考档位](#思考档位reasoning)），但**不影响模型列表本身**。

**Q：`codex` 打开就要登录 / `codex login status` 说 Not logged in？**
`auth.json` 不在。跑 `--fix-login`。provider 表里写了什么都不管用——Codex 就是看这个文件在不在。

**Q：`codex` 直接起不来，报 `failed to parse model_catalog_json ... missing field \`x\``？**
目录格式和你的 codex 版本对不上。**先看 `--status` 里那一行版本提示**——低于 0.143 就需要升级 codex；高于 0.154.0（本工具适配的版本）就要重新适配。0.143~0.154 之间的改名问题本工具已经用"两种写法都发"覆盖了，如果你用的是本工具生成的目录还报这个错，说明是**别的字段**：serde 一次只报一个，照着报错字段名逐个补。完整字段集见[版本兼容](#版本兼容codex-的版本敏感字段)。

**Q：第一句话就 400，错误里有 `reasoning.summary` 或 `invalid type: map, expected a string`？**
网关不吃 `reasoning.summary`。目录里的 `supports_reasoning_summaries` / `supports_reasoning_summary_parameter` 必须都是 `false`（本工具默认如此）。如果你手改过目录，或者用的旧版工具生成的目录，重新 `--refresh-models` 生成一份。

**Q：第一句话就 400？**
网关侧 `allowed_openai_params` 缺 `reasoning_effort`。跑 `--emit-gateway-config` 看，然后按提示修。

**Q：第一句话就 500？**
网关侧 `additional_drop_params` 缺 `client_metadata`。同上。

**Q：提示 `` `wire_api = "chat"` is no longer supported ``？**
Codex 0.150 删掉了 chat 线协议。网关必须能代理 `/v1/responses`。LiteLLM 可以；如果你们的网关只能聊 `/v1/chat/completions`，那 Codex 驱动不了它。脚本在写配置前会先探这个端点。

**Q：报找不到 `env_key`？**
你用了 `--use-env-key`，但当前 shell 是非交互的（`docker exec`、cron、systemd），不 source `~/.bashrc`。**去掉 `--use-env-key` 用默认的内联方式**，这是服务器上推荐的做法。

**Q：包装脚本装了但目录不自动更新？**
`~/.local/bin` 不在 `PATH` 里，或者排在真实 codex 后面。`--status` 的 `codex wrapper` 段会告诉你 `on PATH: yes/NO`。

**Q：某个模型贴不进图？**
看[实测表](#能不能贴图input_modalities)。`deepseek-v4-flash` 和 `xinghai-ultra` 是故意关掉的——前者上游直接 400，后者会对着图编答案。

**Q：模型不会搜索 / 说它不能联网？**
Codex 自带的 web search 在私有网关上**不可能**工作（托管工具，网关会丢掉，见[搜索](#搜索web-search)）。正常跑一次向导就会接上 MCP 搜索。已经配过但还是不行，用 `--check-search` 看是哪一层——它会区分「连不上」「401 key 不对」「404 没注册」这几种。

**Q：`--check-search` 报 404 `MCP server ... not found`？**
网关起来了但不知道这个 MCP server。在**网关那台机器**上跑 `python3 searxng/register_mcp.py`。这是服务器一次性动作，客户端无能为力。

**Q：`--check-search` 报 401？**
客户端用的是**你自己的 LiteLLM key**，所以基本只有一个原因：搜索那步跑在向导之前，或者用了不同的 `--api-key`。**不存在第二个搜索 key**——服务端的 `MCP_SEARXNG_TOKEN` 客户端根本不碰。

**Q：服务器上 `register_mcp.py --check` 报 `unhealthy`，但 searxng-mcp 的 `/health` 是绿的？**
`MCP_HTTP_ALLOWED_HOSTS` 里没有 `searxng-mcp:8090`。它默认只允许回环地址，而且拿它跟请求的 `Host` 头**连端口精确比对**——网关以容器身份来连，`Host` 就是服务名那个值。用**服务名**而不是 IP，换机器就不用改。详见 [`../searxng/README.md`](../searxng/README.md)。

**Q：`/mcp` 里看不到 `searxng`？**
Codex 只在**启动时**读 `config.toml`，改完要重启。仍然看不到就 `--status` 看 `search over MCP` 那一段有没有写进去。

**Q：配置写坏了想回滚？**
`python3 private_api.py --restore`。它把 `config.toml.bak`、`auth.json.bak` 放回去，删掉生成的目录文件，卸掉包装脚本。**per-model 的 reasoning/modalities 覆盖会保留**，要清用 `--reasoning-clear all` / `--modalities-clear all`。

---

## 目录结构

```
codexcli/
├── README.md              # 本文档
├── 设计需求.md
├── private_api.py         # 入口脚本，所有命令都从这里走（唯一直接执行的）
├── model-config.jsonc     # 模型能力配置（首次运行生成，之后归你改；不在 zip 里）
├── pack.py                # 打包成 codexcli.zip（只有维护者用，见下）
├── bin/
│   └── codex-model        # 切模型的快捷方式（跑向导时生成，可删）
└── private-api/           # 模块，不会被直接调用
    ├── codex.py           # config.toml 读写 + 模型目录生成 + 包装脚本
    ├── codex_auth.py      # auth.json（免登录）
    ├── detect.py          # 找 $CODEX_HOME、config、auth、models_cache、codex 二进制
    ├── gateway.py         # 调网关：列模型、探 /v1/responses
    ├── jsonc.py           # 保留注释的 JSONC 读写器
    ├── modelconfig.py     # model-config.jsonc 的 schema / 读写 / seed 合并 / 校验
    ├── model-config.seed.jsonc  # 随包发的实测知识库（数据，不是模块）
    ├── reasoning.py       # 每个模型的思考档位
    ├── modalities.py      # 每个模型的输入类型（能否贴图）+ 实测探针
    ├── search.py          # web search（MCP 客户端）
    ├── litellm_admin.py   # 网关侧 litellm_params 修复
    └── tomlpatch.py       # 保留原格式的 TOML 行编辑
```

`model-config.jsonc` **不在打包清单里**：它是运行期生成的，重新解压不该覆盖你的编辑。`model-config.seed.jsonc` 在里面，因为它是随包发的知识库。

`vscode/` 那份是同一套思路在编辑器插件上的实现（Claude Code + Codex 插件）。两份共用 `private-reasoning.json` / `private-modalities.json`，同一台机器都配过也没问题。
`private-api/` 下的 `search.py`、`jsonc.py`、`modelconfig.py`、`model-config.seed.jsonc` 和 `vscode/` 对应文件是**逐字节相同**的拷贝，改一份就要同步另一份（`md5sum` 对一下即可）。**注意 `modelconfig.py` 里的 `TOOLKIT_ROOT` 是按文件位置算的**，所以两份不需要改任何常量。

### 打包（维护者）

用户拿到的是 `codexcli.zip`，不是这个目录。**改完 `codexcli/` 里的任何东西都要重打一次** —— 忘了重打，用户拿到的就是旧的（`search.py` 曾经就这么漏过一次，用旧 zip 的人完全没有搜索功能）。

```bash
python codexcli/pack.py            # 输出仓库根目录的 codexcli.zip
python codexcli/pack.py --out /tmp/codexcli.zip
```

脚本用**白名单**列举打包内容（不是 `zip -r .`），并且会拿 `EXPECTED_MODULES`（模块）和 `EXPECTED_DATA`（数据文件，如 `model-config.seed.jsonc`）核对 `private-api/` 里的每一样东西 —— 新增了却忘了加进去会**报错退出**，而不是默默打个残包。顺带排除 `__pycache__/*.pyc` 和内部的 `设计需求.md`。
