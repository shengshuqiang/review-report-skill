# 审查报告 Skill

为代码改动生成可离线打开、逐文件逐代码块核对的单文件 H5 审查报告。包含完整 diff、真实行号、改动必要性、合理性、功能影响、问题复现路径与验证边界。

## 快速安装

需要 Node.js/npm（运行安装工具）、Git 和 Python 3（运行报告脚本，使用标准库，无需 pip 安装）。

在终端执行，给 Codex 全局安装主技能和两个必要依赖：

```bash
npx skills add shengshuqiang/review-report-skill --skill '*' --global --agent codex --yes
```

Cursor 或 Claude Code 可把 `codex` 换成 `cursor` 或 `claude-code`。也可交互选择目标工具：

```bash
npx skills add shengshuqiang/review-report-skill --skill '*' --global
```

请安装全部三个技能；只安装 `review-report` 会缺少审查与问题说明依赖。安装参数参考 [skills CLI 官方文档](https://github.com/vercel-labs/skills#install-a-skill)。安装完成后新开会话；已有会话未发现技能时重启宿主。

## 使用

在业务仓库对应会话中输入：

```text
使用 $review-report 生成当前改动的审查报告，保存到仓库外的目录。
```

也支持 staged、unstaged、指定文件或明确 base/target 的 commit range。技能保持自动选择开启，使用“审查报告”也可触发。

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
