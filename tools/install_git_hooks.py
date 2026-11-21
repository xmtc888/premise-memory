"""安装/卸载 git pre-commit 钩子：提交前强制跑 `.ps1` 纯 ASCII 检查。

## 为什么用钩子（而不是"记得自查"）

2026-11-17 一天内三次踩"非 ASCII 进脚本"的坑（refresh.ps1 中文注释 195 字节、
早期 probe 脚本中文路径、内联 python 转义）。**靠自觉记不住，就让它变成提交前闸门。**

## 行为

- 写 `.git/hooks/pre-commit`（POSIX sh，Git for Windows 能执行）；
- 只检查**本次暂存**的 `.ps1/.cmd/.bat`（不扫全仓，避免误伤历史文件）；
- 已有非本工具写的钩子：备份为 `pre-commit.bak-<时间戳>`，不覆盖丢弃；
- `--uninstall` 移除（若备份存在则还原）。

用法：
    python tools/install_git_hooks.py            # 安装
    python tools/install_git_hooks.py --uninstall
"""
from __future__ import annotations

import os
import stat
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(REPO, ".git", "hooks", "pre-commit")
MARK = "# agent-memory: ascii-ps1-guard"

HOOK_BODY = """#!/bin/sh
{mark}
# Block commits that add non-ASCII to .ps1/.cmd/.bat.
# WHY: PowerShell 5.1 decodes BOM-less .ps1 with the ANSI codepage (GBK on zh-CN),
# so a single CJK character breaks parsing and the error points at the wrong place.
# Rule: scripts are ASCII-only; Chinese belongs in .md/.json (read as UTF-8).
root=$(git rev-parse --show-toplevel) || exit 0
py="$root/.venv/Scripts/python.exe"
[ -x "$py" ] || py="$root/.venv/bin/python"
[ -x "$py" ] || py=python
exec "$py" "$root/tools/check_ascii_ps1.py" --staged
""".format(mark=MARK)


def hook_path() -> str:
    return HOOK


def install() -> int:
    hooks_dir = os.path.dirname(HOOK)
    if not os.path.isdir(hooks_dir):
        print("不是 git 仓库（缺 %s）" % hooks_dir, file=sys.stderr)
        return 2
    if os.path.exists(HOOK):
        old = open(HOOK, encoding="utf-8", errors="replace").read()
        if MARK in old:
            print("钩子已是最新（含 %s），重写一遍幂等处理" % MARK)
        else:
            bak = HOOK + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
            os.replace(HOOK, bak)
            print("已有非本工具的钩子，已备份 → %s" % os.path.basename(bak))
    with open(HOOK, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(HOOK_BODY)
    # Git for Windows 需要可执行位；在 Windows 上这一位基本是摆设，但保留惯例
    try:
        os.chmod(HOOK, os.stat(HOOK).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass
    print("已安装 pre-commit 钩子：%s" % HOOK)
    print("  只检查暂存的 .ps1/.cmd/.bat；卸载：python tools/install_git_hooks.py --uninstall")
    return 0


def uninstall() -> int:
    if not os.path.exists(HOOK):
        print("没有钩子，无需卸载")
        return 0
    old = open(HOOK, encoding="utf-8", errors="replace").read()
    if MARK not in old:
        print("该钩子不是本工具安装的，不动它（%s）" % HOOK, file=sys.stderr)
        return 1
    os.remove(HOOK)
    baks = sorted(f for f in os.listdir(os.path.dirname(HOOK))
                  if f.startswith("pre-commit.bak-"))
    if baks:
        newest = os.path.join(os.path.dirname(HOOK), baks[-1])
        os.replace(newest, HOOK)
        print("已卸载，并还原备份：%s" % os.path.basename(newest))
    else:
        print("已卸载（无备份可还原）")
    return 0


def main() -> int:
    if "--uninstall" in sys.argv:
        return uninstall()
    return install()


if __name__ == "__main__":
    sys.exit(main())
