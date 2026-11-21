"""闸门：`.ps1` / `.cmd` / `.bat` 必须**纯 ASCII**（PowerShell 5.1 的 GBK 解码坑）。

## 为什么必须机器检（2026-11-17 第三次踩坑后固化）

Windows PowerShell 5.1 读取**无 BOM 的 .ps1** 时按系统 ANSI 代码页（中文机 = GBK）解码：
文件里只要有一个中文字符，就会变成 mojibake 并**破坏解析**（报错位置与真实错因完全对不上）。
这个坑今天已经在三处复现：

1. `refresh.ps1` 里加了中文注释 → 195 个非 ASCII 字节（写完自查才发现）；
2. 早期 `probe_memorax.ps1` 写死中文路径 → 路径变成 `F:\\鍟嗘満\\work\\mx`，脚本静默跑错目录；
3. 内联 `python -c "…"` 在 PS 里被二次转义 → 连报两种误导性错误。

结论：**脚本只写 ASCII；中文一律放进 `.md` / `.json`（按 UTF-8 读）**。
本脚本就是这条规则的机器闸门，能被 pre-commit 直接调用。

## 用法

    python tools/check_ascii_ps1.py            # 扫整个仓库
    python tools/check_ascii_ps1.py --staged   # 只扫 git 暂存区里的脚本（pre-commit 用）
    python tools/check_ascii_ps1.py a.ps1 b.ps1

退出码：0 = 干净；1 = 有非 ASCII；2 = 用法/git 错误
"""
from __future__ import annotations

import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTS = {".ps1", ".cmd", ".bat"}
#: 不扫的目录（依赖、产物、草稿、本机数据）
SKIP_DIRS = {".venv", ".git", "dist", "work", "index", "__pycache__",
             ".pytest_cache", ".agent-memory", "node_modules"}


def iter_repo_scripts(root: str = REPO):
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if os.path.splitext(name)[1].lower() in EXTS:
                yield os.path.join(base, name)


def staged_scripts(root: str = REPO) -> list[str]:
    """git 暂存区里新增/修改的脚本（pre-commit 只该管这些，扫全仓会误伤旧文件）。"""
    try:
        out = subprocess.run(
            ["git", "-C", root, "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print("check_ascii_ps1: 取 git 暂存区失败：%s" % exc, file=sys.stderr)
        sys.exit(2)
    files = []
    for rel in out.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        if os.path.splitext(rel)[1].lower() in EXTS:
            files.append(os.path.join(root, rel))
    return files


def scan(path: str) -> list[dict]:
    """返回该文件的违规列表：{line, col, byte, gbk}。

    `col` 是**字节列**（不是字符列）——脚本按字节判定，按字节报位置才对得上。
    `gbk` 是这一行被 PowerShell 5.1 按 GBK 解码后的样子（证明它确实会变成乱码）。
    """
    raw = open(path, "rb").read()
    problems: list[dict] = []
    for line_no, line in enumerate(raw.split(b"\n"), 1):
        gbk_line = ""
        for col, byte in enumerate(line, 1):
            if byte < 0x80:
                continue
            if not gbk_line:
                gbk_line = line.decode("gbk", "replace").strip()[:100]
            problems.append({"line": line_no, "col": col, "byte": byte,
                             "gbk": gbk_line})
    return problems


def check_file(path: str) -> tuple[bool, list[dict], bool]:
    """返回 (是否干净, 违规列表, 是否带 BOM)。"""
    raw = open(path, "rb").read()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    problems = scan(path)
    return (not problems), problems, has_bom


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    args = list(sys.argv[1:] if argv is None else argv)
    if "--staged" in args:
        args.remove("--staged")
        targets = staged_scripts()
        scope = "staged"
    elif args:
        targets = [a if os.path.isabs(a) else os.path.join(REPO, a) for a in args]
        scope = "explicit"
    else:
        targets = list(iter_repo_scripts())
        scope = "repo"

    # NOTE: all user-facing text below is ASCII on purpose. This checker runs from a
    # git hook in a GBK console; Chinese here would print as mojibake -- the exact bug
    # this tool exists to prevent.
    bad_files = 0
    total_bytes = 0
    bom_files: list[str] = []
    for path in targets:
        if not os.path.exists(path):
            print("  skip (missing): %s" % path)
            continue
        ok, problems, has_bom = check_file(path)
        rel = os.path.relpath(path, REPO).replace("\\", "/")
        if has_bom:
            bom_files.append(rel)
        if ok:
            continue
        bad_files += 1
        total_bytes += len(problems)
        print("FAIL %s  (%d non-ASCII bytes)" % (rel, len(problems)))
        for p in problems[:6]:
            print("     line %d col %d: 0x%02X  => PS 5.1 reads it (GBK) as: %s"
                  % (p["line"], p["col"], p["byte"], p["gbk"] or "?"))
        if len(problems) > 6:
            print("     ... %d more bytes" % (len(problems) - 6))

    print("check_ascii_ps1: scanned %d script(s) [%s] | bad files %d | non-ASCII bytes %d"
          % (len(targets), scope, bad_files, total_bytes))
    for rel in bom_files:
        print("  note: %s has a UTF-8 BOM. BOM bytes are non-ASCII => violation too."
              " Strip it (PS 5.1 reads BOM-less files as GBK, so scripts stay ASCII)."
              % rel)
    if bad_files:
        print("\nFAIL: scripts must be ASCII-only. Move Chinese into .md/.json (read as UTF-8),"
              " or generate the script with Python.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
