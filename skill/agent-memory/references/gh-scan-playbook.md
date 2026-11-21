# 同类项目情报：快扫流程（包内版）

> 配套脚本：`scripts/gh_scan.py`（仅标准库，无需安装依赖）。
> 仓库里还有一份面向开发者的版本（含仓库内路径），两者内容对应、**本文件是装包后该看的那份**。

## 一、三步法：机器做前两步，人做第三步

```
发现（1~3 个查询词） → 采数（脚本：指标 + README 事实命中 + 与我方交集） → 判定（人：分级 + 结论）
```

**核心分工**：脚本只回答"**是什么数字、README 里能 grep 到什么**"，不回答"它到底行不行"。

## 二、命令速查

```powershell
# A. 发现模式：捞一批同类（30 秒~2 分钟，取决于抓多少 README）
python scripts\gh_scan.py `
    --query "topic:agent-memory" `
    --query "agent memory in:name,description" `
    --limit 12 `
    --out "$env:TEMP\ghscan\scan"

# B. 点名模式：评测/文章里看到的项目，直接采（省一次 search）
python scripts\gh_scan.py --repo owner/name --repo owner2/name2 `
    --out "$env:TEMP\ghscan\scan"

# C. 只看跟我重叠多的（过滤噪音）
python scripts\gh_scan.py --query "topic:agent-memory" --only-overlap 4 `
    --out "$env:TEMP\ghscan\scan"

# D. 跨期比增速：给上次快照，自动算 Δ stars
python scripts\gh_scan.py --query "topic:agent-memory" `
    --prev "$env:TEMP\ghscan\scan-2026-11-19.json" --out "$env:TEMP\ghscan\scan-new"
```

**必须显式给 `--out`**：不给时默认落到脚本旁的 `docs` 子目录（其下 `research/repos/<日期>.json|.md`），会在装包目录里凭空多出一层目录，
污染技能目录。`--out` 会写两个文件：`<前缀>.json`（快照）+ `<前缀>.md`（人看的表）。
目录不存在会被自动创建。

**关于 GitHub token**：不设 `GITHUB_TOKEN` 也能跑，但走**匿名限流**（每小时额度很低，密集查询会被限流甚至失败）；
把 token 设进环境变量即可提到 5000/h。高频使用建议设：
```powershell
$env:GITHUB_TOKEN = "<你的 token>"   # 只需要 public repo 读权限
```

## 三、判定矩阵（人做的那一步）

| 档 | 看什么 | 判据 |
|---|---|---|
| **真同行** | 客户端交集（表已算）+ desc/topics 是否同一件事 | 交集 ≥4 且 desc 明确是"跨 agent 记忆 / 上下文" |
| **间接**（框架/平台） | 平台型项目 | 能用但不是我这条路；只参考机制 |
| **噪音** | 交集 0~2，或 desc 是书 / 数据库 / 大杂烩 | 直接丢 |

对"真同行"逐个按证据分级（**A** 官方一手 / **B** 二手或推算 / **C** 未核实），未验证的单列。

## 四、四条纪律（都是实跑踩出来的）

1. **关键词命中 ≠ 事实**：README 能 grep 到 ≠ 它真做了。判定必须回一手材料。
2. **否定句会把方向说反**：`does not upload …` 裸 grep `upload` 会得出相反结论。脚本已做否定检测，
   命中标「否定句?」，看到这个标记必须人工读原句。
3. **发现模式一定有噪音**：查询词越泛噪音越多（实测一次 31 个里约 5 个是数据库/书/大杂烩）。
   过滤顺序：交集 → desc/topics → 人工扫一眼，别指望查询词精准。
4. **stars 不能跨品类比**：套件类项目与垂直小工具放同一列会误导；要比就看同品类内部的 **Δ**（`--prev` 干这个）。

## 五、未验证清单（C 级，不得当事实）

- 脚本给出的任何"判断"都只基于 desc/topics/README 关键词，**未读代码、未安装、未实测**；
- stars 是快照（JSON 里 `generated_at`），跨文档引用必须带日期；
- 搜索覆盖面有限：几个查询词捞不到全部同类（中文生态基本没覆盖）。
