"""pytest 公共夹具：把实体表载入 topics。

## 为什么需要

实体同义词表已经从代码搬到**数据文件**（`index/entities.json`），
换台机器内容完全不同。测试要跟真实数据对齐——否则 `demo-wiki → 示例知识库`
这类断言会因为"代码里没有别名表"而失败（那是预期的，因为别名属于数据）。

## 全新克隆怎么办（2026-11-20 修）

`index/` 是 gitignore 的，所以**克隆出来就没有 entities.json**，依赖别名的测试全红。
现在退化为 `tests/fixtures/entities.min.json`（测试专用最小表，进仓）：
本机有真实表时优先用真实表，没有就用它。这样 `git clone && pytest` 能直接跑绿。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from topics import load_entities  # noqa: E402

LOCAL_ENTITIES = os.path.join(ROOT, "index", "entities.json")
FIXTURE_ENTITIES = os.path.join(ROOT, "tests", "fixtures", "entities.min.json")


def entities_path() -> str:
    """优先本机真实实体表；没有（全新克隆 / CI）就用仓库内的最小夹具。"""
    return LOCAL_ENTITIES if os.path.isfile(LOCAL_ENTITIES) else FIXTURE_ENTITIES


def _safe_print(zh: str, en: str) -> None:
    """打印中文，控制台编码不支持时退回英文。

    2026-11-21 CI 实测：GitHub 的 Windows runner 默认 stdout 是 **cp1252**，
    `print("载入实体表…")` 直接抛 UnicodeEncodeError，而这行在 `pytest_configure`
    里 —— 等于**整轮 INTERNALERROR**、一条测试都没跑。本机控制台是 UTF-8，看不到。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    try:
        print(zh)
    except UnicodeEncodeError:
        print(en)


def pytest_configure(config):
    path = entities_path()
    n = load_entities(path)
    _safe_print("\n[conftest] 载入实体表: %d 个（%s）" % (n, path),
                "\n[conftest] entities loaded: %d (%s)" % (n, path))


@pytest.fixture(scope="session")
def repo_root():
    return ROOT
