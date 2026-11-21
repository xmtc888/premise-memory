"""agent-memory CLI：把多个 agent 的会话汇成一份可检索的库。

用法：
    python cli.py ingest [--agent dsh|codex|claude|hermes|all] [--limit N]
    python cli.py search "查询词" [--limit 10] [--trigram] [--peer 仓库名]
    python cli.py peers [--backfill] [--top 20]
    python cli.py stats

设计边界（见 docs/design.md）：
  · 只做「采集 + 索引 + 检索」；话题归并/演变时间线是第二阶段，不在这里。
  · 索引不调用任何 LLM。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 控制台编码兜底：Windows 默认控制台是 GBK(cp936)，打印 ⚠️ 之类字符会抛
# UnicodeEncodeError 把命令**直接打断**（实测 topics 因此在全新机器上失败，
# install.ps1 随之中止）。这里把 stdout/stderr 切成 utf-8 + errors='replace'，
# 不依赖调用方设置 PYTHONIOENCODING。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                      # noqa: BLE001
        pass

from store import QueryError, Store, ingest            # noqa: E402

DEFAULT_DB = None    # 由 paths.DB_FILE 决定（见 main()），避免出现"第二个真相源"

COLLECTORS = {}


def _load_collectors():
    from collectors import claude, codebuddy, codex, dsh, hermes, workbuddy
    COLLECTORS.update({"dsh": dsh, "codex": codex, "claude": claude, "hermes": hermes,
                       "workbuddy": workbuddy, "codebuddy": codebuddy})


def cmd_ingest(args):
    _load_collectors()
    store = Store(args.db)
    agents = list(COLLECTORS) if args.agent == "all" else [args.agent]
    grand = {"added": 0, "skipped": 0, "by_agent": {}, "sec": 0.0}
    for name in agents:
        mod = COLLECTORS[name]
        print("== 采集 %s ==" % name, flush=True)
        t0 = time.perf_counter()
        try:
            res = ingest(store, mod.collect(limit=args.limit), verbose=True)
        except Exception as e:                            # noqa: BLE001
            print("  [%s] 采集失败: %s: %s" % (name, type(e).__name__, e))
            continue
        print("  新增 %d 会话，跳过 %d，耗时 %.1fs"
              % (res["added"], res["skipped"], time.perf_counter() - t0), flush=True)
        grand["added"] += res["added"]
        grand["skipped"] += res["skipped"]
        grand["sec"] += res["sec"]
        for k, v in res["by_agent"].items():
            grand["by_agent"][k] = grand["by_agent"].get(k, 0) + v
    print("\n===== 汇总 =====")
    print("  新增会话 %d ｜ 已存在跳过 %d ｜ 耗时 %.1fs"
          % (grand["added"], grand["skipped"], grand["sec"]))
    # 已存在的会话不会重写：新能力（peer）必须显式补，否则老会话永远没有归属
    from peers import peer_for_cwd
    bf = store.backfill_peers(peer_for_cwd)
    if bf["updated"]:
        print("  补算 peer %d 个（扫描 %d 个空 peer 会话）" % (bf["updated"], bf["scanned"]))
    _print_stats(store.stats())
    store.close()


def cmd_search(args):
    # ── 升级式检索：docs（编译层）→ sessions（原始层）──
    # 2026-11-06 实测：问「是否推翻过不要向量切块的 RAG」，会话检索 0 命中 / 5 条 checkpoint 噪音，
    # 而 docs 一次命中带日期与数字的结论。所以 docs 优先，命中不足才落回 sessions。
    # 没配 docs 根时**静默跳过**（这台机器还没接这层，不是错误）。
    docs_hits, docs_source, docs_meta = [], "none", {}
    if getattr(args, "docs_first", False):
        import docs_index
        docs_hits, docs_source, docs_meta = docs_index.search_ex(
            args.query, limit=args.limit,
            max_chars=getattr(args, "max_chars", 0) or 0,
            timeout_ms=getattr(args, "timeout_ms", 0) or 0)
        if docs_meta.get("timed_out"):
            # 超时不许静默：不说清就会让人以为"docs 里没有"
            print("docs 层超时（%s ms 预算），扫了 %d 个文件后停下并回落会话"
                  % (getattr(args, "timeout_ms", 0), docs_meta.get("scanned", 0)))
        if docs_hits:
            print("docs 层命中 %d 条（根来源=%s ｜ 扫 %d 文件 ｜ %.0f ms）"
                  % (len(docs_hits), docs_source, docs_meta.get("scanned", 0),
                     docs_meta.get("elapsed_ms", 0.0)))
            for h in docs_hits:
                print("-" * 96)
                print("%s:%d" % (h["file"], h["line"]))
                if h["heading"]:
                    print("  §%s" % h["heading"])
                print("  %s" % h["snippet"])
            print()
            if not getattr(args, "with_sessions", False):
                print("（docs 层已命中；要看原始会话请加 --with-sessions）")
                return
        else:
            print("docs 层无命中（根来源=%s），回落到会话检索" % docs_source)

    store = Store(args.db)
    t0 = time.perf_counter()
    try:
        rows = store.search(args.query, limit=args.limit, use_trigram=args.trigram,
                            peer=args.peer or "",
                            cross_message=getattr(args, "cross_message", False),
                            explain_substring=getattr(args, "explain", False))
    except QueryError as exc:
        store.close()
        # 语法错误 ≠ 没命中：必须让调用方看见（2026-11-04 glm-5.3 复核）
        sys.stderr.write("%s\n" % exc)
        sys.exit(2)
    dt = (time.perf_counter() - t0) * 1000
    via = store.last_search_via or ("trigram" if args.trigram else "fts5")
    print("查询: %r ｜ 命中 %d ｜ %.1f ms ｜ 索引=%s%s"
          % (args.query, len(rows), dt, via,
             (" ｜ peer=%s" % args.peer) if args.peer else ""))
    if store.last_query_relaxed:
        print("  ⚠ 查询含 FTS5 语法字符（`-`/`:`/`.`），已自动按短语处理（不是语法错误）")
    # 别名补检必须报出来：用户要知道结果里混进了别的写法（数据没丢，是查询侧不认旧名）
    if getattr(store, "last_cross_note", ""):
        print("  ↪ %s" % store.last_cross_note)
    if getattr(store, "last_cross_hint", ""):
        print("  提示：%s" % store.last_cross_hint)
    if getattr(store, "last_alias_note", ""):
        print("  ↪ %s（同义写法来自 index/entities.json）" % store.last_alias_note)
    elif getattr(store, "last_alias_error", ""):
        print("  ⚠ 别名补检有一条失败：%s" % store.last_alias_error)
    if via == "like":
        print("  ⚠ 索引未命中，已回退 LIKE 全表扫描：1~2 字中文在 trigram 上无法命中"
              "（换成 3 字以上的词才会走索引）")
    elif not rows and not args.trigram and not getattr(store, "last_cross_hint", ""):
        # 已经因为"多词不共现"给过针对性提示时，不再叠加通用提示（两条一起打只会让人困惑）
        print("  提示：中文查询请加 --trigram（unicode61 不切分中文，默认索引查不到中文）")
    # 子串缺口（2026-11-17）：FTS 按 token 命中，短中文词会漏掉"含在更长词里"的消息
    if getattr(store, "last_substring_note", ""):
        print("  ⚠ %s" % store.last_substring_note)
    for r in rows:
        print("-" * 96)
        print("[%s] %s" % (r["agent"], (r["title"] or "(无标题)")[:60]))
        print("  %s ｜ %s ｜ %s%s"
              % (r["started_at"], r["role"], r["session_key"],
                 (" ｜ peer=%s" % r["peer"]) if r.get("peer") else ""))
        print("  %s" % (r["snippet"] or "").replace("\n", " ")[:300])
    store.close()


def cmd_peers(args):
    """工作区身份（peer）列表与补算：同一仓库的 clone/worktree 共享一个 peer。"""
    store = Store(args.db)
    if args.backfill:
        from peers import peer_for_cwd
        print("== 补算 peer（只补空值；按 cwd 缓存，反复跑幂等）==")
        r = store.backfill_peers(peer_for_cwd)
        print("  扫描 %d 个会话 ｜ 写入 peer %d 个" % (r["scanned"], r["updated"]))
    st = store.stats()["peers"]
    print("\n== peer 覆盖 ==")
    print("  %d 个 peer ｜ 覆盖 %d 个会话" % (st["distinct"], st["sessions"]))
    rows = store.peer_summary(limit=args.top)
    if not rows:
        print("  （空：没有会话落在 git 仓库里，或还没跑 `peers --backfill`）")
    else:
        print("\n== peer TOP（会话数 ｜ 时间跨度 ｜ 代表 cwd）==")
        for r in rows:
            print("  %-44s %4d 会话 ｜ %s → %s"
                  % (r["peer"][:44], r["sessions"],
                     (r["first_at"] or "")[:10], (r["last_at"] or "")[:10]))
            if r["top_cwd"]:
                print("      cwd: %s" % r["top_cwd"])
    store.close()


def cmd_stats(args):
    store = Store(args.db)
    _print_stats(store.stats())
    store.close()


def cmd_doctor(args):
    """环境自检：让"装了但没跑通"一眼可见（借鉴 openviking-server doctor / npm doctor）。

    退出码：0 = 没有阻塞问题（允许 warn）；1 = 有 error（必须处理后才能用）。
    """
    import platform
    import re

    from paths import DB_FILE, HOME

    checks = []

    def add(level, name, detail):
        checks.append((level, name, detail))

    add("OK", "工程目录", HOME)
    py = platform.python_version()
    if sys.version_info >= (3, 10):
        add("OK", "Python", py)
    else:
        add("ERROR", "Python", "%s（需要 3.10+）" % py)
    for mod in ("zstandard", "yaml"):
        try:
            __import__(mod)
            add("OK", "依赖 %s" % mod, "可导入")
        except Exception as exc:                      # noqa: BLE001
            add("ERROR", "依赖 %s" % mod, "导入失败：%s: %s" % (type(exc).__name__, exc))

    db = args.db or DB_FILE
    if not os.path.exists(db):
        add("ERROR", "索引库", "不存在：%s（先跑一次 ingest，约 90 秒）" % db)
    else:
        st = Store(db)
        lib_ver, code_ver = st.schema_version()
        add("OK" if lib_ver == code_ver else "ERROR", "数据 schema",
            "库 v%d / 代码 v%d" % (lib_ver, code_ver))
        stats = st.stats()
        if stats["sessions"]:
            add("OK", "会话 / 消息", "%d / %d" % (stats["sessions"], stats["messages"]))
        else:
            add("ERROR", "会话 / 消息", "0 / 0（索引是空的，先跑 ingest）")
        peers = stats.get("peers") or {}
        add("OK" if peers.get("sessions") else "WARN", "peer 覆盖",
            "%d 个 peer / %d 会话（没有就跑一次 peers --backfill）"
            % (peers.get("distinct", 0), peers.get("sessions", 0)))
        last = st.con.execute("SELECT MAX(indexed_at) FROM sessions").fetchone()[0] or ""
        add("OK" if last else "WARN", "最近入库", last or "（无记录）")
        st.close()

    skill_md = ""
    for cand in (os.path.join(HOME, "SKILL.md"),
                 os.path.join(HOME, "skill", "agent-memory", "SKILL.md")):
        if os.path.isfile(cand):
            skill_md = cand
            break
    if skill_md:
        with open(skill_md, encoding="utf-8", errors="replace") as fh:
            txt = fh.read()
        m = re.search(r'(?m)^version:\s*"([^"]+)"', txt)
        add("OK", "技能版本", m.group(1) if m else "未标注")
    else:
        add("WARN", "技能版本", "找不到 SKILL.md（git 工程形态下正常）")

    # 别名表自检（2026-11-07 GLM 复核发现 3）：同一写法挂到多个实体上时，
    # 反查表 `setdefault` 先到先得、**全程无提示**，会确定性地解析到错的那个实体。
    # 这里只报 WARN，不阻断检索。
    try:
        import aliases
        dups = aliases.duplicates()
        if dups:
            shown = "、".join("%s→%s" % (k, "/".join(v)) for k, v in list(dups.items())[:3])
            more = "（共 %d 条）" % len(dups) if len(dups) > 3 else ""
            add("WARN", "实体别名表", "同一写法挂到多个实体，反查取第一个：%s%s" % (shown, more))
        else:
            add("OK", "实体别名表", "无重复写法")
    except Exception as exc:  # noqa: BLE001
        add("WARN", "实体别名表", "自检失败：%s: %s" % (type(exc).__name__, exc))

    # 注入块新鲜度（2026-11-09）：refresh.ps1 每天写各项目的 .agent-memory/context.md，
    # 但它靠计划任务驱动 —— 任务没注册/被删/机器休假，文件就会悄悄过期，
    # 而 agent 仍照着"旧路径已改名"这类过期事实工作（实测代价：16 次工具调用 vs 2 次）。
    try:
        from inject import freshness, STALE_AFTER_DAYS
        _tg = _load_targets()
        if not _tg:
            add("OK", "注入块", "无注入目标（targets.json 为空）")
        else:
            stale, fresh, missing = [], [], []
            for _t in _tg:
                _f = freshness(_t.get("out", ""), STALE_AFTER_DAYS)
                if not _f["exists"]:
                    missing.append(_t.get("entity", "?"))
                elif _f["stale"]:
                    stale.append((_t.get("entity", "?"), _f))
                else:
                    fresh.append(_t.get("entity", "?"))
            if stale or missing:
                parts = ["%s 过期 %.1f 天" % (e, f["age_days"] or 0) for e, f in stale]
                parts += ["%s 未生成" % e for e in missing]
                add("WARN", "注入块", "；".join(parts) + "（跑 scripts/refresh.ps1 刷新）")
            else:
                add("OK", "注入块", "%d 个目标均新鲜（阈值 %d 天）" % (len(fresh), STALE_AFTER_DAYS))
    except Exception as exc:  # noqa: BLE001
        add("WARN", "注入块", "自检失败：%s: %s" % (type(exc).__name__, exc))

    # 计划任务自检（2026-11-17 复核查因新增）：refresh.ps1 靠 agent-memory-daily 驱动，
    # 但初版注册用 InteractiveToken（只在用户登录且未锁屏时有令牌），每天 06:30 触发时
    # 锁屏/未登录 ⇒ LastTaskResult=0x800710E0、脚本从未执行，静默断流 8 天才被撞见。
    # 这里读任务上次运行结果：非 0 即 WARN —— 让断流在第一天就可见，而不是第 8 天。
    try:
        import subprocess as _sp
        _q = _sp.run(
            ["schtasks", "/Query", "/TN", "agent-memory-daily", "/V", "/FO", "CSV"],
            capture_output=True, text=True, encoding="gbk", errors="replace",
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
            timeout=10,
        )
        if _q.returncode != 0 or not _q.stdout or "agent-memory-daily" not in _q.stdout:
            add("WARN", "计划任务", "查不到 agent-memory-daily（未注册或被删，注入块不会刷新）")
        else:
            import csv as _csv
            _rows = list(_csv.reader(_q.stdout.splitlines()))
            _hdr = _rows[0]
            _val = _rows[1]
            _task = dict(zip(_hdr, _val))
            # schtasks 的 CSV 列名随系统语言：中文系统是「上次结果/上次运行时间」，
            # 英文系统是 "Last Result"/"Last Run Time"（实测踩坑：初版猜了「上次运行结果」，差一个字取空）
            _last_res = (_task.get("上次结果") or _task.get("Last Result") or "").strip()
            _last_run = (_task.get("上次运行时间") or _task.get("Last Run Time") or "").strip()
            if _last_res == "0":
                add("OK", "计划任务", "上次运行成功 %s" % _last_run)
            elif _last_res == "267009":
                add("OK", "计划任务", "正在运行（上次触发 %s）" % _last_run)
            else:
                _known = {"2147946720": "0x800710E0 拒绝请求（InteractiveToken 锁屏时段拿不到令牌，改 S4U 重注册）"}
                _hint = _known.get(_last_res, "非零失败码")
                add("WARN", "计划任务", "上次运行失败 %s：%s（上次 %s）"
                    % (_last_res, _hint, _last_run or "未知"))
    except Exception as exc:  # noqa: BLE001
        add("WARN", "计划任务", "自检失败：%s: %s" % (type(exc).__name__, exc))

    # procedure 原料（2026-11-17）：tool_calls 是 procedure 挖掘的地基。
    # 表存在但没数据 = 采集没接上（或刚换机没回填），此时 `am procedure` 只会空转，
    # 注入块里的「常用操作」也永远为空——这类"静默为空"正是本项目一直在防的。
    try:
        if os.path.exists(db):
            _c = Store(db)
            _n = _c.con.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
            _s = _c.con.execute(
                "SELECT COUNT(DISTINCT session_key) FROM tool_calls").fetchone()[0]
            _c.close()
            if _n:
                add("OK", "procedure 原料", "%d 条命令 / %d 会话（am procedure 可挖）" % (_n, _s))
            else:
                add("WARN", "procedure 原料", "tool_calls 为空（`am procedure` 会空转；"
                    "跑一次 ingest 或回填历史会话）")
    except Exception as exc:  # noqa: BLE001
        add("WARN", "procedure 原料", "自检失败：%s: %s" % (type(exc).__name__, exc))

    # procedure 缓存（2026-11-17）：MCP 工具与 inject --procedures 都读它。
    # 缺缓存时工具会现算（约 12 秒），所以这里只报 WARN 并给修复命令。
    try:
        if os.path.exists(db):
            import proc_cache
            _doc = proc_cache.load()
            _fresh, _why = proc_cache.is_fresh(_doc, db)
            if _doc is None or not _fresh:
                add("WARN", "procedure 缓存", "%s（跑 `am procedure --refresh-cache`，约 12 秒）"
                    % (_why or "缺失"))
            else:
                add("OK", "procedure 缓存",
                    "%d 仪式 / %d 修复对；%s" % (len(_doc.get("rituals") or []),
                                                len(_doc.get("repairs") or []), _why))
    except Exception as exc:  # noqa: BLE001
        add("WARN", "procedure 缓存", "自检失败：%s: %s" % (type(exc).__name__, exc))

    width = max(len(n) for _l, n, _d in checks)
    print("=== agent-memory doctor ===")
    for level, name, detail in checks:
        print("  [%-5s] %-*s  %s" % (level, width, name, detail))
    errors = sum(1 for level, _n, _d in checks if level == "ERROR")
    warns = sum(1 for level, _n, _d in checks if level == "WARN")
    print("\n结果：%d error / %d warn" % (errors, warns))
    return 1 if errors else 0


def cmd_init(args):
    from init_cmd import init
    store = Store(args.db)
    r = init(store, force=args.force)
    print("=== 初始化本机数据 ===")
    print("  实体表: %s（%d 个实体）"
          % ("已生成" if r["entities_created"] else "已存在（未改）", r["entities"]))
    print("  注入目标: %s" % ("已生成" if r["targets_created"] else "已存在（未改）"))
    if r["candidates"]:
        print("  候选项目（按会话数）：")
        for d, n in r["candidates"]:
            print("    %-40s %d 会话" % (d, n))
    store.close()
    print("\n提示：历史改名（旧名→新名）脚本不猜，请手工补到 index/entities.json。")


def cmd_topic(args):
    store = Store(args.db)
    if args.build:
        from topic_build import build
        print("== 构建话题标签 ==", flush=True)
        res = build(store)
        print("  %d 会话 ｜ 有实体 %d ｜ 有意图 %d ｜ 耗时 %.1fs"
              % (res["sessions"], res["with_entity"], res["with_intent"], res["sec"]))
    cov = store.coverage()
    tot = max(1, cov["sessions"])
    print("\n== 标签覆盖 ==")
    print("  会话 %d ｜ 有实体 %d (%.0f%%) ｜ 有意图 %d (%.0f%%)"
          % (cov["sessions"], cov["with_entity"], 100.0 * cov["with_entity"] / tot,
             cov["with_intent"], 100.0 * cov["with_intent"] / tot))
    print("\n== 实体 TOP（会话数 ｜ 首末时间）==")
    tot_s = max(1, cov["sessions"])
    for r in store.topic_summary("entity")[:args.top]:
        flag = "  ⚠️粗" if r["sessions"] > tot_s * 0.5 else ""
        print("  %-26s %4d 会话 (%2.0f%%) ｜ %s → %s%s"
              % (r["label"], r["sessions"], 100.0 * r["sessions"] / tot_s,
                 (r["first_at"] or "")[:10], (r["last_at"] or "")[:10], flag))
    print("\n== 意图 TOP（覆盖率 >25% 标 ⚠️ 说明太粗，需拆分或收紧关键词）==")
    for r in store.topic_summary("intent")[:args.top]:
        pct = 100.0 * r["sessions"] / tot_s
        flag = "  ⚠️粗" if pct > 25 else ""
        print("  %-14s %4d 会话 (%2.0f%%)%s" % (r["label"], r["sessions"], pct, flag))
    print("\n== 话题组合 (实体 × 意图)，会话数 >= %d ==" % args.pairs)
    for r in store.topic_pairs(min_sessions=args.pairs)[:args.top]:
        print("  %-26s × %-14s %3d 会话 ｜ %s → %s"
              % (r["entity"], r["intent"], r["sessions"],
                 (r["first_at"] or "")[:10], (r["last_at"] or "")[:10]))

    if args.entity:
        rows = store.sessions_for(args.entity, args.intent or "")
        print("\n== 时间线：%s%s（%d 个会话，按时间）=="
              % (args.entity, (" × " + args.intent) if args.intent else "", len(rows)))
        for r in rows[:args.top]:
            print("  %s  %-8s %s"
                  % ((r["started_at"] or "")[:10], r["agent"], (r["title"] or "(无标题)")[:60]))
    store.close()


def cmd_timeline(args):
    from timeline import build_timeline, render
    tl = build_timeline(args.db, args.entity, args.intent or "")
    out = render(tl, max_concl=args.per)
    print(out)
    if args.out:
        parent = os.path.dirname(os.path.abspath(args.out))
        os.makedirs(parent, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(out)
        print("\n已落盘 -> %s" % args.out)


def cmd_premises(args):
    from premises import check_conclusions
    r = check_conclusions(args.db, args.entity, limit=args.limit)
    s = r["stats"]
    print("=== 前提校验：%s ===" % r["entity"])
    print("  会话 %d ｜ 结论 %d ｜ check_at %s"
          % (r["sessions"], s.get("结论总数", 0), r["check_at"]))
    print("  活目录（该实体现存的）: %s" % (r["live_dirs"].get(r["entity"]) or "（无）"))
    print()
    total = max(1, s.get("结论总数", 0))
    for k, label in (("alive", "🟢 前提通过"),
                     ("dead_with_successor", "🟡 项目已迁移（带注，不劝退）"),
                     ("dead_no_successor", "🔴 真失效"),
                     ("unverified", "⚪ 无可验证前提 / 非项目资产")):
        n = s.get(k, 0)
        print("  %-32s %5d (%4.1f%%)" % (label, n, 100.0 * n / total))
    print()
    for verdict, title in (("dead_no_successor", "🔴 真失效清单"),
                           ("dead_with_successor", "🟡 迁移清单（带注，不劝退）")):
        rows = [it for it in r["items"] if it["verdict"] == verdict][:args.top]
        print("=== %s（%d 条，显示前 %d）===" % (title, s.get(verdict, 0), len(rows)))
        for it in rows:
            print("  [%s] %s" % (it["when"], ", ".join(it["paths"])))
            if it["note"]:
                print("     └ %s" % it["note"])
            # 认证四件套（2026-11-17）：依据 + 检验时刻 + 失效条件
            print("     └ 认证 %s ｜ 依据：%s ｜ 失效条件：%s"
                  % (it.get("certified_at") or it.get("check_at") or "?",
                     it.get("basis") or "-", it.get("recheck") or "-"))
        print()


def cmd_diagnose(args):
    """人类诊断：待诊断候选 / 记录诊断 / 已诊断清单。

    为什么单独一个子命令（WASP-Mem, arXiv 2611.04003）：**agent 负责组织、人类负责诊断**；
    一轮人类诊断胜过四轮自判 7.0~10.9%。自动判定出来的"修复对"只是候选，
    诊断过人话之后才是可注入的知识。
    """
    import diagnoses
    import procedures

    action = args.action
    if action == "list":
        store = Store(args.db)
        try:
            rows = procedures.mine_repairs(store.con)
        finally:
            store.close()
        doc = diagnoses.load()
        done = doc["items"]
        pending = [r for r in rows if r.get("id") not in done]
        print("=== 待诊断候选：%d 条（已诊断 %d 条；全量修复对 %d 组）==="
              % (len(pending), len(done), len(rows)))
        if not pending:
            print("  （没有待诊断的候选：跑一次 ingest 攒数据，或 `am diagnose show` 看已诊断的）")
            return 0
        for r in pending[:args.top]:
            print("  [%s] %d 次 / %d 会话 / exit=%s"
                  % (r["id"], r["raw_hits"], r["sessions"],
                     ",".join(str(c) for c in r["exit_codes"]) or "?"))
            print("     坏: %s" % r["bad"][:150])
            print("     好: %s" % r["good"][:150])
            print("     实: %s" % r["sample_bad"][:130])
            print("     实: %s" % r["sample_good"][:130])
            print("     → 诊断: am diagnose add %s \"当时是因为…应该…\"" % r["id"])
        if len(pending) > args.top:
            print("  …另有 %d 条未显示（用 --top 调整）" % (len(pending) - args.top))
        return 0

    if action == "add":
        rid = (args.id or "").strip()
        text = " ".join(args.text or []).strip()
        if not rid:
            print("用法: am diagnose add <id> \"<人话诊断>\"", file=sys.stderr)
            return 2
        if not text:
            print("诊断内容不能为空（诊断就该是人写的原话）", file=sys.stderr)
            return 2
        store = Store(args.db)
        try:
            rows = procedures.mine_repairs(store.con)
        finally:
            store.close()
        item = next((r for r in rows if r.get("id") == rid), None)
        if item is None:
            print("找不到候选 %s：id 必须来自 `am diagnose list`（候选每次重新挖掘，id 稳定）"
                  % rid, file=sys.stderr)
            return 2
        rec = diagnoses.record(item, text)
        print("已记录诊断 %s（%s）" % (rec["id"], rec["diagnosed_at"]))
        print("  坏: %s" % rec["bad"][:120])
        print("  好: %s" % rec["good"][:120])
        print("  诊断: %s" % rec["diagnosis"])
        print("  下一步：`am inject <实体> --procedures` 会把它带进注入块（人已诊断 = 强证据）")
        return 0

    if action == "show":
        items = diagnoses.all_items()
        st = diagnoses.stats()
        print("=== 已诊断：%d 条（%s）===" % (st["total"], st["file"]))
        if not items:
            print("  （还没有诊断记录；`am diagnose list` 看候选）")
            return 0
        for r in items[:args.top]:
            print("  [%s] %s" % (r["id"], r["diagnosed_at"]))
            print("     坏: %s" % (r["bad"] or "")[:130])
            print("     好: %s" % (r["good"] or "")[:130])
            print("     诊断: %s" % r["diagnosis"])
        return 0

    print("未知动作: %s（可选 list / add / show）" % action, file=sys.stderr)
    return 2


def cmd_install_host(args):
    """给 Codex / Claude Code 安装或撤销「开工先读 context.md」的指令片段。

    默认装**全局**（`~/.codex/AGENTS.md` 或 `~/.claude/CLAUDE.md`）；
    `--project <目录>` 装项目级（影响面更小，适合给别人用）。
    装的是**标记块**：`--revert` 只删块，不动文件里其它内容。
    """
    import install_host as IH

    host = args.host
    project = args.project or ""
    if args.status:
        st = IH.status(IH.target_path(host, project))
        print("目标：%s" % st["path"])
        print("  文件存在: %s ｜ 已安装片段: %s ｜ 现字符数: %d"
              % (st["exists"], st["installed"], st["chars"]))
        return 0

    if args.revert:
        r = IH.revert(host, project, dry_run=args.dry_run)
        act = r["action"]
        print("[revert%s] %s → %s" % ("-dry" if args.dry_run else "", r["path"], act))
        if act == "reverted":
            print("  字符 %d → %d（只删了标记块，其它内容未动）"
                  % (r["chars_before"], r["chars_after"]))
        elif act == "deleted-file":
            print("  该文件是安装器创建的，已整体删除")
        return 0

    if args.dry_run:
        p = IH.plan(host, project)
        print("=== 预览（未写入）===")
        print("目标：%s（存在=%s ｜ 已装=%s）" % (p["path"], p["exists"], p["already_installed"]))
        print("将写入的块：")
        for ln in p["block"].splitlines():
            print("  | " + ln)
        if p["exists"]:
            print("文件现有末尾 400 字符（供对照）：")
            print(p["current_tail"])
        print("\n确认无误后去掉 --dry-run 再跑一次即可写入。")
        return 0

    r = IH.install(host, project)
    print("[install] %s → %s（%s）" % (r["path"], r["action"],
                                       "新增 %d 行" % r["added_lines"]
                                       if r.get("added_lines") else "无变化"))
    print("  撤销：am install-host %s%s --revert"
          % (host, (" --project \"%s\"" % project) if project else ""))
    return 0


def cmd_procedure(args):
    """procedure 挖掘（只读）：从 tool_calls 里提炼可复用的操作步骤。

    两类原料（2026-11-17 实测）：
      repair  失败 -> 5 条内同程序改正 -> 成功（"怎么修"，1,776 组）
      ritual  同目录跨 >=3 会话重复的命令（"怎么做"的仪式，39 条）
    """
    import procedures
    store = Store(args.db)
    con = store.con

    if args.refresh_cache:
        import proc_cache
        store = Store(args.db)
        try:
            doc = proc_cache.build(store.con, args.db)
        finally:
            store.close()
        print("procedure 缓存已重建：%s" % proc_cache.CACHE_FILE)
        print("  rituals=%d ｜ repairs=%d ｜ 耗时 %.1f s ｜ built_at=%s"
              % (len(doc["rituals"]), len(doc["repairs"]),
                 doc["elapsed_ms"] / 1000.0, doc["built_at"]))
        return 0

    s = procedures.summarize(con)
    print("=== procedure 挖掘（只读） ===")
    print("  命令调用 %d ｜ 成功 %d ／ 失败 %d ／ 无结果 %d ｜ 涉及会话 %d"
          % (s["calls"], s["ok"], s["failed"], s["no_result"], s["sessions"]))
    if not s["calls"]:
        print("  （库里没有 tool_calls：跑一次 ingest，或用工具回填历史会话）")
        store.close()
        return 0
    print()

    rc = 0
    want_repair = args.kind in ("all", "repair")
    want_ritual = args.kind in ("all", "ritual")

    # 前提判定的上下文（第 3 步）：只在 --judge 时加载（要读 session_paths，有开销）
    judge_ctx = None
    if args.judge:
        import premises
        from premises import dir_entity_map, live_dirs_by_entity
        judge_ctx = (live_dirs_by_entity(args.db), dir_entity_map(args.db))

    def _j(item):
        if judge_ctx is None:
            return ""
        v = procedures.judge(item, judge_ctx[0], judge_ctx[1])
        mark = {premises.ALIVE: "🟢", premises.DEAD_WITH_SUCCESSOR: "🟡",
                premises.DEAD_NO_SUCCESSOR: "🔴"}.get(v["verdict"], "⚪")
        s = "%s %s" % (mark, v["verdict"])
        if v["note"]:
            s += "（%s）" % v["note"]
        # 认证四件套（2026-11-17）：结论 + 检验时刻 + 依据 + 失效条件。
        # 依据 Many Steps into One：计数不是变更检测器，必须给出"凭什么"和"何时作废"。
        s += " ｜ 认证 %s ｜ 依据：%s ｜ 失效条件：%s" % (
            v.get("certified_at") or "?", v.get("basis") or "-", v.get("recheck") or "-")
        return s

    if want_repair:
        rows = procedures.mine_repairs(con, min_sessions=args.repair_min_sessions)
        cross = [r for r in rows if r["sessions"] >= 2]
        print("=== 修复对（失败 → 改正 → 成功）：%d 组，其中跨 >=2 会话 %d 组 ==="
              % (len(rows), len(cross)))
        if rows and not cross:
            print("  提示：修复知识基本是「这一次碰到的问题」，价值在当次可查；")
            print("        按出现次数排序（同一会话重复踩同一个坑同样高价值）。")
        for r in rows[:args.top]:
            print("  [%d 会话 / %d 次 / exit=%s]"
                  % (r["sessions"], r["raw_hits"], ",".join(str(c) for c in r["exit_codes"]) or "?"))
            print("     坏: %s" % r["bad"][:160])
            print("     好: %s" % r["good"][:160])
            print("     实: %s" % r["sample_bad"][:150])
            print("     实: %s" % r["sample_good"][:150])
            jd = _j(r)
            if jd:
                print("     前提: %s" % jd)
        if rows:
            print()

    if want_ritual:
        rows = procedures.mine_rituals(con, min_sessions=args.ritual_min_sessions)
        print("=== 仪式（同目录跨 >=%d 会话重复）：%d 条 ==="
              % (args.ritual_min_sessions, len(rows)))
        for r in rows[:args.top]:
            print("  [%d 会话] %s" % (r["sessions"], r["shape"][:150]))
            print("     cwd=%s" % (r["cwd"] or "?"))
            print("     实: %s" % r["sample"][:150])
            jd = _j(r)
            if jd:
                print("     前提: %s" % jd)

    store.close()
    return rc


def cmd_inject(args):
    from inject import (STALE_AFTER_DAYS, build_cards_block, build_injection,
                        freshness, render_cards_block)
    stale_days = args.stale_days if getattr(args, "stale_days", None) else STALE_AFTER_DAYS

    # ── --check：只查"现有的注入块是否过期"，不生成 ──
    # 用途：日更任务自检、CI、以及 agent 开工前确认"我读到的环境事实还算不算数"。
    if getattr(args, "check", False):
        targets = _load_targets()
        rows = []
        if args.write:
            rows = [("(指定路径)", args.write)]
        elif targets:
            rows = [(t.get("entity", "?"), t.get("out", "")) for t in targets]
        if not rows:
            print("没有可检查的目标（既没给 --write，targets.json 也是空的）", file=sys.stderr)
            return 2
        bad = 0
        for entity, out in rows:
            f = freshness(out, stale_days)
            tag = "STALE" if f["stale"] else "OK"
            if f["stale"]:
                bad += 1
            print("[%-5s] %-10s %s" % (tag, entity, out))
            print("        %s" % f["reason"])
            if f["check_at"]:
                print("        check_at=%s ｜ age=%.1f 天 ｜ 阈值 %d 天"
                      % (f["check_at"], f["age_days"] or 0, stale_days))
        if bad:
            print("\n有 %d 个注入块过期：跑一次 scripts/refresh.ps1（或 inject --write）刷新。"
                  % bad)
            return 1
        return 0

    chunks, stats_lines = [], []
    #: 每段的名字与字符数（用于块头的"成本可见"行：claude-mem 的头部做法——
    #: 会话开头给索引，同时告诉你这一块花了多少 token，别让人盲付上下文成本）
    section_sizes: list[tuple[str, int]] = []

    # ── 卡片块（改进清单第 3 条）：有界注入 + 明说截断 ──
    if getattr(args, "cards", False):
        blk = build_cards_block(
            topic=getattr(args, "topic", "") or "",
            scope=getattr(args, "scope_filter", "") or "",
            max_cards=getattr(args, "max_cards", 0) or 5,
            max_chars=getattr(args, "card_chars", 0) or 900,
            include_dead=getattr(args, "include_dead", False),
            skip_acked=not getattr(args, "include_acked", False),
            ack_ttl_days=getattr(args, "ack_ttl_days", 0) or 0,
            include_ids=not getattr(args, "no_card_ids", False))
        ctext = render_cards_block(blk)
        chunks.append(ctext)
        section_sizes.append(("卡片", len(ctext)))
        stats_lines.append("卡片：候选 %d ｜ 注入 %d ｜ 截断 %d ｜ 已确认跳过 %d ｜ %d 字符"
                           % (blk["candidates"], blk["picked"], blk["truncated"],
                              blk["acked_skipped"], blk["chars"]))

    # ── 环境事实块（原有能力）：给了 entity 才做 ──
    # ⚠️ 顺序纪律：它带 MARKER+check_at，必须排在 procedure 块**之前** ——
    # 否则一块 300+ 字符的 procedure 会把头部标记挤出窗口，`inject --check`
    # 与 doctor 都会误报"文件头没有 check_at"（2026-11-17 实测踩到）。
    r = None
    if args.entity:
        r = build_injection(args.db, args.entity, max_chars=args.max_chars,
                            stale_days=stale_days)
        chunks.append(r["text"])
        section_sizes.append(("环境事实", len(r["text"])))
        st = r["stats"]
        stats_lines.append("迁移条目 %d → 规则 %d 条 ｜ 失效 %d ｜ %d 字符（约 %d token）"
                           % (st["migrate_items"], st["rules"], st["dead_items"],
                              st["chars"], st["est_tokens"]))

    # ── procedure 块（2026-11-17）：本项目"每次都要做的"与"这个坑怎么填" ──
    # 放在环境事实之后（见上面的顺序纪律）。
    if getattr(args, "procedures", False):
        if not args.entity:
            print("--procedures 需要 <entity>：procedure 按项目作用域挑选，不能全局注入",
                  file=sys.stderr)
            return 2
        import procedures as P
        # 走缓存（2026-11-17）：现挖 11.8 s，缓存后毫秒级；缓存缺失/过期会自动重建并如实标注
        blk = P.pick_cached(args.db, args.entity,
                            max_items=getattr(args, "max_procedures", 0) or 3,
                            max_repairs=getattr(args, "max_procedure_repairs", 0) or 1)
        ptext = P.render_block(blk, max_chars=getattr(args, "procedure_chars", 0) or 500)
        if ptext.strip():
            chunks.append(ptext)
            section_sizes.append(("procedure", len(ptext)))
        stats_lines.append("procedure：候选 %d ｜ 注入 %d ｜ 前提失效跳过 %d ｜ %d 字符"
                           % (blk["candidates"], len(blk["items"]),
                              blk["dead_skipped"], len(ptext)))
        if blk.get("cache_note"):
            stats_lines.append("procedure 缓存：%s（built_at %s）"
                               % (blk["cache_note"], blk.get("cache_built_at") or "?"))

    if not chunks:
        print("什么都没生成：给 <entity>（环境事实）或加 --cards（结论卡）", file=sys.stderr)
        return 2

    text = "\n\n".join(c.rstrip("\n") for c in chunks if c.strip()) + "\n"
    # ── 成本可见（claude-mem 的头部做法）──
    # 会话开头给的是"索引层"，同时必须告诉读者这一块占了多少上下文；否则注入
    # 就成了黑箱收费。细节层不塞进块里，而是给展开命令（按需付费）。
    if section_sizes:
        total_chars = sum(n for _l, n in section_sizes)
        parts = " / ".join("%s 约 %d token" % (label, round(n / 1.6))
                           for label, n in section_sizes)
        cost = ("> 本块约 %d token（%s）。这是**索引层**；细节按需展开："
                "`am procedure --judge` 看步骤与前提、`am premises \"<实体>\"` 看失效路径、"
                "`am search \"<关键词>\"` 回原文。" % (round(total_chars / 1.6), parts))
        text = _insert_cost_line(text, cost)
    print(text)
    for line in stats_lines:
        print("--- 统计: %s" % line)
    if args.write:
        parent = os.path.dirname(os.path.abspath(args.write))
        os.makedirs(parent, exist_ok=True)
        with open(args.write, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("已写入 -> %s" % args.write)
        if r is not None:
            print("提示：文件头带 stale_after_days=%d；用 `inject --check` 可随时查它是否过期，"
                  "日更见 scripts/refresh.ps1。" % stale_days)
    return 0


def _insert_cost_line(text: str, cost_line: str) -> str:
    """把"成本行"插到块头 MARKER 之后（MARKER 必须在第一行，`inject --check` 靠它）。

    为什么插在 MARKER 后而不是最前：MARKER 是**机器识别**用的（文件头识别 + 读 check_at），
    成本行是**给人/agent 读**的；两者顺序不能颠倒。
    """
    from inject import MARKER
    lines = text.split("\n")
    if lines and lines[0].startswith(MARKER):
        return "\n".join([lines[0], "", cost_line, ""] + lines[1:])
    # 没有 MARKER（例如只注入卡片块）：直接放最前
    return cost_line + "\n\n" + text


def _load_targets() -> list[dict]:
    """读本机 targets.json（`index/targets.json`）里的注入目标。读不到返回 []。"""
    try:
        from paths import TARGETS_FILE
    except Exception:                                  # noqa: BLE001
        return []
    if not os.path.exists(TARGETS_FILE):
        return []
    try:
        with open(TARGETS_FILE, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
        return list(doc.get("targets") or [])
    except Exception:                                  # noqa: BLE001
        return []


def _print_stats(st):
    print("  会话 %d ｜ 消息 %d" % (st["sessions"], st["messages"]))
    for row in st["by_agent"]:
        print("    %-8s %4d 会话 ｜ %6d 条" % (row["agent"], row["sessions"], row["msgs"] or 0))
    peers = st.get("peers") or {}
    if peers:
        print("  peer：%d 个 ｜ 覆盖 %d 个会话（其余 session 不在 git 仓库里，无 peer）"
              % (peers.get("distinct", 0), peers.get("sessions", 0)))


def cmd_docs(args):
    """docs 层自检：这台机器接了哪些 docs 根、有多少 md、最近更新。

    没有配置时明确说"未接入（这层关闭）"，而不是报错 —— 这层本来就是可选的。
    """
    import docs_index
    st = docs_index.stats()
    print("=== docs 层（编译产物）===")
    print("  根来源：%s" % st["source"])
    if not st["roots"]:
        print("  未接入：没有可用的 docs 根（这层关闭，检索自动走会话层）")
        print("  接法：在 %s 写 {\"roots\": [\"<docs 目录>\"]}" % st["config"])
        print("       或设环境变量 DOCS_MCP_ROOTS=<逗号分隔的目录>")
        return 0
    print("  根 %d 个：" % len(st["roots"]))
    for r in st["roots"]:
        print("    - %s" % r)
    print("  md 文件 %d 个 ｜ 最近更新 %s" % (st["files"], st["newest"] or "-"))
    print("  配置文件：%s" % st["config"])
    return 0


def cmd_cards(args):
    """结论卡：add / list / search / supersede / export / import / validate / stats。

    输出一律用 ASCII 标签（`[alive]` 等）而不是 emoji：本仓常在 Windows GBK 控制台里跑，
    emoji 会直接 UnicodeEncodeError（2026-11-08 实测，写 markdown 时踩过同类坑）。
    """
    import cards as C

    act = args.action
    if act == "add":
        ev = []
        for raw in args.ev or []:
            parts = [x.strip() for x in raw.split("|")]
            if len(parts) < 3:
                print("证据格式不对：%r（应为 agent|YYYY-MM-DD|原文摘录[|session]）" % raw,
                      file=sys.stderr)
                return 2
            e = {"agent": parts[0], "date": parts[1], "quote": parts[2]}
            if len(parts) >= 4 and parts[3]:
                e["session"] = parts[3]
            if len(parts) >= 5 and parts[4]:
                e["msg"] = parts[4]          # messages.id：有了它 cards open 才能回原文全文
            ev.append(e)
        card = C.build(args.claim, ev, status=args.status or "alive", topic=args.topic,
                       scope=args.scope, supersedes=args.supersedes_card or [],
                       scopes=args.scopes or None)
        # 机器状态类卡的声明式活体检查（2026-11-18）：--verify "dir_exists:C:\x"
        if getattr(args, "verify_spec", None):
            try:
                card["verify"] = [C.parse_verify_spec(s) for s in args.verify_spec]
            except ValueError as exc:
                print("verify 参数错误：%s" % exc, file=sys.stderr)
                return 2
        state, msg = C.upsert(card)
        print("%-8s %s" % (state, msg))
        if state == "invalid":
            return 2
        if args.also_supersede:
            ok, m = C.supersede(args.also_supersede, card["id"])
            print("%-8s %s" % ("ok" if ok else "error", m))
            return 0 if ok else 2
        return 0

    if act == "list":
        rows = C.search(args.filter or "", status=args.status, topic=args.topic,
                        scope=args.scope_filter or "")
        print("卡片 %d 张（文件 %s）" % (len(rows), C.cards_file()))
        for c in rows[:args.limit]:
            print(C.render(c))
            print()
        if len(rows) > args.limit:
            print("…… 其余 %d 张，用 --limit 放大" % (len(rows) - args.limit))
        return 0

    if act == "search":
        rows = C.search(args.text, status=args.status, topic=args.topic,
                        scope=args.scope_filter or "")
        print("命中 %d 张" % len(rows))
        for c in rows[:args.limit]:
            print(C.render(c))
            print()
        return 0

    if act == "supersede":
        ok, msg = C.supersede(args.old_id, args.new_id)
        print("%-8s %s" % ("ok" if ok else "error", msg))
        return 0 if ok else 2

    if act == "open":
        # 证据锚点下钻（等价于 TencentDB Agent Memory 的 node_id 回原文）：
        # 卡片里的 quote 只有 200 字，要上下文就靠 evidence.msg 精确取回 messages 全文。
        cid = (args.text or "").strip()
        rows, _ = C.load()
        card = next((c for c in rows if c.get("id") == cid), None)
        if not card:
            print("找不到卡片 %r（用 cards list 看 id）" % cid, file=sys.stderr)
            return 2
        print("卡片 %s" % cid)
        print(C.render(card))
        anchors = [e for e in card.get("evidence") or [] if e.get("msg") not in (None, "")]
        if not anchors:
            print("\n（这张卡的证据没有 msg 锚点，只能看 quote；写入时用 --ev "
                  "agent|date|quote|session|msg 可加锚点）")
            return 0
        store = Store(args.db)
        try:
            for e in anchors:
                m = store.message_by_id(e["msg"])
                print("\n" + "-" * 96)
                if not m:
                    print("证据 #%s：本机库里没有这条消息（跨机或库被重建过）——如实说明，不当成找到"
                          % e["msg"])
                    continue
                print("证据 #%s ｜ %s ｜ %s ｜ %s ｜ %s"
                      % (m["id"], m["agent"], m["role"], m["ts"], m["session_key"]))
                if m.get("title"):
                    print("会话：%s" % m["title"][:80])
                print("")
                print(m["text"])
        finally:
            store.close()
        return 0

    if act == "export":
        if args.md:
            n, warns = C.export_md(args.dst)
            print("导出 Markdown %d 张 -> %s（可被 search --docs-first 检索）" % (n, args.dst))
        else:
            n, warns = C.export_to(args.dst)
            print("导出 %d 张 -> %s" % (n, args.dst))
        for w in warns:
            print("  warn: %s" % w)
        return 0

    if act == "ack":
        # 人工确认账本（2026-11-19）：确认过的卡在内容指纹未变时不再注入。
        # 三条失效路径都不需要人手动清账（内容变 / 过期 / 卡被删），见 cards.py 顶部说明。
        if getattr(args, "prune", False):
            n, remain = C.prune_acks(ttl_days=args.ttl_days)
            print("清理 %d 条（账本剩 %d 条；文件 %s）"
                  % (n, remain, C.ack_file() or "(未初始化)"))
            return 0
        if getattr(args, "show", False):
            rows = C.acks(ttl_days=args.ttl_days)
            if not rows:
                print("账本还是空的：`cards ack <卡id>` 记一次确认")
                return 0
            label = {"valid": "免打扰", "changed": "内容已变（会自动重现）",
                     "expired": "已过期（会自动重现）", "missing": "卡已不存在"}
            print("账本 %d 条（文件 %s）" % (len(rows), C.ack_file() or "(跟随卡片文件)"))
            for r in rows:
                print("- %-12s %s  %-8s %s  %s"
                      % (r.get("id"), r.get("ack_at") or "?", r.get("by") or "?",
                         label.get(r.get("state"), r.get("state")),
                         (r.get("claim") or "")[:60]))
            print("\n免打扰条件：内容指纹未变且未超过 %d 天（--ttl-days 可改）"
                  % (args.ttl_days or C.ACK_TTL_DAYS))
            return 0
        cid = (args.text or "").strip()
        if not cid:
            print("ack 需要卡 id：`cards ack <卡id>`；看名单 `cards ack --show`；撤销加 --undo",
                  file=sys.stderr)
            return 2
        if getattr(args, "undo", False):
            ok, msg = C.unack(cid)
        else:
            ok, msg = C.ack(cid, by=getattr(args, "by", "") or "human")
        print("%-8s %s" % ("ok" if ok else "error", msg))
        return 0 if ok else 2

    if act == "influence":
        # 记忆影响披露的**机械核验**（2026-11-19）：卡片 id 出现在 assistant 回复里 = 被引用过。
        # 披露如果只有"请说明"没有核验，就是口号；反过来说，没有 id 也核验不了 —— 两者同批做的。
        rows, _ = C.load()
        if not rows:
            print("卡片库是空的（先 cards add）", file=sys.stderr)
            return 2
        ids = [c.get("id") for c in rows if c.get("id")]
        try:
            store = Store(args.db)
        except Exception as exc:                        # noqa: BLE001
            print("打不开索引库：%s（先跑一次 am ingest）" % exc, file=sys.stderr)
            return 2
        strict = not getattr(args, "loose", False)
        try:
            stats = store.id_mentions(ids, since=getattr(args, "since", "") or "",
                                      strict=strict)
        finally:
            store.close()
        used = [(cid, s) for cid, s in stats.items() if s["count"]]
        used.sort(key=lambda x: (-x[1]["count"], x[0]))
        since = getattr(args, "since", "") or ""
        print("引用统计%s：卡片 %d 张 ｜ 被引用 %d 张 ｜ 未引用 %d 张"
              % (("（since %s）" % since) if since else "", len(ids), len(used),
                 len(ids) - len(used)))
        print("口径：%s" % ("只认回答开头的披露句式「受 agent-memory 卡片 c-xxxxxxxx 影响」"
                          "（`--loose` 看任意提及，仅用于排查）"
                          if strict else
                          "任意提及（含讨论机制时的顺带提到，**不能当引用率**）"))
        by_id = {c.get("id"): c for c in rows}
        for cid, s in used:
            claim = (by_id.get(cid) or {}).get("claim") or ""
            print("- %s  %d 次 ｜ 最近 %s ｜ %s"
                  % (cid, s["count"], (s["last_ts"] or "?")[:10],
                     (s["last_session"] or "")[:40]))
            print("    %s" % claim[:72])
            if s.get("truncated"):
                print("    （命中已达单卡上限，数字是下限）")
        if not used:
            print("\n还没有任何卡被引用过。两点说明：")
            print("  ① 卡片 id 是最近才写进注入块的 —— 更早的会话里模型看不到 id，统计从此刻开始；")
            print("  ② `ingest` 之后的新会话才在库里，刚聊完的还没采。")
        return 0

    if act == "backup":
        baks = C.backups()
        if not baks:
            print("还没有快照（任何一次写操作都会自动留一份）")
            return 0
        print("快照 %d 份（旧 → 新，最多留 %d 份）：" % (len(baks), C.MAX_BACKUPS))
        for b in baks:
            try:
                n = len(C.load(b)[0])
            except Exception:                          # noqa: BLE001
                n = -1
            print("  %-28s %s 张" % (os.path.basename(b), "?" if n < 0 else n))
        return 0

    if act == "restore":
        if not args.yes:
            print("restore 会覆盖当前卡片文件，需加 --yes 确认（当前文件也会先留一份快照，可反悔）",
                  file=sys.stderr)
            return 2
        ok, msg = C.restore(args.text or "")
        print("%-8s %s" % ("ok" if ok else "error", msg))
        return 0 if ok else 2

    if act == "import":
        if args.mode == "replace" and not args.yes:
            print("replace 会整表替换，需加 --yes 确认", file=sys.stderr)
            return 2
        stats, warns = C.import_from(args.src, mode=args.mode)
        print("导入（%s）：库内共 %d 张 ｜ 新增 %d ｜ 更新 %d ｜ 跳过 %d"
              % (stats["mode"], stats["total"], stats["added"], stats["updated"],
                 stats["skipped"]))
        for w in warns:
            print("  warn: %s" % w)
        return 0

    if act == "distill":
        # SOP 结晶（改进清单第 8 条）：把某 topic 的卡确定性重排成一份草稿。
        # 零 LLM；产出是**草稿**，人工审阅后才该落 docs/。
        topic = (getattr(args, "topic", "") or "").strip()
        if not topic:
            print("distill 需要 --topic（要汇总哪个话题）", file=sys.stderr)
            return 2
        dst = getattr(args, "dst", "") or ""
        r = C.distill(topic, dst=dst)
        if not r["cards"]:
            print("topic=%r 下没有卡片" % topic, file=sys.stderr)
            return 0
        if dst:
            print("已生成草稿 -> %s（%d 张卡，证据 %d 条 / 带锚点 %d）"
                  % (dst, r["cards"], r["evidence"], r["anchored"]))
            print("\n下一步：人工审阅；确认无误再提交到 docs/。草稿不是结论。")
        else:
            print(r["text"])
            print("--- 统计: 卡片 %d ｜ 证据 %d（带锚点 %d）" % (r["cards"], r["evidence"], r["anchored"]))
        if r["anchored"] < r["evidence"]:
            print("注意：有 %d 条证据无 msg 锚点，跨机不可回查。" % (r["evidence"] - r["anchored"]))
        return 0

    if act == "conflicts":
        # 矛盾检测（改进清单第 6 条）：只报告，不自动合并/改写。
        # 判据：同 topic + 都还活着 + 无既有推翻关系 + 剥掉否定与情态后 2-gram 相似度达标 + 极性不同。
        pairs = C.detect_conflicts(topic=getattr(args, "topic", "") or "",
                                   min_sim=getattr(args, "min_sim", 0) or 0.6)
        print("=== 疑似矛盾（只报告，不改任何东西）===")
        print("  阈值：剥离否定/情态后 2-gram 相似度 ≥ %.2f" % (getattr(args, "min_sim", 0) or 0.6))
        if not pairs:
            print("  未发现疑似矛盾。")
            return 0
        print("  发现 %d 对：" % len(pairs))
        for p in pairs:
            print("  --- 相似度 %.2f ｜ topic=%s" % (p["similarity"], p["topic"] or "(无)"))
            print("      A %s [%s]%s" % (p["a"]["id"], p["a"]["status"],
                                          "（含否定）" if p["a"]["neg"] else ""))
            print("        %s" % (p["a"]["claim"] or "")[:90])
            print("      B %s [%s]%s" % (p["b"]["id"], p["b"]["status"],
                                          "（含否定）" if p["b"]["neg"] else ""))
            print("        %s" % (p["b"]["claim"] or "")[:90])
        print("\n裁决方式：确认后 `cards supersede --old-id <旧> --new-id <新>`，"
              "或改一张卡的 status。系统不会自动改。")
        return 0

    if act == "log":
        # 审计日志（改进清单第 5 条）：谁在何时改了什么，改前/改后哈希。
        # 巡检会自动改状态、多个 agent 都可能写卡 —— 没有它追不到"谁改的"。
        cid = (args.text or "").strip()
        rows = C.read_audit(card_id=cid, limit=args.limit or 30)
        if not rows:
            print("没有审计记录%s（文件 %s）"
                  % (("（card_id=%s）" % cid) if cid else "", C.audit_file()), file=sys.stderr)
            return 0
        print("=== 卡片审计（新 → 旧，显示 %d 条%s）==="
              % (len(rows), ("，card_id=%s" % cid) if cid else ""))
        for r in rows:
            arrow = ""
            if r.get("before") or r.get("after"):
                arrow = "  [%s → %s]" % (r.get("before") or "—", r.get("after") or "—")
            print("  %s  %-14s %-12s %s%s"
                  % (r.get("at", ""), r.get("op", ""), r.get("by", ""),
                     r.get("card_id") or "(无)", arrow))
            if r.get("note"):
                print("      %s" % r["note"])
        print("\n文件：%s" % C.audit_file())
        return 0

    if act == "audit":
        # 前提巡检（改进清单第 4 条）：把"结论里提到的路径现在还在不在"交给 premises 判，
        # 命中失效前提的卡置 stale（不删、留痕、可回滚）。默认只报告，--apply 才写。
        import premises as P
        live = P.live_dirs_by_entity(args.db)
        d2e = P.dir_entity_map(args.db)

        def _analyze(card):
            text = " ".join([card.get(k) or "" for k in ("claim", "scope")] +
                            [(e.get("quote") or "") for e in card.get("evidence") or []])
            out = []
            for p in P.extract_paths(text):
                if not P.is_project_asset(p):
                    continue
                verdict, note = P.judge_path(p, live, d2e)
                out.append({"path": p, "verdict": verdict, "note": note})
            return out

        rep = C.audit_cards(_analyze, apply=getattr(args, "apply", False))
        print("=== 卡片前提巡检 ===")
        print("  卡片 %d 张 ｜ 扫描 %d 张 ｜ 无可验证前提 %d 张 ｜ 前提失效 %d 张%s"
              % (rep["cards"], rep["scanned"], rep["no_paths"], len(rep["candidates"]),
                 ("（**已置 stale**）" if getattr(args, "apply", False) else "（仅报告，未改动）")))
        for item in rep["candidates"]:
            print("  %s %s" % ("✗" if getattr(args, "apply", False) else "·",
                               (item["claim"] or "")[:60]))
            print("      %s" % item["reason"])
        if rep["candidates"] and not getattr(args, "apply", False):
            print("\n要写回（置 stale + 记录 status_history）请加 --apply。")
        for w in rep["warnings"]:
            print("  warn: %s" % w)
        return 0 if not rep["candidates"] else (0 if getattr(args, "apply", False) else 0)

    if act == "validate":
        all_cards, warns = C.load()
        bad = 0
        for c in all_cards:
            errs = C.validate(c)
            if errs:
                bad += 1
                print("非法 %s：%s" % (c.get("id") or "(无 id)", "；".join(errs)))
        print("校验 %d 张 ｜ 非法 %d 张 ｜ 读警告 %d 条" % (len(all_cards), bad, len(warns)))
        for w in warns:
            print("  warn: %s" % w)
        rc = 1 if bad else 0

        # --verify：把 quote 回到原文核对（格式校验之外的事实校验）。
        # 单独开关的理由：要查会话库、比格式校验慢；且跨机卡片本来就无法回查，
        # 所以默认不做，做了要如实统计（无锚点不计入分母）。
        if getattr(args, "verify", False):
            store = Store(args.db)
            try:
                def _fetch(mid):
                    m = store.message_by_id(mid)
                    return m["text"] if m else None
                vr = C.verify_cards(_fetch)
            finally:
                store.close()
            print()
            print("=== 证据核验（回到原文）===")
            print("  卡片 %d 张 ｜ 证据 %d 条：可验证 %d ／ 不符 %d ／ 消息已不在 %d ／ 无锚点 %d"
                  % (vr["cards"], vr["evidence"], vr["verified"], vr["mismatch"],
                     vr["missing_msg"], vr["no_anchor"]))
            print("  **可验证率 %.0f%%**（分母 = 可验证+不符+消息已不在；无锚点不计）"
                  % (vr["verifiable_ratio"] * 100))
            for item in vr["bad_cards"]:
                d = item["detail"]
                print("  x %s %s" % (item["id"] or "(无 id)", (item["claim"] or "")[:50]))
                for i, why in d["mismatch"]:
                    print("      evidence[%d] %s" % (i, why))
                if d["missing_msg"]:
                    print("      evidence%s 的 msg 在本机库里找不到（跨机或库被重建）"
                          % d["missing_msg"])
            if vr["mismatch"] or vr["missing_msg"]:
                rc = 1

        # --machine：机器状态类卡的**活体检查**（2026-11-18）。
        # 这类卡说的是"本机现在是什么样"（哪个 exe 在 PATH、目录在不在），
        # 用聊天消息锚定它等于把"可复核"降级成"有人说过"；活体检查才是它的证据。
        # 只跑声明式检查（file_exists / dir_exists / path_env_contains），不执行任意命令。
        if getattr(args, "machine", False):
            mr = C.verify_machine_cards(all_cards)
            print()
            print("=== 机器状态活体检查（声明式，不执行命令）===")
            print("  卡片 %d 张 ｜ 带检查 %d ｜ 通过 %d ｜ 失败 %d ｜ 无检查 %d"
                  % (mr["cards"], mr["with_checks"], mr["passed"], mr["failed"],
                     mr["unchecked"]))
            for item in mr["items"]:
                mark = "ok  " if item["ok"] else "FAIL"
                print("  [%s] %s %s" % (mark, item["id"], (item["claim"] or "")[:52]))
                for chk in item["checks"]:
                    print("         %s %-18s %s ｜ %s"
                          % ("✓" if chk["ok"] else "✗", chk["kind"],
                             chk["target"], chk["detail"]))
            if mr["failed"]:
                print("  提示：检查失败的卡说明「本机现在不是这个状态」——"
                      "用 `am cards supersede` 更新它，别放着误导。")
                rc = rc or 1
        return rc

    # stats
    s = C.stats()
    print("=== 结论卡 ===")
    print("  文件 %s" % s["file"])
    print("  共 %d 张 ｜ " % s["total"] +
          " ｜ ".join("%s %d" % (k, v) for k, v in s["by_status"].items()))
    if s["topics"]:
        print("  话题：" + "、".join("%s(%d)" % (k, v) for k, v in
                                  sorted(s["topics"].items(), key=lambda x: -x[1])[:8]))
    for w in s["warnings"]:
        print("  warn: %s" % w)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="agent-memory", description="多 agent 会话索引")
    ap.add_argument("--db", default=None, help="索引库路径（默认由 paths.DB_FILE 决定）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="采集并写入索引")
    p.add_argument("--agent", default="all",
                   choices=["all", "dsh", "codex", "claude", "hermes",
                            "workbuddy", "codebuddy"])
    p.add_argument("--limit", type=int, default=0, help="每个来源只取前 N 个会话（调试）")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("search", help="检索（可先查 docs 编译层）")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--trigram", action="store_true", help="用 trigram 索引（中文短语更准）")
    p.add_argument("--peer", default="",
                   help="只检索该工作区身份（git origin 派生，子串匹配，如 demo-base）")
    p.add_argument("--docs-first", action="store_true",
                   help="先查 docs 层（编译产物），命中就不查会话；未配置根时自动跳过")
    p.add_argument("--with-sessions", action="store_true",
                   help="配合 --docs-first：docs 命中后仍继续查原始会话")
    p.add_argument("--max-chars", type=int, default=0,
                   help="docs 层总字符预算（0=不限）；借鉴 TencentDB Agent Memory 的召回预算")
    p.add_argument("--timeout-ms", type=int, default=0,
                   help="docs 层扫描超时毫秒（0=不限）；超时如实提示并回落会话")
    p.add_argument("--cross-message", action="store_true",
                   help="会话层：多词未在同一条消息共现时，按同一会话相邻 ≤3 条消息再找一次")
    p.add_argument("--explain", action="store_true",
                   help="报告短中文词的子串命中缺口（FTS 按 token 匹配会漏；多花约 0.2 秒全表扫描）")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("docs", help="docs 层（编译产物）：查看配置的根与命中情况")
    p.add_argument("action", nargs="?", default="stats", choices=["stats"])
    p.set_defaults(func=cmd_docs)

    p = sub.add_parser("peers", help="工作区身份（peer）列表 / 补算")
    p.add_argument("--backfill", action="store_true", help="给空 peer 的已入库会话补算（幂等）")
    p.add_argument("--top", type=int, default=20, help="显示前 N 个 peer")
    p.set_defaults(func=cmd_peers)

    p = sub.add_parser("stats", help="索引统计")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("doctor", help="环境自检（依赖 / 索引 / schema 版本 / 数据新鲜度）")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("topics", help="话题归并（实体 × 意图）")
    p.add_argument("--build", action="store_true", help="重算全部会话的标签（幂等）")
    p.add_argument("--pairs", type=int, default=2, help="组合视图的最小会话数")
    p.add_argument("--top", type=int, default=15, help="每节显示前 N 条")
    p.add_argument("--entity", default="", help="列出该实体下的会话时间线")
    p.add_argument("--intent", default="", help="配合 --entity 限定意图")
    p.set_defaults(func=cmd_topic)

    p = sub.add_parser("init", help="首次安装/换机：生成本机实体表与注入目标")
    p.add_argument("--force", action="store_true", help="覆盖重建（会丢手工加的别名）")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("timeline", help="演变时间线（某话题的结论按时间排 + 冲突）")
    p.add_argument("entity", help="实体名，如 demo-kb / PageIndex")
    p.add_argument("intent", nargs="?", default="", help="可选：意图标签")
    p.add_argument("--per", type=int, default=3, help="每会话最多显示几条结论")
    p.add_argument("--out", default="", help="落盘路径（markdown）")
    p.set_defaults(func=cmd_timeline)

    p = sub.add_parser("premises", help="前提校验（三路判定：活 / 迁移 / 真失效）")
    p.add_argument("entity", help="实体名，如 demo-kb")
    p.add_argument("--limit", type=int, default=0, help="只查前 N 个会话")
    p.add_argument("--top", type=int, default=10, help="每类显示前 N 条")
    p.set_defaults(func=cmd_premises)

    p = sub.add_parser("procedure", help="procedure 挖掘（只读）：修复对 / 仪式")
    p.add_argument("--kind", choices=("all", "repair", "ritual"), default="all",
                   help="挖掘哪一类（默认 all）")
    p.add_argument("--top", type=int, default=10, help="每类显示前 N 条")
    p.add_argument("--repair-min-sessions", type=int, default=1,
                   help="修复对的最小跨会话数（默认 1：修复知识多为一次性）")
    p.add_argument("--ritual-min-sessions", type=int, default=3,
                   help="仪式的最小跨会话数（默认 3：太少不算每次都做）")
    p.add_argument("--judge", action="store_true",
                   help="给每条挂前提判定（🟢活 / 🟡项目已迁移 / 🔴前提已失效 / ⚪无可验证）")
    p.add_argument("--refresh-cache", action="store_true",
                   help="只重建 procedure 缓存（日更调用；约 12 秒）")
    p.set_defaults(func=cmd_procedure)

    # 人类诊断（WASP-Mem：agent 组织、人类诊断；一轮人诊断 > 四轮自判）
    p = sub.add_parser("diagnose", help="人类诊断：list 待诊断候选 / add 记录诊断 / show 已诊断")
    p.add_argument("action", choices=("list", "add", "show"))
    p.add_argument("id", nargs="?", default="", help="add 时的候选 id（来自 diagnose list）")
    p.add_argument("text", nargs="*", help="add 时的诊断原话（引号包住）")
    p.add_argument("--top", type=int, default=10, help="list/show 显示前 N 条")
    p.set_defaults(func=cmd_diagnose)

    # 跨宿主接入（Codex / Claude Code）：装/撤"开工先读 context.md"的指令片段
    p = sub.add_parser("install-host",
                       help="给 codex/claude 安装「开工读 context.md」指令片段（可 --dry-run / --revert）")
    p.add_argument("host", choices=("codex", "claude"))
    p.add_argument("--project", default="", help="装到项目级 <目录>/AGENTS.md|CLAUDE.md（默认全局）")
    p.add_argument("--revert", action="store_true", help="撤销：只删标记块，不动其它内容")
    p.add_argument("--dry-run", action="store_true", help="只预览将写入的块，不落盘")
    p.add_argument("--status", action="store_true", help="只看是否已安装")
    p.set_defaults(func=cmd_install_host)

    p = sub.add_parser("inject", help="生成可粘贴进会话开头的「环境事实」块（可查过期）")
    p.add_argument("entity", nargs="?", default="", help="实体名，如 demo-kb（--check 时可省）")
    p.add_argument("--max-chars", type=int, default=1400, help="字符预算（默认约 500 token）")
    p.add_argument("--write", default="", help="写入文件（如 .agent-memory/context.md）")
    p.add_argument("--cards", action="store_true",
                   help="同时注入「相关结论卡」（有界：--max-cards / --card-chars，截断会明说）")
    p.add_argument("--procedures", action="store_true",
                   help="同时注入本项目 procedure（常用操作/修法；前提失效的不注入）")
    p.add_argument("--max-procedures", type=int, default=3, help="--procedures：最多几条（默认 3）")
    p.add_argument("--max-procedure-repairs", type=int, default=1,
                   help="--procedures：其中最多几条「修法」（默认 1，其余给常用操作）")
    p.add_argument("--procedure-chars", type=int, default=500,
                   help="--procedures：字符上限（默认 500）")
    p.add_argument("--topic", default="", help="--cards：只取该话题的卡")
    p.add_argument("--max-cards", type=int, default=5, help="--cards：最多注入几条（默认 5）")
    p.add_argument("--card-chars", type=int, default=900, help="--cards：卡片块字符上限（默认 900）")
    p.add_argument("--include-dead", action="store_true",
                   help="--cards：连「已被推翻」的卡一起注入（默认排除；它们标为 已被推翻）")
    p.add_argument("--include-acked", action="store_true",
                   help="--cards：连「已确认」的卡一起注入（默认跳过；账本由 `cards ack` 维护）")
    p.add_argument("--ack-ttl-days", type=int, default=0,
                   help="--cards：确认有效期天数（0=默认 30 天；过期后重新注入）")
    p.add_argument("--no-card-ids", action="store_true",
                   help="--cards：行尾不带卡片 id（默认带；id 是「记忆影响披露」可机械核验的锚点）")
    p.add_argument("--check", action="store_true",
                   help="只检查已有注入块是否过期（不生成）；过期退出码 1，供日更/CI/agent 自检")
    p.add_argument("--stale-days", type=int, default=0,
                   help="过期阈值天数（默认 3）")
    p.set_defaults(func=cmd_inject)

    # ── 结论卡（编译层）：把会话压成可复用断言，独立于项目目录，复制即可迁移 ──
    p = sub.add_parser("cards", help="结论卡：add/list/search/open/supersede/export/import/backup/restore/validate/stats")
    p.add_argument("action",
                   choices=["add", "list", "search", "open", "supersede", "ack", "influence",
                            "export", "import",
                            "backup", "restore", "audit", "log", "conflicts", "distill",
                            "validate", "stats"])
    p.add_argument("text", nargs="?", default="",
                   help="search 的关键词 / open 的卡 id / restore 的快照文件名 / 省略")
    p.add_argument("--claim", default="", help="add：一句话断言")
    p.add_argument("--status", default="",
                   help="add 默认 alive；list/search 不填则不限。"
                        "取值 alive/stale/dead/unverified（也认中文：仍成立/前提已变/已被推翻/未验证）")
    p.add_argument("--topic", default="", help="add/search：话题")
    p.add_argument("--scope", default="",
                   help="add：适用范围（自由文本说明；建议必填，否则结论会被过度泛化）")
    p.add_argument("--scopes", default="",
                   help="add：结构化作用域，逗号分隔，如 \"machine,project:F:/demo-base\""
                        "（machine=全机通用；project:<路径>=仅该项目）。缺省时旧卡视为 machine")
    p.add_argument("--scope-filter", default="",
                   help="list/search：只出属于该作用域的卡（machine 或 project:<路径>，项目按路径前缀匹配）")
    p.add_argument("--ev", action="append", default=[],
                   help="add：证据，格式 agent|YYYY-MM-DD|原文摘录[|session][|msg_id]，可重复；"
                        "msg_id 是 messages.id，加了才能用 cards open 回原文全文")
    p.add_argument("--supersedes-card", action="append", default=[],
                   help="add：本卡推翻了哪张（可重复）")
    p.add_argument("--also-supersede", default="",
                   help="add：顺手把这张旧卡标成 dead（等价于随后再跑 supersede）")
    p.add_argument("--filter", default="", help="list：子串过滤")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--old-id", default="", help="supersede：被推翻的卡")
    p.add_argument("--new-id", default="", help="supersede：新的卡")
    p.add_argument("--dst", default="", help="export：目标路径（--md 时是 .md）")
    p.add_argument("--md", action="store_true",
                   help="export：导出人类可读 Markdown（按 topic 分组，可被 docs 层检索）")
    p.add_argument("--src", default="", help="import：源 JSONL 路径")
    p.add_argument("--mode", default="merge", choices=["merge", "replace"],
                   help="import：merge（按 id 取较新）/ replace（整表替换，需 --yes）")
    p.add_argument("--yes", action="store_true", help="import --mode replace 的确认")
    p.add_argument("--min-sim", type=float, default=0.6,
                   help="conflicts：相似度阈值（默认 0.6；调高更严、调低更多候选）")
    p.add_argument("--apply", action="store_true",
                   help="audit：把命中失效前提的卡置 stale 并记录 status_history（默认只报告）")
    p.add_argument("--verify", action="store_true",
                   help="validate：把证据 quote 回到原文核对（查会话库；不符则退出码 1）")
    p.add_argument("--machine", action="store_true",
                   help="validate：跑机器状态类卡的声明式活体检查（file_exists/dir_exists/path_env_contains）")
    p.add_argument("--verify-spec", action="append", default=[], dest="verify_spec",
                   help="add：给卡加活体检查，写法 'dir_exists:C:\\path'（可重复）")
    p.add_argument("--by", default="human",
                   help="ack：谁确认的（写进账本，便于事后追责/多 agent 区分；默认 human）")
    p.add_argument("--undo", action="store_true", help="ack：撤销确认（下轮注入重新出现）")
    p.add_argument("--show", action="store_true",
                   help="ack：列出账本及每条现状态（免打扰/内容已变/过期/卡已不存在）")
    p.add_argument("--prune", action="store_true",
                   help="ack：清掉已失效的账本条目（内容已变/过期/卡已删除）")
    p.add_argument("--ttl-days", type=int, default=0, dest="ttl_days",
                   help="ack/inject：确认有效期天数（0=默认 %d 天）" % 30)
    p.add_argument("--since", default="",
                   help="influence：只统计该日期起的引用（YYYY-MM-DD）")
    p.add_argument("--loose", action="store_true",
                   help="influence：按「任意提及」统计（含讨论机制时的顺带提到，不能当引用率）")
    p.set_defaults(func=cmd_cards)

    args = ap.parse_args(argv)
    # 索引库路径统一由 paths.DB_FILE 决定（env 优先），消除"两个真相源"：
    # 否则 AGENT_MEMORY_HOME 指向别处时，库写在代码目录而词典读 env 目录，实体表与库分家。
    if not getattr(args, "db", None):
        try:
            from paths import DB_FILE
            args.db = DB_FILE
        except Exception:                                  # noqa: BLE001
            args.db = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "index", "sessions.db")
    # 实体同义词表来自数据文件（每台机器不同），启动时载入；缺文件则退化为"目录名即实体"
    try:
        from topics import load_entities
        load_entities()
    except Exception:                                      # noqa: BLE001
        pass
    # doctor 等命令返回退出码；其余命令返回 None（视为 0）
    _rc = args.func(args)
    sys.exit(0 if _rc is None else _rc)


if __name__ == "__main__":
    main()
