#!/usr/bin/env python3
"""Meaningful offline integration checks in disposable Git repos; never touch a user's checkout."""
import base64
import copy
from html.parser import HTMLParser
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect_diff as collect
import render_report as render


class Page(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.tags, self.patches, self.ids = [], [], []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        self.tags.append((tag, data))
        if "id" in data:
            self.ids.append(data["id"])
        if "data-patch-base64" in data:
            self.patches.append(base64.b64decode(data["data-patch-base64"]).decode("utf-8"))


def complete_form(snapshot):
    # Structural test data only: these explanations are not a semantic review.
    data = collect.review_form(snapshot)
    data.update(goal="隔离样例：验证报告传输与覆盖，不是业务审查", summary="这是自动化结构验证样例，语义评估未执行")
    for file in data["files"].values():
        for key in ("purpose", "change", "why", "without", "reasonableness", "risk"):
            file[key] = "结构测试字段，不构成真实审查结论"
        file["necessity"], file["loss"] = "证据不足", "证据不足"
        for hunk in file["hunks"].values():
            for key in ("title", "change", "reasonableness", "without", "evidence"):
                hunk[key] = "结构测试字段，语义待人工审查"
            hunk["necessity"] = "证据不足"
    titles = ["当前改动影响范围与潜在隐患", "基线、约束与验证矩阵", "最小必要改动基线", "最小必要改动门禁",
              "功能点前后差异", "文件功能影响与丢失", "冗余/过度改动", "用户视角验证", "待确认改动与验证边界"]
    data["sections"] = [{"title": title, "headers": ["测试边界"], "rows": [["结构自动验证；实际语义未评估"]]} for title in titles]
    data["validations"] = [{"name": "隔离结构测试", "result": "仅检查渲染与输入校验", "layer": "脚本集成",
                            "oracle": "完整patch回读一致", "boundary": "不证明用户设备行为"}]
    return data


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="review-report-test-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Isolated report test")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "core.fileMode", "true")
        for path, content in {"src/service.ts": b"export const value = 1;\n", "src/old.ts": b"rename me\n",
                              "src/delete.ts": b"delete\nthree\nlines\n", "src/crlf.ts": b"a\r\nb\r\n",
                              "tests/service.test.ts": b"expect(value).toBe(1);\n", "mode.sh": b"#!/bin/sh\nexit 0\n",
                              "binary.bin": b"a\x00b"}.items():
            self.write(path, content)
        # Commits are confined to this disposable fixture, never the reviewed repo.
        self.git("add", ".")
        self.git("commit", "-qm", "fixture baseline")
        self.base = self.git("rev-parse", "HEAD").decode().strip()
        self.write("src/service.ts", b"export const value = 2;\n")
        self.git("add", "src/service.ts")
        self.write("src/service.ts", b"export const value = 3;\nexport const label = '</script><img onerror=alert(1)>';\n")
        self.git("mv", "src/old.ts", "src/中文 renamed.ts")
        (self.repo / "src/delete.ts").unlink()
        self.write("src/crlf.ts", b"a\r\nchanged\r\n")
        self.write("tests/service.test.ts", b"expect(value).toBe(3);\n")
        (self.repo / "mode.sh").chmod(0o755)
        self.write("binary.bin", b"x\x00y")
        self.write("untracked 中文.ts", b"const result = 42;")
        self.write("empty.txt", b"")
        external = self.root / "external.txt"
        external.write_text("external private contents must not be read")
        (self.repo / "pointer").symlink_to(external)
        self.before_status = self.git("status", "--porcelain=v1", "-z")
        self.before_index = (self.repo / ".git/index").read_bytes()

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return collect.git(self.repo, *args)

    def write(self, path, raw):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)

    def snapshot(self, scope="worktree", extra=()):
        target = self.root / (scope + "-snapshot.json")
        result = subprocess.run([sys.executable, str(Path(collect.__file__)), "--repo", str(self.repo),
                                 "--scope", scope, "--output", str(target), *extra], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(target.read_text())

    def test_worktree_files_counts_patch_and_no_mutation(self):
        snapshot = self.snapshot()
        by_path = {file["path"]: file for file in snapshot["files"]}
        self.assertEqual(len(by_path), 10)
        self.assertEqual((by_path["src/service.ts"]["before_lines"], by_path["src/service.ts"]["after_lines"]), (1, 2))
        self.assertEqual((by_path["src/service.ts"]["added"], by_path["src/service.ts"]["removed"]), (2, 1))
        self.assertEqual(by_path["src/delete.ts"]["after_lines"], 0)
        self.assertEqual(by_path["src/delete.ts"]["removed"], 3)
        self.assertEqual(by_path["src/中文 renamed.ts"]["old_path"], "src/old.ts")
        self.assertEqual(by_path["mode.sh"]["status"], "mode-only")
        self.assertIsNone(by_path["binary.bin"]["added"])
        self.assertEqual(by_path["untracked 中文.ts"]["after_lines"], 1)
        self.assertIn("\\ No newline at end of file", by_path["untracked 中文.ts"]["patch"])
        self.assertNotIn("external private contents", by_path["pointer"]["patch"])
        self.assertEqual(by_path["tests/service.test.ts"]["bucket"], "测试")
        self.assertIn("\r", by_path["src/crlf.ts"]["hunks"][0]["lines"][0])
        expected = self.git("diff", "--no-ext-diff", "--no-textconv", "--find-renames", "--full-index", "--unified=3", self.base, "--", "src/service.ts").decode()
        self.assertEqual(by_path["src/service.ts"]["patch"], expected)
        self.assertEqual(self.git("status", "--porcelain=v1", "-z"), self.before_status)
        self.assertEqual((self.repo / ".git/index").read_bytes(), self.before_index)

    def test_staged_unstaged_range_are_distinct(self):
        staged, unstaged = self.snapshot("staged"), self.snapshot("unstaged")
        self.assertEqual(unstaged["base"], "index")
        a = next(f for f in staged["files"] if f["path"] == "src/service.ts")
        b = next(f for f in unstaged["files"] if f["path"] == "src/service.ts")
        self.assertIn("+export const value = 2;", a["patch"])
        self.assertIn("-export const value = 2;", b["patch"])
        self.assertFalse(any(f["path"].startswith("untracked") for f in staged["files"] + unstaged["files"]))
        self.git("commit", "-qm", "fixture index change")
        ranged = self.snapshot("range", ("--base", self.base, "--target", "HEAD"))
        self.assertEqual(ranged["target"], self.git("rev-parse", "HEAD").decode().strip())
        self.assertEqual({f["path"] for f in ranged["files"]}, {f["path"] for f in staged["files"]})

    def test_secret_exclusion_is_explicit_and_recorded(self):
        self.write(".env", b"secret-content-not-for-output")
        output = self.root / "blocked.json"
        result = subprocess.run([sys.executable, collect.__file__, "--repo", str(self.repo), "--output", str(output)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
        self.assertNotIn("secret-content", result.stderr)
        snapshot = self.snapshot(extra=("--exclude", ".env"))
        self.assertEqual(snapshot["excluded"], [".env"])

    def test_outputs_inside_repo_are_rejected(self):
        result = subprocess.run([sys.executable, collect.__file__, "--repo", str(self.repo), "--output", str(self.repo / "snapshot.json")], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.repo / "snapshot.json").exists())
        snapshot = self.snapshot()
        (self.root / "review.json").write_text(json.dumps(complete_form(snapshot)))
        result = subprocess.run([sys.executable, render.__file__, "--snapshot", str(self.root / "worktree-snapshot.json"), "--review", str(self.root / "review.json"), "--output", str(self.repo / "report.html")], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.repo / "report.html").exists())

    def test_complete_diff_roundtrip_and_html_escaping(self):
        snapshot = self.snapshot()
        review = complete_form(snapshot)
        review["title"] = '</title><img src="https://bad.invalid" onerror="alert(1)">'
        render.validate_snapshot(snapshot)
        render.validate_review(snapshot, review, False)
        page = Page(render.build_html(snapshot, review, False))
        self.assertEqual(page.patches, [f["patch"] for f in snapshot["files"]])
        self.assertEqual(len(page.ids), len(set(page.ids)))
        self.assertFalse(any(tag == "img" or ("src" in attrs or "srcset" in attrs) for tag, attrs in page.tags))
        self.assertEqual(sum(tag == "script" for tag, _ in page.tags), 1)
        for file in snapshot["files"]:
            self.assertIn("file-" + file["id"], page.ids)
            for hunk in file["hunks"]:
                self.assertIn("hunk-" + hunk["id"], page.ids)

    def test_draft_cannot_be_used_as_formal_review(self):
        snapshot = self.snapshot()
        blank = collect.review_form(snapshot)
        with self.assertRaises(render.ReportError):
            render.validate_review(snapshot, blank, False)
        draft = render.draft_review(snapshot, blank)
        render.validate_review(snapshot, draft, True)
        self.assertIn("草稿预览", render.build_html(snapshot, draft, True))

    def test_file_type_change_keeps_both_patches_and_correct_counts(self):
        path = self.repo / "src/service.ts"
        path.unlink()
        path.symlink_to("new-target.ts")
        snapshot = self.snapshot()
        file = next(f for f in snapshot["files"] if f["path"] == "src/service.ts")
        self.assertEqual((file["added"], file["removed"]), (1, 1))
        self.assertEqual(len(file["hunks"]), 2)
        self.assertNotIn("diff --git", "\n".join(file["hunks"][0]["lines"]))
        render.validate_snapshot(snapshot)
        review = complete_form(snapshot)
        render.validate_review(snapshot, review, False)
        self.assertIn("new file mode 120000", render.build_html(snapshot, review, False))

    def test_focus_coordinates_markers_and_patch_preservation(self):
        snapshot = self.snapshot()
        review = complete_form(snapshot)
        file = next(f for f in snapshot["files"] if f["path"] == "src/service.ts")
        detail = review["files"][file["id"]]
        detail["position"] = "exported value and label"
        hunk = detail["hunks"][file["hunks"][0]["id"]]
        hunk["focus"] = [{"side": "new", "line": 2, "marker": 1, "explanation": "E1：新增行的完整源码和 HTML 转义证据，不是设备行为"}]
        render.validate_review(snapshot, review, False)
        html = render.build_html(snapshot, review, False)
        page = Page(html)
        self.assertEqual(page.patches, [f["patch"] for f in snapshot["files"]])
        self.assertEqual(sum("is-focus" in attrs.get("class", "").split() for _, attrs in page.tags), 1)
        self.assertEqual(sum("evidence-marker" in attrs.get("class", "").split() for _, attrs in page.tags), 1)
        self.assertIn("新增行的完整源码", html)
        source_uri = (Path(snapshot["repo"]) / "src/service.ts").as_uri()
        self.assertTrue(any(tag == "a" and attrs.get("href", "").startswith(source_uri + "#L") for tag, attrs in page.tags))
        hunk["focus"][0]["line"] = 999
        with self.assertRaises(render.ReportError):
            render.validate_review(snapshot, review, False)
        hunk["focus"][0]["line"] = 2
        hunk["focus"].append({"side": "new", "line": 1, "marker": 1, "explanation": "重复编号应拒绝"})
        with self.assertRaises(render.ReportError):
            render.validate_review(snapshot, review, False)

    def test_fingerprint_missing_file_hunk_and_table_rejected(self):
        snapshot = self.snapshot()
        review = complete_form(snapshot)
        changed = copy.deepcopy(snapshot)
        changed["files"][0]["patch"] += "tampered\n"
        with self.assertRaises(render.ReportError):
            render.validate_snapshot(changed)
        bad = copy.deepcopy(review)
        bad["snapshot_fingerprint"] = "other"
        with self.assertRaises(render.ReportError):
            render.validate_review(snapshot, bad, False)
        for mutation in (lambda data: data["files"].pop(next(iter(data["files"]))),
                         lambda data: data["sections"].pop()):
            bad = copy.deepcopy(review)
            mutation(bad)
            with self.assertRaises(render.ReportError):
                render.validate_review(snapshot, bad, False)
        bad = copy.deepcopy(review)
        file = next(value for value in bad["files"].values() if value["hunks"])
        file["hunks"].pop(next(iter(file["hunks"])))
        with self.assertRaises(render.ReportError):
            render.validate_review(snapshot, bad, False)

    def test_issue_repro_and_links(self):
        snapshot = self.snapshot()
        review = complete_form(snapshot)
        file = next(f for f in snapshot["files"] if f["path"] == "src/service.ts")
        review["issues"] = [{"id": "P1", "priority": "P2", "title": "隔离样例问题卡片",
            "evidence": "待确认", "frequency": "频率待确认",
            "locations": [{"file_id": file["id"], "line": 1, "symbol": "value"}],
            "condition": "结构测试，不代表用户问题", "invariant": "验证引用完整性",
            "impact": "实际业务影响未评估", "scope": "临时样例", "recovery": "不适用，结构测试",
            "repro": {"persona": "样例读者", "preconditions": "仅打开报告样例", "steps": ["打开问题卡片", "点击代码块链接"],
                      "actual": "设备运行未验证", "expected": "报告展开对应代码", "observations": ["证据状态文字"],
                      "type": "候选复现路径/待验证", "status": "未执行业务验证", "boundary": "仅验证渲染"},
            "suggestion": "真实问题须人工判断"}]
        review["files"][file["id"]]["hunks"][file["hunks"][0]["id"]]["issues"] = ["P1"]
        render.validate_review(snapshot, review, False)
        html = render.build_html(snapshot, review, False)
        self.assertIn('id="issue-P1"', html)
        self.assertIn('href="#hunk-' + file["hunks"][0]["id"] + '"', html)
        self.assertIn("候选复现路径/待验证", html)
        review["issues"][0]["repro"]["steps"] = []
        with self.assertRaises(render.ReportError):
            render.validate_review(snapshot, review, False)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable; DOM fixture is optional and not browser proof")
    def test_controlled_dom_interactions(self):
        result = subprocess.run([shutil.which("node"), str(Path(__file__).with_name("test_interactions.cjs"))], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Controlled DOM:", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
