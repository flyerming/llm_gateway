#!/usr/bin/env python3
"""把 codexcli/ 打成可以直接发给用户的 codexcli.zip。

用法（在仓库根目录或 codexcli/ 里都行）：
    python codexcli/pack.py
    python codexcli/pack.py --out /tmp/codexcli.zip

只用标准库。**每次改完 codexcli/ 里的东西都要重跑一次** —— 用户拿到的是 zip，
不是这个目录；忘了重跑，用户拿到的就是旧的。

打包清单是**显式列举**的，不是 `zip -r .`:黑名单式排除会随目录里新增文件而漏
（这个 zip 上一次就是这么把 `__pycache__/*.pyc` 打进去的），白名单式漏掉的是
「本该有却没有」,跑一次 README 里的自检就能看出来。

所以下面有一份 `EXPECTED_MODULES` —— 打包结束后核对 zip 里 .py 的数量，
对不上就报错退出。这防的是**静默漏文件**（比如新加一个 private-api/xxx.py
忘了加进来），那种错误用户装上才会发现,而且表现为「功能莫名没有」。
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

# private-api/ 下必须进包的模块。新增模块时**必须**同步加到这里 —— 加漏了脚本
# 不会自己发现，但 `verify()` 会在打包末尾把差异喊出来。
EXPECTED_MODULES = (
    "codex.py",
    "codex_auth.py",
    "detect.py",
    "gateway.py",
    "jsonc.py",  # 保留注释的 JSONC 读写器。原来只有 vscode 带，现在两边都要。
    "litellm_admin.py",
    "modalities.py",
    "modelconfig.py",  # 模型能力配置的读写与解析（model-config.jsonc）
    "reasoning.py",
    "search.py",  # web search（MCP 客户端）。曾经漏过 —— 用旧 zip 的人没有搜索功能。
    "tomlpatch.py",
)

# private-api/ 下的**数据**文件（不是模块，但同样必须进包）。分开列是为了让
# verify() 的报错说得准：模块漏了是「功能莫名没有」，数据漏了是「生成的配置少了
# 实测结论」—— 后者更隐蔽，因为功能看起来是好的，只是值退回了保守默认。
EXPECTED_DATA = (
    "model-config.seed.jsonc",
)

# 不进包的东西。设计文档是内部用的；__pycache__ 是字节码，跨 Python 版本没用还占地方。
# 注意 `model-config.jsonc`（用户那份）**不在** private-api/ 下，本来就不会被收进来 ——
# 它是运行期生成的，重新解压不该覆盖用户的编辑。
SKIP_NAMES = {"__pycache__", "设计需求.md"}


def collect(root: Path) -> list[Path]:
    """返回要进包的文件（相对 root），已排序。"""
    files: list[Path] = []

    def add(p: Path) -> None:
        if p.is_file() and p.name not in SKIP_NAMES and p.suffix != ".pyc":
            files.append(p.relative_to(root))

    add(root / "private_api.py")
    add(root / "README.md")
    add(root / "bin" / "codex-model")
    # 安装脚本带版本号（codex-cli-install-0.154.0.sh），用 glob 免得升版本时漏改。
    for pat in ("codex-cli-install-*.sh", "codex-cli-install-*.ps1"):
        files.extend(
            p.relative_to(root) for p in sorted(root.glob(pat)) if p.is_file()
        )
    for p in sorted((root / "private-api").iterdir()):
        add(p)

    return sorted(files)


def verify(files: list[Path]) -> list[str]:
    """核对打包清单是否和 EXPECTED_MODULES / EXPECTED_DATA 一致。空表示没问题。"""
    problems = []
    # files 里是相对 codexcli/ 的路径，所以 private-api/x.py 的 parts[0] == "private-api"
    packed_api = {f.name for f in files if f.parts and f.parts[0] == "private-api"}
    known = set(EXPECTED_MODULES) | set(EXPECTED_DATA)

    for name in EXPECTED_MODULES:
        if name not in packed_api:
            problems.append(f"缺少模块 private-api/{name}")
    for name in EXPECTED_DATA:
        if name not in packed_api:
            problems.append(f"缺少数据文件 private-api/{name}")
    for name in sorted(packed_api - known):
        problems.append(
            f"private-api/{name} 在包里但不在清单里 —— "
            f"是模块请加进 EXPECTED_MODULES，是数据文件请加进 EXPECTED_DATA，"
            f"是误入请加进 SKIP_NAMES"
        )
    return problems


def main() -> int:
    # Windows 的默认控制台编码是 GBK，中文输出会变成乱码（哪怕这边只是打包脚本，
    # 报错信息读不了就白报了）。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    ap = argparse.ArgumentParser(description="打包 codexcli.zip")
    ap.add_argument("--out", default=None, help="输出 zip 路径（默认 <仓库根>/codexcli.zip）")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent
    repo = root.parent
    out = Path(args.out) if args.out else repo / "codexcli.zip"

    files = collect(root)
    problems = verify(files)
    if problems:
        print("打包清单核对失败：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    # 用 zip 内路径 codexcli/xxx，用户解压出来就是一个 codexcli/ 目录。
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(root / f, f"codexcli/{f.as_posix()}")

    print(f"已写出 {out}")
    packed_api = [f for f in files if f.parts and f.parts[0] == "private-api"]
    print(f"共 {len(files)} 个文件，其中 private-api/ {len(packed_api)} 个"
          f"（{len(EXPECTED_MODULES)} 个模块 + {len(EXPECTED_DATA)} 个数据文件）：")
    for f in files:
        print(f"  codexcli/{f.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
