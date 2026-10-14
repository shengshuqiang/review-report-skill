# 数据与命令合同

填审查数据或适配已有报告时读取。本包两步脚本只执行 Git 读取与 JSON → HTML 转换；人工/Agent 完成语义判断。

## 范围与统计

| scope | 改前 | 改后 | untracked |
| --- | --- | --- | --- |
| worktree（默认） | base，默认 HEAD | 当前磁盘 | 纳入 |
| staged | base，默认 HEAD | Git index | 不纳入 |
| unstaged | Git index | 当前磁盘 | 不纳入 |
| range | 指定 base commit | 指定 target commit | 不纳入 |

`--base` 解析为真实 commit；unstaged 的实际 base 固定是 index，不接受自定义 base。`--target` 仅 range 使用。`--path` 为仓库相对 literal pathspec，可重复；`--exclude` 为 Python fnmatch 路径模式，可重复，排除路径和模式进入快照。不要用 shell 通配展开代替这两个参数。

新增文件改前行数 0，删除文件改后行数 0；未跟踪文件相对空文件生成 diff。有无末尾换行均按文件真实行数计，缺末尾换行保留 marker。rename 保留旧路径；仅 mode 修改无 hunk，但显示完整元信息。二进制 patch 不嵌入二进制 payload，行数和增删为 null，页面显示未知并声明边界。

`layers` 是采集时三层文件清单，不代表 range 纳入了本地改动。staged/unstaged 同文件最终 aggregate patch 可能抵消；范围由 `scope/base/target` 决定。跨范围复用指纹或审查解释不合法。

## snapshot.json

由 `collect_diff.py` 生成，**不要手动改源码、行号、统计或指纹**。

```json
{
  "version": 1,
  "repo": "/actual/repo",
  "branch": "branch",
  "head": "full commit hash",
    "base": "resolved commit hash / index (unstaged)",
  "target": "worktree / index / resolved commit hash",
  "scope": "worktree",
  "captured_at": "UTC ISO time",
  "fingerprint": "SHA256 of canonical snapshot excluding timestamp and fingerprint",
  "files": [{
    "id": "F001", "path": "src/file.ts", "old_path": null,
    "status": "modified", "bucket": "业务", "category_basis": "路径分类规则；待人工核对",
    "before_lines": 100, "after_lines": 101, "added": 2, "removed": 1,
    "binary": false, "patch": "complete unified diff",
    "hunks": [{"id": "F001-H01", "header": "@@ -1,3 +1,4 @@", "lines": [" context", "-old", "+new"]}]
  }]
}
```

文件类别固定 `业务/测试/配置/文档/生成物/其他`。状态包括 modified/added/deleted/renamed/binary/mode-only。`hunk.lines` 含原 prefix，**不含 @@ header**。指纹包含完整 patch 与内容 SHA，空 diff 也明确展示。

## review.json

采集器生成空表；从实际审查填非空字段。字段值均为普通字符串，HTML/Markdown 不执行。`snapshot_fingerprint` 必须与 snapshot 完全一致。语义内容不能用“已检查”“测试绿”代替。

顶层：`version=1`、`snapshot_fingerprint`、`title`、`goal`、`non_goals` 字符串数组、`summary` 整体判断、`files`、`issues`、`sections`、`validations`。

`files` 用 file ID 做 key，每个文件：

| 字段 | 含义 |
| --- | --- |
| bucket/category_basis | 可选：人工修正分类与依据 |
| purpose | 文件用途/改前职责 |
| change | 改动位置与改动用途，含稳定符号 |
| position | 可选：单独填写方法/组件/稳定符号；未填写以 change 的稳定符号说明为准，不用 @@ 代替 |
| why | 对应独立目标与必须改变已有功能的原因 |
| without | 保留其他改动而移除此文件变化，会失去什么 |
| necessity | 必要/有条件必要/冗余/过度改动/证据不足 |
| reasonableness | 合理性、owner/边界/消费者与最小充分依据 |
| loss | 否/是（按需求移除）/是（移除意图待确认）/是（非预期丢失）/高疑似/证据不足/不适用（新增文件） |
| risk | 独立 P? 引用及未验证边界；无确认隐患也保留真实缺口 |
| risks | 可选：未包含在 issues 的逐条证据缺口或补充说明，单独编号；不要重复已有 P?。可独立修正/复现的问题应进入 issues，risk 保留总体边界 |
| pair | 原生文件另一端结论 + 文件/稳定符号 + 最短依据 |
| hunks | 每个 hunk ID 的分段解释 |

每个 hunk：`title`、`change`、`necessity`（同固定枚举）、`reasonableness`、`without`、`evidence` 均非空，`issues` 为可选 P? 数组。`evidence` 指真实行号/符号与能证明什么；该 hunk ID 是代码证据锚点，问题可直接关联。必要性与合理性是不同判断，不互相替代。

问题的关键代码用可选 `focus` 选择真实行，而不是只给整块红绿颜色：

```json
"focus": [{ "side": "new", "line": 42, "marker": 1, "explanation": "E1：这一行把状态放到共享 owner，支持 P1；不证明设备表现" }]
```

side 为 `old/new`，line 为该 hunk 确实包含的正整数行号，marker 为块内唯一正整数。删除行用 old，新增行用 new，上下文可选任一侧；每个标记写“该行证明什么”并关联 E?/P?。渲染器校验坐标，显示聚焦边框/背景、数字标记和邻近解释；不改原始 patch 或行号。无关键证据点可留空，不能假造坐标。

每个 issue：

```json
{
  "id": "P1", "priority": "P1", "title": "用户能看懂的问题标题",
  "evidence": "代码实锤", "frequency": "频率待确认",
  "locations": [{"file_id": "F001", "line": 42, "symbol": "actualSymbol"}],
  "condition": "页面入口、触发条件与关键原因",
  "user_problem": "用户在哪里、做什么、看到什么异常；未运行标代码推导",
  "reason": "理解问题必需的关键原因，保留证据边界",
  "evidence_refs": ["E1", "F001-H01"],
  "invariant": "破坏的契约/不变量，关联代码证据 ID",
  "impact": "用户实际或可能观察到什么", "scope": "用户、平台与版本范围",
  "recovery": "是否恢复以及条件",
  "repro": {
    "persona": "用户画像", "preconditions": "版本/数据/页面条件",
    "steps": ["打开具体页面", "执行可观察的用户动作"],
    "actual": "已观察结果；未运行必须标按代码推导",
    "expected": "来自需求/契约的预期", "observations": ["页面状态或日志观察点"],
    "type": "候选复现路径/待验证", "status": "未执行",
    "boundary": "以下为代码推演路径，设备运行态未验证"
  },
  "suggestion": "最小修正或补证据动作"
}
```

证据等级固定 `运行实锤/测试实锤/代码实锤/高疑似/猜测/待确认`；频率固定 `稳定复现/偶现/频率待确认`。priority 为 P0/P1/P2/P3，与唯一 issue ID 分开。`repro.type` 为 `已执行` 或 `候选复现路径/待验证`；稳定测试不自动成为真实用户稳定复现。

问题必须依 `plain-language` 填用户化表现与步骤。受控测试输入/方法调用写证据，不伪装成设备操作。location 行号为真实当前行号，删除行标明基线；无法定位不猜测，line=null 并说明缺口。源码关键行证据可以使用同 hunk 的 `issues` 字段绑定 P?。

`user_problem`、`reason`、`evidence_refs` 是问题说明的可选显式字段；首次填写推荐提供，旧数据未提供时分别使用 repro.actual、condition，并保留 hunk 关联。E?/F?-H? 必须指向实际证据和对应解释，不凭空生成编号。

## sections：语义审查表

每项 `{ "title": "表名", "headers": ["列名"], "rows": [["单元格"]] }`。根据 `file-change-review` 报告合同保留表名和列；不得填空或用无关验证替代：

1. 当前改动影响范围与潜在隐患
2. 基线、约束与验证矩阵
3. 最小必要改动基线（目标依据、当前目标、最小充分链路、非目标）
4. 最小必要改动门禁（五项）
5. 功能点前后差异
6. 文件功能影响与丢失
7. 冗余/过度改动
8. 用户视角验证（各 F 的前提、动作、改前/改后、观察、状态；S→I→Oracle）
9. 待确认改动与验证边界

允许在有同类机械文件时列全路径合并；所有文件仍须覆盖。无冗余写“未发现确认的冗余或过度改动”，有待确认项另列。无用户直接变化仍写用户预期不变与验证依据。

`validations` 每项 `{ "name", "result", "layer", "oracle", "boundary" }`，分别写执行结果、源码/Mock/单测/契约/编译/设备/后台层级、实际判定标准、不能证明什么。未执行可显式列未验证，不能虚构命令或结果。生成器不会执行业务检查。
