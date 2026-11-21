# -*- coding: utf-8 -*-
"""打包闸门：**stage 里的文本文件不许含控制字符**（BEL/BS/NUL/FF），`.md` 里连 TAB 也不许。

## 为什么

2026-11-08 的 changelog 事故：用 PowerShell 双引号 here-string 写含反引号的中文，
`` `t `` 变成 TAB、`` `a `` 变成 BEL、`` `$ `` 变成字面 `$`，反引号 + 汉字被吞成单字符。
结果**包里随附的 changelog 已经不是中文了**，而 private-terms / 版本闸门都发现不了
（它们只管隐私和"同号不同内容"）。这类损坏必须单独机器检 —— 写成明文门槛。

判据：
  · 任何被扫文件里出现 BEL(0x07) / BS(0x08) / NUL(0x00) / FF(0x0c) → 失败；
  · `.md` 里出现 TAB → 失败（markdown 不需要 TAB；它几乎一定是转义事故的残留）；
  · `.py` 里的 TAB 合法（缩进），不检。

用法（由 pack_platform.ps1 调用）：
    python tools/check_pack_text.py
退出码：0 = 干净；1 = 有损坏（逐条打印 文件:行号）；2 = stage 不存在
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAGE = os.path.join(REPO, "dist", "stage")
TEXT_EXT = {".md", ".txt", ".ps1", ".py", ".json", ".yml", ".yaml"}
CTRL = {"\x07": "BEL", "\x08": "BS", "\x00": "NUL", "\x0c": "FF"}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if not os.path.isdir(STAGE):
        print("check_pack_text: stage 不存在：%s" % STAGE, file=sys.stderr)
        return 2

    hits: list[tuple[str, int, str]] = []
    scanned = 0
    for base, _dirs, files in os.walk(STAGE):
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext not in TEXT_EXT:
                continue
            path = os.path.join(base, name)
            rel = os.path.relpath(path, STAGE).replace("\\", "/")
            scanned += 1
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for n, line in enumerate(fh, 1):
                        for ch, label in CTRL.items():
                            if ch in line:
                                hits.append((rel, n, label))
                        if ext == ".md" and "\t" in line:
                            hits.append((rel, n, "TAB"))
            except OSError as exc:
                hits.append((rel, 0, "读不了：%s" % exc))

    print("check_pack_text: 扫 %d 个文本文件 ｜ 控制字符/TAB 命中 %d" % (scanned, len(hits)))
    for rel, n, label in hits[:40]:
        print("  HIT  %-34s 第 %-4d 行  %s" % (rel, n, label))
    if len(hits) > 40:
        print("  …… 其余 %d 条" % (len(hits) - 40))
    if hits:
        print("\nPACK FAIL: 包内文本含控制字符。多半是用 PowerShell here-string 写的内容 —— "
              "带反引号/中文一律改用 Python 写文件，然后重新打包。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
