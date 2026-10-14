# 代码审查报告 Skill｜Diff 详解、截图红圈，一站式说清楚

```bash
npx skills add shengshuqiang/review-report-skill --skill '*' --global --agent codex --yes
```

> 用**最懂描述的 H5**，把代码审查报告说清楚：图文并茂，自带代码 Diff 查看，一站式说明**改了哪里、为什么改、是否必要、有什么隐患**；还可按需结合已有的模拟器运行截图，**红圈圈选重点**，让代码改动与用户看到的结果对应起来——**重点突出，细节到位，一目了然**，并可持续集成更多能力。

## 案例一：红圈圈选重点

摘自 2026-10-08 自动化加载验收报告：iPhone 16e 模拟器 / iOS 26.2。仅展示当时画面，不代表当前状态或下方 Bridge 改动的验收结果。

![模拟器截图局部与红圈说明：①准备页未进入游戏；②分享回退页404](docs/images/simulator-red-circles.jpg)

- **① 停在准备页：** 原报告记录观察 30 秒后，仍显示“PREPARING THE PITCH”。
- **② 分享页 404：** 回退入口出现错误页，不能据此判定原游戏加载成功。

截图截取相关区域，红圈标出重点，说明与画面对应，帮助读者快速定位关键表现。

## 案例二：代码 Diff 逐文件审查

“iOS Bridge 文档来源补齐”示例展示 **7 个文件、12 个代码块**，支持查看完整 Diff、真实行号、改动必要性、合理性与验证边界，并可折叠、筛选和切换代码换行。

每处改动都回答：**改了哪里、为什么改、是否必要、有什么隐患**。代码、测试、模拟器与后台证据分别说明，未验证的部分保留缺口。

[查看完整交互式 H5 案例与文档](https://icnimsatr0zz.feishu.cn/wiki/MqmzwKgzciEZ03koKjRc4x3Cn5b#doxcnImVaG4vRnR6zBKvcTRqnIh)。GitHub README 保留介绍和截图，完整报告在飞书文档内交互查看。

## 快速安装

公开仓库：[shengshuqiang/review-report-skill](https://github.com/shengshuqiang/review-report-skill)。顶部命令给 Codex 全局安装 `review-report`，并一并安装必要依赖 `file-change-review` 和 `plain-language`。

前置条件：**Node.js/npm、Git 和 Python 3**。报告生成脚本使用 Python 标准库，无需 pip 安装。

Cursor 或 Claude Code 用户可将安装命令中的 `codex` 替换为 `cursor` 或 `claude-code`。也可交互选择目标工具：

```bash
npx skills add shengshuqiang/review-report-skill --skill '*' --global
```

请安装全部三个技能；只安装 `review-report` 会缺少审查与问题说明依赖。安装参数参考 [skills CLI 官方文档](https://github.com/vercel-labs/skills#install-a-skill)。安装完成后新开会话；已有会话未发现技能时重启宿主。

## 使用

安装完成后，在需要审查的业务仓库会话中输入：

```text
使用 $review-report 生成当前改动的审查报告，保存到仓库外的目录。
```

也支持 staged、unstaged、指定文件或明确 base/target 的 commit range。技能保持自动选择开启，使用“审查报告”也可触发。

截图可使用已有的真实运行证据；本技能自身不操作设备或服务。需要新增模拟器运行验证时，按任务授权和对应测试工具执行，再将实际证据纳入报告。

## 包含内容

| 技能 | 职责 |
| --- | --- |
| [review-report](skills/review-report/SKILL.md) | 冻结 diff、组织审查结果、生成与核验 H5 |
| [file-change-review](skills/file-change-review/SKILL.md) | 逐文件、逐 hunk 审查必要性、功能影响与证据 |
| [plain-language](skills/plain-language/SKILL.md) | 用户表现、影响、复现路径与证据标注 |

分享版只调整 `plain-language` 的相对路径，保留原有审查要求。被审查业务仓库只读；本技能不自动修复、提交、推送或发布，也不增加设备、账号或服务操作权限。

## 不使用 npm 的安装方式

```bash
git clone https://github.com/shengshuqiang/review-report-skill.git
```

把仓库 `skills/` 下的三个目录一起复制到宿主的技能目录，保持三者同级。有同名技能时先比较内容，避免覆盖自己的版本。Codex 支持用户级 `~/.agents/skills/`。

## 本地验证

```bash
python3 skills/review-report/scripts/test_report.py
node skills/review-report/scripts/test_interactions.cjs
```

前者验证隔离 Git 仓库中的采集、完整 diff 和渲染约束；后者使用受控 DOM 验证筛选、展开收起与跳转，不代表真实浏览器视觉验收。

[飞书介绍与案例原文](https://icnimsatr0zz.feishu.cn/wiki/MqmzwKgzciEZ03koKjRc4x3Cn5b)
