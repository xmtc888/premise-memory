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


def pytest_configure(config):
    path = entities_path()
    n = load_entities(path)
    print("\n[conftest] 载入实体表: %d 个（%s）" % (n, path))


@pytest.fixture(scope="session")
def repo_root():
    return ROOT
