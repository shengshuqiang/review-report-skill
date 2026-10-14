#!/usr/bin/env python3
"""把已冻结的 diff 与人工语义审查渲染为离线 H5；不读取源码、不执行 Git。"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import html
import json
import re
import sys
from collections import Counter
from pathlib import Path


BUCKETS = ("业务", "测试", "配置", "文档", "生成物", "其他")
STATUSES = ("modified", "added", "deleted", "renamed", "binary", "mode-only")
NECESSITY = ("必要", "有条件必要", "冗余", "过度改动", "证据不足")
LOSSES = ("否", "是（按需求移除）", "是（移除意图待确认）", "是（非预期丢失）", "高疑似", "证据不足", "不适用（新增文件）")
EVIDENCE = ("运行实锤", "测试实锤", "代码实锤", "高疑似", "猜测", "待确认")
FREQUENCY = ("稳定复现", "偶现", "频率待确认")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
FILE_RE = re.compile(r"^F\d{3,}$")
ISSUE_RE = re.compile(r"^P\d+$")
PENDING = "待填写（草稿，不构成审查结论）"
REQUIRED_SECTIONS = {
    "当前改动影响范围与潜在隐患", "基线、约束与验证矩阵", "最小必要改动基线",
    "最小必要改动门禁", "功能点前后差异", "文件功能影响与丢失",
    "冗余/过度改动", "用户视角验证", "待确认改动与验证边界",
}


class ReportError(ValueError):
    """字段、冻结范围或审查覆盖不足时停止输出。"""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ReportError(message)


def field(obj: dict, name: str, kind: type, where: str, *, empty: bool = False):
    require(name in obj, f"{where}.{name}: 缺少字段")
    value = obj[name]
    require(isinstance(value, kind) and not (kind is int and isinstance(value, bool)), f"{where}.{name}: 应为 {kind.__name__}")
    if kind is str and not empty:
        require(bool(value.strip()), f"{where}.{name}: 不能为空；缺证据请明确写明")
    return value


def string_list(value, where: str) -> None:
    require(isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value), f"{where}: 应为非空字符串组成的列表（列表本身可为空）")


def nullable_count(obj: dict, name: str, where: str) -> None:
    require(name in obj, f"{where}.{name}: 缺少字段")
    value = obj[name]
    require(value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0), f"{where}.{name}: 应为非负整数或 null")


def split_patch(patch: str) -> list[tuple[str | None, list[str]]]:
    """保留元信息的顺序；每个 @@ 块独立，其余内容作为原始元信息。"""
    blocks: list[tuple[str | None, list[str]]] = []
    current_header = None
    current_lines: list[str] = []
    patch_lines = patch.split("\n")
    if patch.endswith("\n"):
        patch_lines.pop()
    for line in patch_lines:
        if HUNK_RE.match(line):
            if current_lines or current_header is not None:
                blocks.append((current_header, current_lines))
            current_header, current_lines = line, []
        elif line.startswith("diff --git ") and current_header is not None:
            # Keep a following delete/add patch's metadata out of code line counts.
            blocks.append((current_header, current_lines))
            current_header, current_lines = None, [line]
        else:
            current_lines.append(line)
    if current_lines or current_header is not None:
        blocks.append((current_header, current_lines))
    return blocks


def diff_coordinates(header: str, lines: list[str]) -> list[tuple[str, int | None, int | None, str]]:
    """只按冻结 hunk 的真实前缀生成两侧行号，供显示和聚焦校验共用。"""
    match = HUNK_RE.match(header)
    require(match is not None, "hunk.header: 无法解析真实行号")
    old_line, new_line = int(match[1]), int(match[3])
    result = []
    for line in lines:
        before = after = None
        kind = "metadata"
        if line.startswith("+"):
            after, new_line, kind = new_line, new_line + 1, "added"
        elif line.startswith("-"):
            before, old_line, kind = old_line, old_line + 1, "removed"
        elif line.startswith(" "):
            before, after, kind = old_line, new_line, "context"
            old_line, new_line = old_line + 1, new_line + 1
        result.append((line, before, after, kind))
    return result


def validate_snapshot(snapshot: dict) -> None:
    require(isinstance(snapshot, dict), "snapshot: 应为对象")
    require(snapshot.get("version") == 1, "snapshot.version: 仅支持 1")
    for name in ("repo", "branch", "head", "base", "target", "scope", "captured_at", "fingerprint"):
        field(snapshot, name, str, "snapshot")
    require(Path(snapshot["repo"]).is_absolute(), "snapshot.repo: 必须为绝对路径")
    frozen_data = {key: value for key, value in snapshot.items() if key not in {"captured_at", "fingerprint"}}
    actual_fingerprint = hashlib.sha256(json.dumps(frozen_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    require(snapshot["fingerprint"] == actual_fingerprint, "snapshot.fingerprint: 内容校验失败；快照被改动或指纹算法不匹配，必须重新采集")
    if "layers" in snapshot:
        require(isinstance(snapshot["layers"], dict), "snapshot.layers: 应为对象")
        for layer in ("staged", "unstaged", "untracked"):
            require(layer in snapshot["layers"], f"snapshot.layers.{layer}: 缺少字段")
            string_list(snapshot["layers"][layer], f"snapshot.layers.{layer}")
    for name in ("excluded", "exclusion_patterns", "pathspecs"):
        if name in snapshot:
            string_list(snapshot[name], f"snapshot.{name}")
    files = field(snapshot, "files", list, "snapshot")
    file_ids: set[str] = set()
    all_hunk_ids: set[str] = set()
    for index, item in enumerate(files):
        where = f"snapshot.files[{index}]"
        require(isinstance(item, dict), f"{where}: 应为对象")
        file_id = field(item, "id", str, where)
        require(bool(FILE_RE.fullmatch(file_id)) and file_id not in file_ids, f"{where}.id: 应为唯一 F001 格式")
        file_ids.add(file_id)
        path = field(item, "path", str, where)
        require(not Path(path).is_absolute() and ".." not in Path(path).parts and path != ".", f"{where}.path: 必须为仓库内相对路径")
        require("old_path" in item and (item["old_path"] is None or isinstance(item["old_path"], str)), f"{where}.old_path: 应为字符串或 null")
        require(field(item, "status", str, where) in STATUSES, f"{where}.status: 未知状态")
        require(field(item, "bucket", str, where) in BUCKETS, f"{where}.bucket: 未知分类")
        field(item, "category_basis", str, where)
        for name in ("before_lines", "after_lines", "added", "removed"):
            nullable_count(item, name, where)
        field(item, "binary", bool, where)
        patch = field(item, "patch", str, where, empty=True)
        hunks = field(item, "hunks", list, where)
        actual_hunks = [(header, lines) for header, lines in split_patch(patch) if header is not None]
        require(len(actual_hunks) == len(hunks), f"{where}.hunks: 与完整 patch 的 @@ 块数量不一致")
        for hindex, hunk in enumerate(hunks):
            hwhere = f"{where}.hunks[{hindex}]"
            require(isinstance(hunk, dict), f"{hwhere}: 应为对象")
            hunk_id = field(hunk, "id", str, hwhere)
            require(bool(re.fullmatch(re.escape(file_id) + r"-H\d{2,}", hunk_id)) and hunk_id not in all_hunk_ids, f"{hwhere}.id: 应为唯一 {file_id}-H01 格式")
            all_hunk_ids.add(hunk_id)
            header = field(hunk, "header", str, hwhere)
            lines = field(hunk, "lines", list, hwhere)
            require(all(isinstance(line, str) for line in lines), f"{hwhere}.lines: 应为字符串列表")
            require((header, lines) == actual_hunks[hindex], f"{hwhere}: header/lines 未逐行匹配完整 patch；不能输出不完整或错位 diff")


def draft_review(snapshot: dict, review: dict) -> dict:
    """预览可显式留白，但不会自动推导必要性、风险、证据或优先级。"""
    require(isinstance(review, dict), "review: 应为对象")
    result = copy.deepcopy(review)
    result.setdefault("version", 1)
    result.setdefault("snapshot_fingerprint", snapshot["fingerprint"])
    for name, value in (("title", "审查报告 · 草稿"), ("goal", PENDING), ("non_goals", []), ("summary", PENDING), ("issues", []), ("sections", []), ("validations", []), ("files", {})):
        result.setdefault(name, value)
        if isinstance(value, str) and result[name] == "":
            result[name] = value
    require(isinstance(result["files"], dict), "review.files: 应为对象")
    for item in snapshot["files"]:
        details = result["files"].setdefault(item["id"], {})
        require(isinstance(details, dict), f"review.files.{item['id']}: 应为对象")
        for name in ("purpose", "change", "why", "without", "necessity", "reasonableness", "loss", "risk"):
            details.setdefault(name, PENDING)
            if details[name] == "":
                details[name] = PENDING
        details.setdefault("hunks", {})
        require(isinstance(details["hunks"], dict), f"review.files.{item['id']}.hunks: 应为对象")
        for hunk in item["hunks"]:
            explanation = details["hunks"].setdefault(hunk["id"], {})
            require(isinstance(explanation, dict), f"review.files.{item['id']}.hunks.{hunk['id']}: 应为对象")
            for name in ("title", "change", "necessity", "reasonableness", "without", "evidence"):
                explanation.setdefault(name, PENDING)
                if explanation[name] == "":
                    explanation[name] = PENDING
            explanation.setdefault("issues", [])
    return result


def validate_review(snapshot: dict, review: dict, draft: bool) -> None:
    require(isinstance(review, dict), "review: 应为对象")
    require(review.get("version") == 1, "review.version: 仅支持 1")
    require(field(review, "snapshot_fingerprint", str, "review") == snapshot["fingerprint"], "review.snapshot_fingerprint: 不匹配冻结快照，必须重新核对审查")
    for name in ("title", "goal", "summary"):
        field(review, name, str, "review")
    string_list(field(review, "non_goals", list, "review"), "review.non_goals")
    files = field(review, "files", dict, "review")
    expected_files = {item["id"] for item in snapshot["files"]}
    require(set(files) == expected_files, "review.files: 必须精确覆盖快照的全部文件，不能遗漏或混入其他范围")
    issues = field(review, "issues", list, "review")
    issue_ids: set[str] = set()
    for index, issue in enumerate(issues):
        where = f"review.issues[{index}]"
        require(isinstance(issue, dict), f"{where}: 应为对象")
        issue_id = field(issue, "id", str, where)
        require(bool(ISSUE_RE.fullmatch(issue_id)) and issue_id not in issue_ids, f"{where}.id: 应为唯一 P1 格式")
        issue_ids.add(issue_id)
        require(field(issue, "priority", str, where) in ("P0", "P1", "P2", "P3"), f"{where}.priority: 应为 P0–P3")
        require(field(issue, "evidence", str, where) in EVIDENCE, f"{where}.evidence: 非法证据等级")
        require(field(issue, "frequency", str, where) in FREQUENCY, f"{where}.frequency: 非法频率")
        for name in ("title", "condition", "invariant", "impact", "scope", "recovery", "suggestion"):
            field(issue, name, str, where)
        for name in ("user_problem", "reason"):
            if name in issue:
                field(issue, name, str, where)
        if "evidence_refs" in issue:
            string_list(issue["evidence_refs"], f"{where}.evidence_refs")
        locations = field(issue, "locations", list, where)
        require(bool(locations), f"{where}.locations: 至少一个范围内位置")
        for lindex, location in enumerate(locations):
            lwhere = f"{where}.locations[{lindex}]"
            require(isinstance(location, dict), f"{lwhere}: 应为对象")
            require(field(location, "file_id", str, lwhere) in expected_files, f"{lwhere}.file_id: 不属于快照")
            nullable_count(location, "line", lwhere)
            require(location["line"] is None or location["line"] > 0, f"{lwhere}.line: 应为正整数或 null")
            field(location, "symbol", str, lwhere)
        repro = field(issue, "repro", dict, where)
        for name in ("persona", "preconditions", "actual", "expected", "status", "boundary"):
            field(repro, name, str, f"{where}.repro")
        for name in ("steps", "observations"):
            string_list(field(repro, name, list, f"{where}.repro"), f"{where}.repro.{name}")
            require(bool(repro[name]), f"{where}.repro.{name}: 至少一项")
        require(field(repro, "type", str, f"{where}.repro") in ("已执行", "候选复现路径/待验证"), f"{where}.repro.type: 必须区分已执行与候选")
    for item in snapshot["files"]:
        where = f"review.files.{item['id']}"
        detail = files[item["id"]]
        require(isinstance(detail, dict), f"{where}: 应为对象")
        for name in ("purpose", "change", "why", "without", "necessity", "reasonableness", "loss", "risk"):
            field(detail, name, str, where)
        if not draft:
            require(detail["necessity"] in NECESSITY, f"{where}.necessity: 非法必要性分类")
            require(detail["loss"] in LOSSES, f"{where}.loss: 非法功能丢失分类")
        if "bucket" in detail:
            require(detail["bucket"] in BUCKETS, f"{where}.bucket: 非法覆盖分类")
            field(detail, "category_basis", str, where)
        elif "category_basis" in detail:
            field(detail, "category_basis", str, where)
        if "pair" in detail:
            field(detail, "pair", str, where)
        if "position" in detail:
            field(detail, "position", str, where)
        if "risks" in detail:
            string_list(detail["risks"], f"{where}.risks")
        hunks = field(detail, "hunks", dict, where)
        require(set(hunks) == {hunk["id"] for hunk in item["hunks"]}, f"{where}.hunks: 必须精确解释每个 diff 块")
        for hunk in item["hunks"]:
            hwhere = f"{where}.hunks.{hunk['id']}"
            explanation = hunks[hunk["id"]]
            require(isinstance(explanation, dict), f"{hwhere}: 应为对象")
            for name in ("title", "change", "necessity", "reasonableness", "without", "evidence"):
                field(explanation, name, str, hwhere)
            if not draft:
                require(explanation["necessity"] in NECESSITY, f"{hwhere}.necessity: 非法必要性分类")
            refs = explanation.get("issues", [])
            string_list(refs, f"{hwhere}.issues")
            require(set(refs).issubset(issue_ids), f"{hwhere}.issues: 引用了不存在的问题")
            if "focus" in explanation:
                require(isinstance(explanation["focus"], list), f"{hwhere}.focus: 应为列表")
                coordinates = diff_coordinates(hunk["header"], hunk["lines"])
                available = {("old", before) for _, before, _, _ in coordinates if before is not None}
                available.update(("new", after) for _, _, after, _ in coordinates if after is not None)
                markers: set[int] = set()
                for findex, focus in enumerate(explanation["focus"]):
                    fwhere = f"{hwhere}.focus[{findex}]"
                    require(isinstance(focus, dict), f"{fwhere}: 应为对象")
                    side = field(focus, "side", str, fwhere)
                    require(side in ("old", "new"), f"{fwhere}.side: 应为 old 或 new")
                    line = field(focus, "line", int, fwhere)
                    marker = field(focus, "marker", int, fwhere)
                    require(line > 0 and marker > 0, f"{fwhere}: line/marker 应为真实正整数")
                    field(focus, "explanation", str, fwhere)
                    require((side, line) in available, f"{fwhere}: 所选 {side} 行 {line} 不存在于本 hunk，不能伪造证据行")
                    require(marker not in markers, f"{fwhere}.marker: 同一代码块内编号必须唯一")
                    markers.add(marker)
    sections = field(review, "sections", list, "review")
    require(draft or bool(sections), "review.sections: 正式报告必须提供 file-change-review 输出合同的矩阵与验证映射")
    for index, section in enumerate(sections):
        where = f"review.sections[{index}]"
        require(isinstance(section, dict), f"{where}: 应为对象")
        field(section, "title", str, where)
        headers = field(section, "headers", list, where)
        string_list(headers, f"{where}.headers")
        require(bool(headers), f"{where}.headers: 不能为空")
        rows = field(section, "rows", list, where)
        require(bool(rows), f"{where}.rows: 无证据请明确填写缺口，不能空表")
        for rindex, row in enumerate(rows):
            string_list(row, f"{where}.rows[{rindex}]")
            require(len(row) == len(headers), f"{where}.rows[{rindex}]: 与表头列数不一致")
    if not draft:
        missing_sections = REQUIRED_SECTIONS - {section["title"] for section in sections}
        require(not missing_sections, "review.sections: 缺少输出合同必填表：" + "、".join(sorted(missing_sections)))
    validations = field(review, "validations", list, "review")
    require(draft or bool(validations), "review.validations: 正式报告必须明确已执行或未验证的证据边界")
    for index, validation in enumerate(validations):
        where = f"review.validations[{index}]"
        require(isinstance(validation, dict), f"{where}: 应为对象")
        for name in ("name", "result", "layer", "oracle", "boundary"):
            field(validation, name, str, where)


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def count(value) -> str:
    return "未知" if value is None else str(value)


def sum_count(files: list[dict], key: str) -> str:
    known = sum(item[key] for item in files if item[key] is not None)
    unknown = sum(item[key] is None for item in files)
    return f"{known}（另 {unknown} 文件未知）" if unknown else str(known)


def fields_list(pairs: list[tuple[str, str]], ordered: bool = False) -> str:
    tag = "ol" if ordered else "dl"
    if ordered:
        return '<ol class="review-fields">' + "".join(f"<li><strong>{esc(label)}：</strong><span>{esc(value)}</span></li>" for label, value in pairs) + "</ol>"
    return '<dl class="field-grid">' + "".join(f"<dt>{esc(label)}</dt><dd>{esc(value)}</dd>" for label, value in pairs) + "</dl>"


def issue_link(issue_id: str) -> str:
    return f'<a class="issue-ref" data-open-target href="#issue-{esc(issue_id)}">{esc(issue_id)}</a>'


def render_diff(header: str, lines: list[str], hunk_id: str, focus: list[dict]) -> str:
    output = [f'<span class="diff-row hunk-header"><span class="old-line"></span><span class="new-line"></span><span class="code-text">{esc(header)}</span></span>']
    for line, before, after, kind in diff_coordinates(header, lines):
        selected = [entry for entry in focus if (entry["side"] == "old" and entry["line"] == before) or (entry["side"] == "new" and entry["line"] == after)]
        markers = "".join(f'<span class="evidence-marker" id="focus-{esc(hunk_id)}-M{entry["marker"]}" title="{esc(entry["explanation"])}" aria-label="重点 {entry["marker"]}，{esc(entry["side"])} 行 {entry["line"]}">{entry["marker"]}</span>' for entry in sorted(selected, key=lambda entry: entry["marker"]))
        marker_group = f'<span class="focus-markers">{markers}</span>' if markers else ""
        focus_class = " is-focus" if selected else ""
        output.append(f'<span class="diff-row {kind}{focus_class}"><span class="old-line" aria-hidden="true">{before if before is not None else ""}</span><span class="new-line" aria-hidden="true">{after if after is not None else ""}</span><span class="code-cell">{marker_group}<span class="code-text">{esc(line)}</span></span></span>')
    return '<pre class="diff-code"><code>' + "\n".join(output) + "</code></pre>"


def render_hunk(hunk: dict, explanation: dict, index: int, draft: bool) -> str:
    refs = " ".join(issue_link(issue_id) for issue_id in explanation.get("issues", []))
    opening = " open" if explanation.get("issues") or draft else ""
    return (
        f'<details class="hunk-review" data-review-expand id="hunk-{esc(hunk["id"])}"{opening}>'
        f'<summary><span class="hunk-title">代码块 {index} · {esc(explanation["title"])}</span><span class="anchor-label">{esc(hunk["id"])}</span></summary>'
        + render_diff(hunk["header"], hunk["lines"], hunk["id"], explanation.get("focus", []))
        + ('<ol class="focus-explanations">' + "".join(f'<li value="{entry["marker"]}"><a data-open-target href="#focus-{esc(hunk["id"])}-M{entry["marker"]}">重点 {entry["marker"]} · {"新" if entry["side"] == "new" else "旧"}行 {entry["line"]}</a>：{esc(entry["explanation"])}</li>' for entry in sorted(explanation["focus"], key=lambda entry: entry["marker"])) + "</ol>" if explanation.get("focus") else "")
        + fields_list([("改动用途", explanation["change"]), ("必要性", explanation["necessity"]), ("合理性与边界", explanation["reasonableness"]), ("移除此块的影响", explanation["without"]), ("证据", explanation["evidence"])])
        + (f'<p class="hunk-issues">关联问题：{refs}</p>' if refs else "")
        + "</details>"
    )


def representative_line(item: dict) -> tuple[str | None, str | None, int | None]:
    """选择真实新增行，只有删除时选择旧行；元信息变更不编造源码行号。"""
    removed = None
    for hunk in item["hunks"]:
        for _, before, after, kind in diff_coordinates(hunk["header"], hunk["lines"]):
            if kind == "added":
                return hunk["id"], "new", after
            if kind == "removed" and removed is None:
                removed = (hunk["id"], "old", before)
    return removed or (None, None, None)


def file_path_link(item: dict, snapshot: dict) -> str:
    """当前磁盘与基线/index/commit 快照链接分开，防止链接冒充受审源码。"""
    hunk_id, side, line = representative_line(item)
    href = "#hunk-" + hunk_id if hunk_id else "#file-" + item["id"]
    internal = " data-open-target"
    if line is None:
        suffix = "（元信息变化，无文本行号）"
    elif side == "new" and snapshot["scope"] in ("worktree", "unstaged") and item["status"] != "deleted":
        href = (Path(snapshot["repo"]) / item["path"]).as_uri() + f"#L{line}"
        internal = ""
        suffix = f" (line {line})"
    else:
        source = "基线" if side == "old" else ("index 快照" if snapshot["scope"] == "staged" else "目标快照")
        suffix = f"（{source} line {line}）"
    return f'<a class="file-path"{internal} href="{esc(href)}">{esc(item["path"] + suffix)}</a>'


def render_file(item: dict, detail: dict, issues: list[dict], draft: bool, snapshot: dict) -> str:
    file_id = item["id"]
    bucket = detail.get("bucket", item["bucket"])
    basis = detail.get("category_basis", item["category_basis"])
    issue_ids = {issue["id"] for issue in issues if any(location["file_id"] == file_id for location in issue["locations"])}
    issue_ids.update(ref for hunk in detail["hunks"].values() for ref in hunk.get("issues", []))
    opening = " open" if issue_ids or draft else ""
    path_search = item["path"] + " " + (item["old_path"] or "")
    encoded_patch = base64.b64encode(item["patch"].encode("utf-8")).decode("ascii")
    summary = (
        f'<summary class="file-summary"><span class="file-primary"><span class="bucket bucket-{BUCKETS.index(bucket)}">{esc(bucket)}</span>'
        + file_path_link(item, snapshot) + '</span><span class="file-metrics">'
        f'<span class="status">{esc(item["status"])}</span><span>总行数 {count(item["before_lines"])} → {count(item["after_lines"])}</span>'
        f'<span class="addition">+{count(item["added"])}</span><span class="deletion">−{count(item["removed"])}</span><span>{len(item["hunks"])} 块</span>'
        + (f'<span class="problem-count">{len(issue_ids)} 项问题</span>' if issue_ids else "")
        + "</span></summary>"
    )
    pairs = [("文件用途", detail["purpose"]), ("改动位置", detail.get("position", detail["change"])), ("改动用途", detail["change"]), ("为什么要改", detail["why"]), ("不改动有什么影响", detail["without"]), ("必要性", detail["necessity"]), ("合理性", detail["reasonableness"]), ("功能丢失", detail["loss"])]
    by_issue_id = {issue["id"]: issue for issue in issues}
    for index, issue_id in enumerate(sorted(issue_ids, key=lambda ref: int(ref[1:])), 1):
        issue = by_issue_id[issue_id]
        pairs.append((f"独立隐患 {index}", f'{issue["title"]}（{issue_id}；{issue["evidence"]}；{issue["frequency"]}）'))
    for index, risk in enumerate(detail.get("risks", []), 1):
        pairs.append((f"其他独立隐患 {index}", risk))
    pairs.append(("补充风险与验证边界", detail["risk"]))
    if "pair" in detail:
        pairs.append(("另一端逻辑是否一致", detail["pair"]))
    pairs.append(("分类依据", basis))
    contents = fields_list(pairs, ordered=True)
    if item["old_path"]:
        contents += f'<p>原路径：<code>{esc(item["old_path"])}</code></p>'
    if issue_ids:
        contents += '<p class="file-issues">关联问题：' + " ".join(issue_link(issue_id) for issue_id in sorted(issue_ids, key=lambda ref: int(ref[1:]))) + "</p>"
    contents += (
        '<div class="patch-actions"><button type="button" data-copy-patch>复制完整 diff</button><button type="button" data-download-patch>下载此文件 diff</button>'
        f'<span hidden class="raw-patch" data-patch-base64="{encoded_patch}" data-filename="{esc(item["path"])}"></span></div>'
        '<p class="diff-legend">左侧为旧行号 / 新行号；红色删除、绿色新增；元信息及全部代码块均保留。行数未知显示“未知”，不按 0 计算。</p>'
    )
    hindex = 0
    for header, lines in split_patch(item["patch"]):
        if header is None:
            contents += '<pre class="diff-meta"><code>' + esc("\n".join(lines)) + "</code></pre>"
        else:
            hunk = item["hunks"][hindex]
            hindex += 1
            contents += render_hunk(hunk, detail["hunks"][hunk["id"]], hindex, draft)
    if not item["patch"]:
        contents += '<p class="boundary-note">此快照未提供可显示 patch；请结合文件层说明确认原因。</p>'
    return f'<li class="file-item" data-path="{esc(path_search.lower())}" data-bucket="{esc(bucket)}" data-has-issue="{"yes" if issue_ids else "no"}"><details class="file-review" data-review-expand id="file-{esc(file_id)}"{opening}>{summary}<div class="file-content">{contents}</div></details></li>'


def render_issue(issue: dict, snapshot_files: dict, review_files: dict) -> str:
    links = []
    for location in issue["locations"]:
        item = snapshot_files[location["file_id"]]
        suffix = f':{location["line"]}' if location["line"] is not None else ""
        links.append(f'<a data-open-target href="#file-{esc(item["id"])}">{esc(item["path"] + suffix)} · {esc(location["symbol"])}</a>')
    blocks = []
    for file_id, detail in review_files.items():
        for hunk_id, explanation in detail["hunks"].items():
            if issue["id"] in explanation.get("issues", []):
                blocks.append(f'<a data-open-target href="#hunk-{esc(hunk_id)}">{esc(hunk_id)}</a>')
    repro = issue["repro"]
    kind_class = "executed" if repro["type"] == "已执行" else "candidate"
    output = (
        f'<article class="issue-card priority-{esc(issue["priority"])}" id="issue-{esc(issue["id"])}">'
        f'<h3><span class="priority">{esc(issue["priority"])}</span> {esc(issue["id"])} · {esc(issue["title"])}</h3>'
        f'<p class="evidence-label">证据等级：{esc(issue["evidence"])} · 发生频率：{esc(issue["frequency"])}</p>'
        '<p class="location-links">位置：' + "<br>".join(links) + "</p>"
        + ('<p>对应代码块：' + " · ".join(blocks) + "</p>" if blocks else "")
        + fields_list([("用户表现", issue.get("user_problem", repro["actual"])), ("用户影响", issue["impact"]), ("影响范围", issue["scope"])])
        + '<details class="repro" data-review-expand open><summary>用户复现路径 '
        + f'<span class="repro-type {kind_class}">{esc(repro["type"])}</span></summary>'
        + fields_list([("用户画像", repro["persona"]), ("前置条件", repro["preconditions"])])
        + '<ol class="repro-steps">' + "".join(f"<li>{esc(step)}</li>" for step in repro["steps"]) + "</ol>"
        + f'<p class="actual"><strong>实际结果/推导结果：</strong>{esc(repro["actual"])}</p>'
        + f'<p class="expected"><strong>预期结果：</strong>{esc(repro["expected"])}</p>'
        + fields_list([("可达条件/关键原因", issue.get("reason", issue["condition"])), ("破坏的不变量", issue["invariant"]), ("确认状态", issue["evidence"] + " / " + issue["frequency"] + " / " + repro["status"]), ("可恢复性", issue["recovery"]), ("最小修正建议", issue["suggestion"]), ("证据边界", repro["boundary"])])
        + ('<p class="evidence-refs"><strong>证据编号：</strong>' + " · ".join(esc(ref) for ref in issue["evidence_refs"]) + "</p>" if issue.get("evidence_refs") else "")
        + '<p><strong>观察点</strong></p><ul>' + "".join(f"<li>{esc(observation)}</li>" for observation in repro["observations"]) + "</ul></details></article>"
    )
    return output


def render_table(title: str, headers: list[str], rows: list[list[str]]) -> str:
    return (
        f'<section class="matrix-section"><h2>{esc(title)}</h2><div class="table-scroll"><table><thead><tr>'
        + "".join(f"<th scope=\"col\">{esc(header)}</th>" for header in headers)
        + "</tr></thead><tbody>"
        + "".join("<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>" for row in rows)
        + "</tbody></table></div></section>"
    )


def render_scope_details(snapshot: dict) -> str:
    """把排除范围和本地三层状态显式展示，避免冻结文件数被读作全仓覆盖。"""
    pairs = []
    if "layers" in snapshot:
        for layer, label in (("staged", "staged（已暂存）"), ("unstaged", "unstaged（未暂存）"), ("untracked", "untracked（未跟踪）")):
            paths = snapshot["layers"][layer]
            pairs.append((label, "\n".join(paths) if paths else "无"))
    if "pathspecs" in snapshot:
        pairs.append(("路径限定", "\n".join(snapshot["pathspecs"]) if snapshot["pathspecs"] else "未限定路径；实际纳入范围仍由 scope 与排除规则决定"))
    for name, label in (("exclusion_patterns", "排除规则"), ("excluded", "已排除文件")):
        if name in snapshot:
            pairs.append((label, "\n".join(snapshot[name]) if snapshot[name] else "无排除"))
    result = fields_list(pairs) if pairs else ""
    if "layers" in snapshot:
        note = "range 的 staged/unstaged/untracked 清单仅展示采集时本地状态，不等于纳入本次 commit range；实际审查对象以冻结 diff 文件列表为准。" if snapshot["scope"] == "range" else "三层清单展示采集时本地状态；实际审查范围由 scope、路径限定与排除规则决定，以冻结 diff 文件列表为准。"
        result += f'<p class="boundary-note">{esc(note)}</p>'
    return result


def build_html(snapshot: dict, review: dict, draft: bool) -> str:
    assets = Path(__file__).resolve().parent.parent / "assets"
    css = (assets / "report.css").read_text(encoding="utf-8")
    script = (assets / "report.js").read_text(encoding="utf-8")
    hash_source = lambda source: base64.b64encode(hashlib.sha256(source.encode("utf-8")).digest()).decode("ascii")
    csp = f"default-src 'none'; style-src 'sha256-{hash_source(css)}'; script-src 'sha256-{hash_source(script)}'; img-src data:; base-uri 'none'; form-action 'none'"
    files, issues = snapshot["files"], review["issues"]
    by_id = {item["id"]: item for item in files}
    bucket_counts = Counter(review["files"][item["id"]].get("bucket", item["bucket"]) for item in files)
    parts = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        f'<meta http-equiv="Content-Security-Policy" content="{esc(csp)}"><title>{esc(review["title"])}</title><style>{css}</style></head>',
        '<body><a class="skip-link" href="#file-conclusions">跳到逐文件结论</a><main>',
        '<div class="draft-banner">草稿预览：待填写内容不是审查结论，不能作为正式报告交付。</div>' if draft else "",
        f'<header class="report-header"><p class="eyebrow">本地只读审查 · 冻结 diff · 分层证据</p><h1>{esc(review["title"])}</h1><p class="report-summary">{esc(review["summary"])}</p>',
        f'<div class="summary-stats"><span><strong>{len(files)}</strong> 个改动文件</span><span><strong>{bucket_counts["业务"]}</strong> 业务 / <strong>{bucket_counts["测试"]}</strong> 测试</span><span class="addition">+{sum_count(files, "added")}</span><span class="deletion">−{sum_count(files, "removed")}</span><span>{sum(len(item["hunks"]) for item in files)} 个代码块</span><span>{len(issues)} 项录入问题</span></div></header>',
        '<div class="baseline"><p class="baseline-title"><strong>基线与目标</strong></p>',
        fields_list([("checkout", snapshot["repo"]), ("branch / HEAD", snapshot["branch"] + " / " + snapshot["head"]), ("diff 范围", snapshot["scope"] + " · " + snapshot["base"] + " → " + snapshot["target"]), ("快照时间", snapshot["captured_at"]), ("快照指纹", snapshot["fingerprint"]), ("独立目标", review["goal"])]),
        render_scope_details(snapshot),
        '<p><strong>非目标：</strong>' + ("；".join(esc(goal) for goal in review["non_goals"]) if review["non_goals"] else (PENDING if draft else "未另列非目标；以用户授权范围和逐文件说明为准")) + "</p></div>",
        '<section id="file-conclusions"><h2>结论</h2><div class="review-toolbar" role="group" aria-label="文件筛选与展开"><label>路径筛选 <input id="path-filter" type="search" placeholder="输入路径或旧路径"></label><label>文件类型 <select id="bucket-filter"><option value="">全部</option>' + "".join(f'<option value="{esc(bucket)}">{esc(bucket)} ({bucket_counts[bucket]})</option>' for bucket in BUCKETS) + f'</select></label><label><input id="issue-filter" type="checkbox">只看关联问题</label><button type="button" id="expand-all">全部展开</button><button type="button" id="collapse-all">全部收起</button><label><input id="wrap-code" type="checkbox">代码换行</label><span id="filter-result" aria-live="polite">全部 {len(files)} 个文件</span></div>',
        '<p id="interaction-status" class="interaction-status" aria-live="polite"></p><ol class="file-list">',
    ]
    for item in files:
        parts.append(render_file(item, review["files"][item["id"]], issues, draft, snapshot))
    parts.extend([f'<li class="overall-item" value="{len(files) + 1}" id="overall-judgment"><strong>整体判断：</strong><p>{esc(review["summary"])}</p><p class="boundary-note">必要性、实现合理性、证据等级与运行验收分别判断；此报告不增加修改、提交、发布或设备操作授权。</p></li></ol>', '<p id="empty-filter" hidden>没有符合筛选条件的文件。</p></section>', '<section class="issue-overview"><h2>审查问题索引</h2>'])
    if issues:
        parts.append('<ul class="issue-index">' + "".join(f'<li><a data-open-target href="#issue-{esc(issue["id"])}"><span class="priority">{esc(issue["priority"])}</span> {esc(issue["id"])} · {esc(issue["title"])}</a><span>{esc(issue["evidence"])} / {esc(issue["frequency"])}</span></li>' for issue in sorted(issues, key=lambda item: (item["priority"], int(item["id"][1:])))) + "</ul>")
    else:
        parts.append('<p class="boundary-note">' + ("独立问题待填写；草稿没有录入问题不代表无风险。" if draft else "本次录入 0 项独立问题；请结合逐文件风险及未验证边界，不据此推定无风险。") + "</p>")
    parts.append("</section>")
    if issues:
        parts.append('<section id="issues"><h2>问题与风险 · 用户复现路径</h2>')
        for issue in sorted(issues, key=lambda item: (item["priority"], int(item["id"][1:]))):
            parts.append(render_issue(issue, by_id, review["files"]))
        parts.append("</section>")
    for section in review["sections"]:
        parts.append(render_table(section["title"], section["headers"], section["rows"]))
    if not review["sections"]:
        parts.append('<section class="matrix-section"><h2>矩阵与影响/验证映射</h2><p class="boundary-note">待填写 file-change-review 输出合同要求的表格及用户验证。</p></section>')
    parts.append(render_table("验证结果与证据边界", ["验证项", "实际结果", "证据层级", "判定 Oracle", "未验证边界"], [[validation[key] for key in ("name", "result", "layer", "oracle", "boundary")] for validation in review["validations"]]) if review["validations"] else '<section class="matrix-section"><h2>验证结果与证据边界</h2><p class="boundary-note">待填写；没有执行记录时不得声明验证通过。</p></section>')
    parts.append(f'<footer>报告依据冻结快照 {esc(snapshot["fingerprint"])}。代码与评语按文本展示，页面不加载外部资源。</footer></main><script>{script}</script></body></html>')
    return "\n".join(parts)


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReportError(f"无法读取 JSON {path}: {error}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path, help="冻结 diff JSON")
    parser.add_argument("--review", type=Path, help="人工逐文件/hunk 审查 JSON；正式报告必填")
    parser.add_argument("--output", required=True, type=Path, help="业务仓库以外的 HTML 路径")
    parser.add_argument("--draft", action="store_true", help="显式草稿预览，缺失评语显示待填写；不得作为正式报告交付")
    args = parser.parse_args()
    try:
        require(args.review is not None or args.draft, "正式报告必须指定 --review；只有 --draft 允许待填写")
        snapshot = load_json(args.snapshot.expanduser())
        validate_snapshot(snapshot)
        output = args.output.expanduser().resolve()
        repo = Path(snapshot["repo"]).expanduser().resolve()
        require(output != repo and repo not in output.parents, "禁止把审查报告写入 snapshot.repo 业务工作区；请选择仓外路径")
        review = load_json(args.review.expanduser()) if args.review else {}
        if args.draft:
            review = draft_review(snapshot, review)
        validate_review(snapshot, review, args.draft)
        report = build_html(snapshot, review, args.draft)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report, encoding="utf-8")
        print(f"{'草稿' if args.draft else '报告'}已生成：{output}；{len(snapshot['files'])} 文件 / {sum(len(item['hunks']) for item in snapshot['files'])} 代码块")
        return 0
    except (ReportError, OSError, UnicodeError) as error:
        print(f"审查报告未生成：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
