# -*- coding: utf-8 -*-
"""测试桩：固定输出一张结论卡，避免契约回归测试依赖真实数据库内容。"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

print("### 相关结论（agent-memory 卡片；测试桩）")
print("- [成立] 注入消息必须带 source.kind（machine；证据 1 条）")
print("--- 统计: 卡片：候选 1 ｜ 注入 1 ｜ 截断 0 ｜ 40 字符")
