"""只读发布检查：扫描候选源码及所有可达历史/快照 blob，绝不打印匹配值。"""

import json
from pathlib import Path
import re
import subprocess

PROJECT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "疑似服务密钥": re.compile(r"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16})"),
    "私钥": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "疑似个人邮箱": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "疑似手机号": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "疑似凭据赋值": re.compile(r"(?i)(?:api[_-]?key|secret|access[_-]?token)\s*[=:]\s*['\"]([A-Za-z0-9_./+\-=]{24,})['\"]"),
}


def git(*args, cwd=PROJECT):
    result = subprocess.run(["git", "--no-optional-locks", *args], cwd=cwd, capture_output=True, check=True)
    return result.stdout


def scan_text(text):
    categories = set()
    for category, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            value = match.group(1) if match.lastindex else match.group()
            if category == "疑似个人邮箱" and (value.endswith(("@example.com", "@example.org", ".invalid", "@users.noreply.github.com"))):
                continue
            if category == "疑似凭据赋值" and value.lower().startswith(("mock-", "test-", "fake-", "your-")):
                continue
            categories.add(category)
    return sorted(categories)


def scan_content(raw):
    """按内容而非后缀区分二进制；文本仍使用完整原有检测规则。

    返回（风险类别、跳过原因）。跳过不是安全认证，图片及元数据需人工复核。
    不允许仅把含密钥的文本改成 .png 就绕过扫描。
    """
    signatures = ((b"\x89PNG\r\n\x1a\n", "PNG"), (b"\xff\xd8\xff", "JPEG"),
                  (b"GIF87a", "GIF"), (b"GIF89a", "GIF"),
                  (b"%PDF-", "PDF"), (b"PK\x03\x04", "ZIP"),
                  (b"\x1f\x8b", "GZIP"))
    for signature, kind in signatures:
        if raw.startswith(signature):
            return [], f"{kind}二进制未作文本扫描；需人工检查内容及元数据"
    try:
        # BOM 文本同样必须检测，不能因 UTF-16/32 的 NUL 字节而误跳过。
        if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            text = raw.decode("utf-32")
        elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = raw.decode("utf-16")
        else:
            text = raw.decode("utf-8-sig")
        if "\x00" in text:
            return [], "未知二进制或非标准文本编码，需人工检查"
    except UnicodeError:
        return [], "未知二进制或非标准文本编码，需人工检查"
    return scan_text(text), None


def audit():
    repo = Path(git("rev-parse", "--show-toplevel").decode().strip())
    relative = PROJECT.relative_to(repo).as_posix()
    candidates = git("ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", ".").decode().split("\0")
    findings, incomplete, skipped_binary = [], [], []
    for name in filter(None, candidates):
        path = PROJECT / name
        if not path.is_file():
            continue
        if path.stat().st_size > 5 * 1024 * 1024:
            incomplete.append({"scope": "working_tree", "path": name, "reason": "超过5MB，需人工检查"})
            continue
        categories, skipped = scan_content(path.read_bytes())
        if skipped:
            record = {"scope": "working_tree", "path": name, "reason": skipped}
            (incomplete if skipped.startswith("未知") else skipped_binary).append(record)
        if categories:
            findings.append({"scope": "working_tree", "path": name, "categories": categories})
    objects = git("rev-list", "--objects", "--all", cwd=repo).decode().splitlines()
    blobs = 0
    for entry in objects:
        oid, _, name = entry.partition(" ")
        if git("cat-file", "-t", oid, cwd=repo).strip() != b"blob":
            continue
        blobs += 1
        size = int(git("cat-file", "-s", oid, cwd=repo))
        if size > 5 * 1024 * 1024:
            incomplete.append({"scope": "history", "object": oid[:12], "path": name, "reason": "超过5MB，需人工检查"})
            continue
        categories, skipped = scan_content(git("cat-file", "-p", oid, cwd=repo))
        if skipped:
            record = {"scope": "history", "object": oid[:12], "path": name, "reason": skipped}
            (incomplete if skipped.startswith("未知") else skipped_binary).append(record)
        if categories:
            findings.append({"scope": "history", "object": oid[:12], "path": name, "categories": categories})
    commits = git("rev-list", "--all", cwd=repo).decode().splitlines()
    metadata_findings = sum(bool(scan_text(git("show", "-s", "--format=%ae%n%ce%n%B", commit, cwd=repo).decode(errors="replace"))) for commit in commits)
    project_commits = git("rev-list", "--all", "--", relative, cwd=repo).decode().splitlines() if relative != "." else commits
    return {
        "project_is_repo_root": PROJECT == repo,
        "project_reachable_commits": len(project_commits), "parent_reachable_commits": len(commits),
        "history_blobs_scanned": blobs, "candidate_files_scanned": len(list(filter(None, candidates))),
        "findings": findings, "history_metadata_needing_review": metadata_findings, "incomplete": incomplete,
        "skipped_binary": skipped_binary,
        "status": "STOP_REVIEW_REQUIRED" if findings or incomplete or metadata_findings else "NO_PATTERN_MATCHES",
        "limitations": "启发式扫描不保证检出所有秘密或个人信息；已识别二进制仅记录跳过，需人工复核，不代表安全通过。不包含不可达对象、reflog、Git LFS及外部数据。未执行任何发布。",
    }


if __name__ == "__main__":
    try:
        report = audit()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise SystemExit(1 if report["status"] == "STOP_REVIEW_REQUIRED" else 0)
    except subprocess.CalledProcessError:
        print("Git 扫描失败；未输出命令原文或 stderr，请人工核查。")
        raise SystemExit(2)
