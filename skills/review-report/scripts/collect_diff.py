#!/usr/bin/env python3
"""Freeze a read-only Git diff and an empty review form outside the reviewed repo."""
import argparse
import difflib
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone


class SnapshotError(ValueError):
    pass


def git(repo, *args, check=True):
    result = subprocess.run(
        ["git", "--no-optional-locks", "--literal-pathspecs", "-C", str(repo),
         "-c", "core.quotePath=false", *args], capture_output=True, check=False)
    if check and result.returncode:
        # A diff failure must not become a misleading empty report.
        raise SnapshotError("Git operation failed: " + " ".join(args[:3]))
    return result.stdout


def decode(raw):
    return raw.decode("utf-8", errors="replace")


def resolve(repo, ref):
    return decode(git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}")).strip()


def changes(raw):
    parts = raw.split(b"\0")
    output, index = [], 0
    while index < len(parts) and parts[index]:
        status = decode(parts[index])
        index += 1
        old = None
        if status.startswith(("R", "C")):
            old = decode(parts[index])
            index += 1
        path = decode(parts[index])
        index += 1
        output.append((status, path, old))
    return output


def bucket(path):
    lower = path.lower()
    pieces = lower.split("/")
    if any(p in {"test", "tests", "__tests__"} for p in pieces) or re.search(r"\.(test|spec)\.", lower):
        return "测试", "路径/文件名命中测试规则；需人工核对实际职责"
    if any(p in {"generated", "dist", "build"} for p in pieces):
        return "生成物", "路径命中生成目录规则；需核对生成 owner"
    if Path(lower).suffix in {".md", ".rst", ".txt"} or "docs" in pieces:
        return "文档", "文档扩展名或目录；需人工核对"
    if Path(lower).suffix in {".json", ".yaml", ".yml", ".toml", ".lock"} or Path(lower).name in {"podfile", "makefile"}:
        return "配置", "配置文件规则；需人工核对"
    if Path(lower).suffix in {".ts", ".tsx", ".js", ".jsx", ".cjs", ".mjs", ".swift", ".kt", ".java", ".py", ".go", ".rs", ".c", ".h", ".cpp", ".css", ".scss", ".html"}:
        return "业务", "源码扩展名；工具/测试支撑可能需人工改分类"
    return "其他", "未命中分类规则；需人工确认"


def is_sensitive(path):
    name = Path(path).name.lower()
    return (name == ".env" or name.startswith(".env.") or
            name in {"key.properties", "google-play-api-key.json"} or
            Path(name).suffix in {".p8", ".p12", ".jks", ".keystore", ".pem"})


def count_lines(raw):
    if raw is None or b"\0" in raw:
        return None
    return raw.count(b"\n") + int(bool(raw) and not raw.endswith(b"\n"))


def blob(repo, revision, path):
    return git(repo, "show", (revision + ":" if revision else ":") + path)


def working_bytes(repo, path):
    target = repo / path
    # Report symlink contents, without reading an unrelated external target.
    if target.is_symlink():
        return os.readlink(target).encode("utf-8")
    return target.read_bytes()


def parse_hunks(patch, file_id):
    found, active = [], None
    lines = patch.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for line in lines:
        if line.startswith("@@ "):
            active = {"id": f"{file_id}-H{len(found) + 1:02d}", "header": line, "lines": []}
            found.append(active)
        elif line.startswith("diff --git "):
            # A file type change can emit delete/add patches for the same path.
            active = None
        elif active is not None:
            active["lines"].append(line)
    return found


def untracked_patch(path, raw, mode):
    # Quote the full Git path, including its prefix, for unusual filenames.
    title = json.dumps("b/" + path, ensure_ascii=False)
    old_title = json.dumps("a/" + path, ensure_ascii=False)
    header = f"diff --git {old_title} {title}\nnew file mode {mode}\n"
    if b"\0" in raw:
        return header + f"Binary files /dev/null and {title} differ\n"
    text = decode(raw)
    lines = text.split("\n")
    lines = [line + "\n" for line in lines[:-1]] + ([lines[-1]] if lines[-1] else [])
    body = list(difflib.unified_diff([], lines, fromfile="/dev/null", tofile=title))
    # Preserve Git's missing-final-newline marker in synthetic untracked patches.
    normalized = []
    for line in body:
        normalized.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return header + "".join(normalized)


def scope_args(args, base, target):
    if args.scope == "range":
        return [base, target]
    if args.scope == "staged":
        return ["--cached", base]
    if args.scope == "unstaged":
        return []
    return [base]


def freeze(args, repo, base, target):
    opts = ["diff", "--no-ext-diff", "--no-textconv", "--find-renames", "--full-index", "--unified=3"]
    spec = scope_args(args, base, target)
    records = changes(git(repo, "diff", "--name-status", "-z", "--find-renames", *spec, "--", *args.path))
    layers = {
        "staged": [p for _, p, _ in changes(git(repo, "diff", "--cached", "--name-status", "-z", "--", *args.path))],
        "unstaged": [p for _, p, _ in changes(git(repo, "diff", "--name-status", "-z", "--", *args.path))],
        "untracked": [decode(p) for p in git(repo, "ls-files", "--others", "--exclude-standard", "-z", "--", *args.path).split(b"\0") if p],
    }
    if args.scope == "worktree":
        records += [("?", p, None) for p in layers["untracked"]]
    excluded, files = [], []
    for status, path, old_path in sorted(records, key=lambda item: item[1]):
        if any(fnmatch.fnmatchcase(p, pattern) for p in (path, old_path or path) for pattern in args.exclude):
            excluded.append(path)
            continue
        if is_sensitive(path) or (old_path and is_sensitive(old_path)):
            raise SnapshotError(f"Potential secret file in diff: {path}. Narrow --path or explicitly --exclude it; exclusions are recorded.")
        fid = f"F{len(files) + 1:03d}"
        before, after = b"", b""
        if status != "?" and not status.startswith("A"):
            revision = "" if args.scope == "unstaged" else base
            before = blob(repo, revision, old_path or path)
        if not status.startswith("D"):
            if args.scope == "range":
                after = blob(repo, target, path)
            elif args.scope == "staged":
                after = blob(repo, "", path)
            else:
                after = working_bytes(repo, path)
        if status == "?":
            entry = repo / path
            mode = "120000" if entry.is_symlink() else ("100755" if entry.stat().st_mode & 0o111 else "100644")
            patch = untracked_patch(path, after, mode)
        else:
            patch = decode(git(repo, *opts, *spec, "--", *([old_path] if old_path else []), path))
        hunks = parse_hunks(patch, fid)
        binary = b"\0" in before or b"\0" in after or "Binary files " in patch
        kind = {"?": "added", "A": "added", "D": "deleted", "R": "renamed"}.get(status[:1], "modified")
        if binary:
            kind = "binary"
        elif not hunks and not status.startswith(("R", "C", "A", "?", "D")):
            kind = "mode-only"
        category, basis = bucket(path)
        files.append({"id": fid, "path": path, "old_path": old_path, "status": kind,
                      "bucket": category, "category_basis": basis,
                      "before_lines": None if binary else count_lines(before),
                      "after_lines": None if binary else count_lines(after),
                      "added": None if binary else sum(l.startswith("+") for h in hunks for l in h["lines"]),
                      "removed": None if binary else sum(l.startswith("-") for h in hunks for l in h["lines"]),
                      "binary": binary, "patch": patch, "hunks": hunks,
                      "before_sha256": hashlib.sha256(before).hexdigest(),
                      "after_sha256": hashlib.sha256(after).hexdigest()})
    snapshot = {"version": 1, "repo": str(repo), "branch": decode(git(repo, "branch", "--show-current")).strip() or "(detached HEAD)",
                "head": resolve(repo, "HEAD"), "base": "index" if args.scope == "unstaged" else base,
                "target": target or ("index" if args.scope == "staged" else "worktree"),
                "scope": args.scope, "pathspecs": args.path, "exclusion_patterns": args.exclude,
                "excluded": excluded, "layers": layers, "files": files}
    return snapshot


def fingerprint(snapshot):
    data = {k: v for k, v in snapshot.items() if k not in {"captured_at", "fingerprint"}}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def outside_repo(output, repo):
    candidate = Path(output).expanduser().resolve()
    if candidate == repo or repo in candidate.parents:
        raise SnapshotError("Report artifacts must be outside the reviewed repository")
    return candidate


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        temporary = stream.name
    os.replace(temporary, path)


def review_form(snapshot):
    return {"version": 1, "snapshot_fingerprint": snapshot["fingerprint"], "title": "审查报告",
            "goal": "", "non_goals": [], "summary": "", "files": {
                f["id"]: {"bucket": f["bucket"], "category_basis": f["category_basis"],
                          **dict.fromkeys(["purpose", "change", "why", "without", "necessity", "reasonableness", "loss", "risk"], ""),
                          "hunks": {h["id"]: {**dict.fromkeys(["title", "change", "necessity", "reasonableness", "without", "evidence"], ""), "issues": [], "focus": []}
                                    for h in f["hunks"]}}
                for f in snapshot["files"]}, "issues": [], "sections": [], "validations": []}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--scope", choices=["worktree", "staged", "unstaged", "range"], default="worktree")
    parser.add_argument("--base", default="HEAD")
    parser.add_argument("--target")
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--output", required=True)
    parser.add_argument("--review-template")
    args = parser.parse_args()
    try:
        repo = Path(decode(git(Path(args.repo).expanduser(), "rev-parse", "--show-toplevel")).strip()).resolve()
        if args.scope == "range" and not args.target:
            raise SnapshotError("--scope range requires --target; pass explicit --base for the requested range")
        if args.target and args.scope != "range":
            raise SnapshotError("--target is only valid for --scope range")
        if args.scope == "unstaged" and args.base != "HEAD":
            raise SnapshotError("unstaged compares index to disk; --base does not apply")
        output = outside_repo(args.output, repo)
        template = outside_repo(args.review_template, repo) if args.review_template else None
        if template and (template.exists() or template == output):
            raise SnapshotError("Review template already exists or equals snapshot output; choose a new path to preserve annotations")
        base, target = resolve(repo, args.base), resolve(repo, args.target) if args.target else None
        snapshot = freeze(args, repo, base, target)
        # Freeze a coherent input, instead of mixing concurrent edits across files.
        if fingerprint(snapshot) != fingerprint(freeze(args, repo, base, target)):
            raise SnapshotError("Inputs changed during collection; collect again after the edit completes")
        snapshot["captured_at"] = datetime.now(timezone.utc).isoformat()
        snapshot["fingerprint"] = fingerprint(snapshot)
        write_json(output, snapshot)
        if template:
            write_json(template, review_form(snapshot))
        print(json.dumps({"snapshot": str(output), "files": len(snapshot["files"]),
                          "hunks": sum(len(f["hunks"]) for f in snapshot["files"]),
                          "excluded": len(snapshot["excluded"]), "fingerprint": snapshot["fingerprint"]}, ensure_ascii=False))
    except (SnapshotError, OSError, IndexError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
