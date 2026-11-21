"""打包闸门：包内文件不许出现"装包后不存在"的**行动型**引用。

## 为什么（2026-11-20）

`SKILL.md` 曾写着 `python tools/research/gh_scan.py …` 和 `docs/2026-11-19_竞品情报快扫流程.md`，
但 `tools/` 与 `docs/` **都不进包**（平台只允许根 + 一层目录）。于是装包用户照着做会
「文件不存在」——文档承诺了、包里没有。这类错以前靠人眼，现在机械检。

## 判据（glm-5.3 复核第 6 条）

**装包用户会不会照这条动手？**
  · 行动型（照抄执行、按路径打开、`--out` 之类参数）⇒ 必须指向包内**真实存在**的文件；
  · 出处型（"源自 tools/xxx"、"详见仓库 docs/"这类溯源叙述）⇒ 可留，但路径不许当命令用。

所以本检查对 stage 里的文本文件，找出：
  1. `tools/` `tools\\` 路径引用；
  2. `docs/` `docs\\` 路径引用；
  3. 本机绝对路径 `F:\\`（换机器必失效）；
  4. 反引号里出现 `.ps1/.py` 但该文件**不在 stage**（例如引用了没进包的脚本）。

白名单：文件里出现的字符串若命中 `ALLOW` 列表（按行匹配）则跳过 —— 用于经过复核的出处型叙述。

用法（由 pack_platform.ps1 调用）：`python tools/check_pack_refs.py`
退出码：0 = 干净；1 = 有死引用；2 = stage 不存在
"""
from __future__ import annotations

import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAGE = os.path.join(REPO, "dist", "stage")
#: 只扫**用户会照着做的文档**（SKILL.md / references / templates）。
#: 源码（.py）里的路径出现在注释/docstring 中，是给维护者看的，不是给装包用户执行的 ——
#: 第一版把 .py 也扫了，结果 premise.py/procedures.py 的示例路径全被误报（68 条噪音）。
SCAN_EXT = {".md"}

#: 豁免：变更日志是 **append-only 历史**。它写的是"当年怎么做的"（出处型叙述），
#: 里面有当年的 `tools/…`、`tests/…`、`docs/…` 路径 —— 改写历史比留着更糟：
#: 后人要靠它判断"哪一版改了什么"。真正的操作指引在 SKILL.md / references 其它文件里。
SKIP_FILES = {"changelog.md"}

#: 行内出现这些就跳过（出处型叙述 / 维护者脚本，已人工确认不构成装包用户的操作指引）
ALLOW = (
    "源自", "溯源于", "仓库内", "repo docs", "开发版",
    "tools\\\\check_", "tools/check_",
    "tools\\\\research\\\\", "tools/research/",
    "install.ps1", "pack_platform.ps1", "git checkout",   # 维护者/源码安装路径
    "复核记录", "改动记录",
    "tools/list", "tools/call", "tools/listChanged",   # MCP 协议方法名，不是路径
)

PATH_RE = re.compile(r"(tools[\\/]|docs[\\/]|[A-Za-z]:\\\\)")
#: 反引号里的文件引用：只取不含空白的路径 token（原来会把 `python mcp_server.py` 整串当路径）
BACKTICK_FILE = re.compile(r"`([A-Za-z0-9_\-./\\]+\.(?:py|ps1))`")

#: 模板是"填空示例"，里面的文件名是占位，不是引用
SKIP_DIRS = {"templates"}


def _stage_files():
    for base, dirs, files in os.walk(STAGE):
        if os.path.basename(base).lower() in SKIP_DIRS:
            continue
        for name in files:
            if os.path.splitext(name)[1].lower() not in SCAN_EXT:
                continue
            if name.lower() in SKIP_FILES:
                continue
            yield base, name


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if not os.path.isdir(STAGE):
        print("check_pack_refs: stage 不存在：%s" % STAGE, file=sys.stderr)
        return 2

    stage_names = set()
    for base, _dirs, files in os.walk(STAGE):
        for name in files:
            stage_names.add(name.lower())

    hits = []
    for base, name in _stage_files():
        path = os.path.join(base, name)
        rel = os.path.relpath(path, STAGE).replace("\\", "/")
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError as exc:
            hits.append((rel, 0, "读不了：%s" % exc))
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if any(a in line for a in ALLOW):
                continue
            m = PATH_RE.search(line)
            if m:
                hits.append((rel, n, "引用了不进包的路径：%s" % m.group(1)))
                continue
            for ref in BACKTICK_FILE.findall(line):
                leaf = os.path.basename(ref.replace("\\", "/")).lower()
                if leaf and leaf not in stage_names:
                    hits.append((rel, n, "反引号引用了包内没有的脚本：%s" % ref))

    print("check_pack_refs: 扫 %d 个文本文件 ｜ 死引用 %d" % (len(stage_names), len(hits)))
    for rel, n, why in hits[:40]:
        print("  HIT  %-32s 第 %-4d 行  %s" % (rel, n, why))
    if len(hits) > 40:
        print("  …… 其余 %d 条" % (len(hits) - 40))
    if hits:
        print("\nPACK FAIL: 包内有指向包外的行动型引用（装包用户会照做失败）。"
              "改成包内相对路径，或把资源一起打进包。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
