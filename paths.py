"""路径解析：**唯一**决定"工程在哪、数据在哪"的地方。

## 为什么单独一个文件

换台电脑时唯一会变的就是位置。原来 `F:\\agent-memory` 和 `f:\\demo-base` 散落在
技能文档、脚本、代码里（实测 17 处 + 9 处 venv + 若干实体名），换机全废。
现在集中在这里：

    HOME（工程目录）      $env:AGENT_MEMORY_HOME > 本文件所在目录
    DATA（本机数据目录）   <HOME>/index            —— 索引库、实体词典、targets，全部 gitignore
    ENTITIES_FILE         <DATA>/entities.json      —— 实体同义词表（每台机自己学/自己写）
    TARGETS_FILE          <DATA>/targets.json       —— 注入块要写到哪些项目

代码里**不允许**再出现具体项目名或盘符；只有本文件可以谈路径。
"""
from __future__ import annotations

import os

#: 工程根目录。环境变量优先，否则取本文件所在目录（即项目根）。
HOME = os.environ.get("AGENT_MEMORY_HOME") or os.path.dirname(os.path.abspath(__file__))

#: 本机数据目录（可随时删除重建；不进版本库）
DATA = os.path.join(HOME, "index")

DB_FILE = os.path.join(DATA, "sessions.db")
ENTITIES_FILE = os.path.join(DATA, "entities.json")
TARGETS_FILE = os.path.join(DATA, "targets.json")
#: 结论卡（编译层）：JSONL，一行一张卡。**独立于任何项目目录**，换机复制即迁移。
CARDS_FILE = os.path.join(DATA, "cards.jsonl")
#: docs 层的检索根（`--docs-first` 用）。本机数据：没有这份配置就**静默关闭**这一层。
DOCS_ROOTS_FILE = os.path.join(DATA, "docs-roots.json")
#: 卡片写入审计日志（NDJSON，append-only）：谁在何时改了什么、改前/改后哈希。
#: 为什么需要：巡检（cards audit --apply）与多个 agent 都会改卡片，没有它追不到"谁改的"。
CARDS_AUDIT_FILE = os.path.join(DATA, "cards-audit.ndjson")
#: 人工确认账本（JSONL）：哪张卡、在什么内容指纹上、被谁确认过。
#: 为什么**独立文件**而不是卡片字段：确认是"阅读状态"，卡片是"内容"——
#: 内容会被 agent 重写，阅读状态不该跟着丢；且卡片导出/迁移可只带走内容。
CARDS_ACK_FILE = os.path.join(DATA, "cards-ack.jsonl")

#: Python 解释器（venv 优先，回退系统 python）
def python() -> str:
    exe = os.path.join(HOME, ".venv", "Scripts", "python.exe")
    if os.path.exists(exe):
        return exe
    exe = os.path.join(HOME, ".venv", "bin", "python")     # 非 Windows
    return exe if os.path.exists(exe) else "python"


def ensure_data_dir() -> str:
    os.makedirs(DATA, exist_ok=True)
    return DATA
