# -*- coding: utf-8 -*-
"""打包闸门：**stage 里每个本地 import 的模块都必须真的在包里**。

## 为什么必须机器检

`pack_platform.ps1` 的 `$engFiles` 是一张**手写白名单**。2026-11-07 漏了 `aliases.py`、
2026-11-08 漏了 `cards.py` —— 同一个错误犯两次，症状都是「PACK OK，但装出来的包一用就 ImportError」。
白名单靠人记，就一定会再漏；所以把「漏没漏」变成打包时的一条断言。

判据：扫描 stage 里的每个 `.py`，用 AST 取 `import X` / `from X import …`（只取顶层包名），
若 `X` 是**本项目的模块**（仓库根或 collectors/ 下有同名 `.py`）而 stage 里没有它 → 失败。

用法（由 pack_platform.ps1 调用，也可单独跑）：
    python tools/check_pack_manifest.py
退出码：0 = 齐；1 = 缺模块（逐条打印）；2 = stage 不存在
"""
from __future__ import annotations

import ast
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAGE = os.path.join(REPO, "dist", "stage")


def local_modules() -> set[str]:
    """本项目自己的模块名（仓库根 + collectors/ 下的 .py，去掉 __init__）。"""
    names = set()
    for base in (REPO, os.path.join(REPO, "collectors")):
        if not os.path.isdir(base):
            continue
        for f in os.listdir(base):
            if f.endswith(".py") and f != "__init__.py":
                names.add(f[:-3])
            elif os.path.isdir(os.path.join(base, f)) and os.path.exists(
                    os.path.join(base, f, "__init__.py")):
                names.add(f)                       # 包（如 collectors）
    return names


def staged_files() -> list[str]:
    out = []
    for base, _dirs, files in os.walk(STAGE):
        for f in files:
            if f.endswith(".py"):
                out.append(os.path.join(base, f))
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if not os.path.isdir(STAGE):
        print("check_pack_manifest: stage 不存在：%s" % STAGE, file=sys.stderr)
        return 2

    local = local_modules()
    stage_py = staged_files()
    #: stage 里已存在的东西（按相对路径，用于判断包/模块是否在包内）
    present = set()
    for base, _dirs, files in os.walk(STAGE):
        for f in files:
            rel = os.path.relpath(os.path.join(base, f), STAGE).replace("\\", "/")
            present.add(rel)
    present_mods = {p[:-3].replace("/", ".") for p in present if p.endswith(".py")}
    present_mods |= {p for p in present if p.endswith("/__init__.py")}
    present_mods |= {p.split("/__init__.py")[0] for p in present if p.endswith("/__init__.py")}

    missing: dict[str, set[str]] = {}
    scanned = 0
    for path in stage_py:
        scanned += 1
        rel = os.path.relpath(path, STAGE).replace("\\", "/")
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError as exc:
            print("check_pack_manifest: %s 语法错误：%s" % (rel, exc), file=sys.stderr)
            return 1
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]] if node.level == 0 else []
            else:
                continue
            for name in names:
                if not name or name not in local:
                    continue
                if name not in present_mods:
                    missing.setdefault(name, set()).add(rel)

    print("check_pack_manifest: 扫 %d 个 py ｜ 本地模块 %d 个 ｜ 缺模块 %d 个"
          % (scanned, len(local), len(missing)))
    for name, users in sorted(missing.items()):
        print("  MISS  %-16s 被这些文件 import：%s" % (name, "、".join(sorted(users)[:4])))
    if missing:
        print("\nPACK FAIL: 包内缺模块（装上后会在 import 处崩）。把它们加进 "
              "pack_platform.ps1 的 $engFiles。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
