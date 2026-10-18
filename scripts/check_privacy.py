#!/usr/bin/env python3
"""Privacy guard for the public repository.

Scans tracked files, commit messages, and (optionally) GitHub issue / pull
request text for machine-identifying data and credentials.

Design rules:
* Patterns are generic. This file must never contain a real identifier or
  secret, otherwise the checker itself becomes a leak.
* Findings are printed with the matched value masked, so CI logs stay safe.
* Standard library only; no third-party dependency.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

HOME_PATH = re.compile(
    r"(?<![\w.])(?<![A-Za-z]:)/(?:home|Users)/[A-Za-z0-9._-]+(?:/|\b)"
)
WINDOWS_USER_PATH = re.compile(
    r"(?i)\b[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"']+"
)
PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,80}[A-Za-z0-9+/=]{40,}"
)
GITHUB_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
)
CLOUD_KEY = re.compile(
    r"\b(?:AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{16,})\b"
)
PASSWORD_ASSIGNMENT = re.compile(
    r"(?i)\b(?:password|passwd|pwd)\s*[:=]\s*[\"']([^\"']{8,})[\"']"
)
EMAIL = re.compile(
    r"\b[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)
IPV4 = re.compile(
    r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"
)
SSH_ALIAS = re.compile(
    r"(?<![\w-])ssh\s+([A-Za-z0-9._-]+(?:@[A-Za-z0-9.-]+)?)"
)
LOCAL_ONLY_PATH = re.compile(
    r"(?:^|/)(?:local_info\.md|\.env(?:\.[A-Za-z0-9_.-]+)?|"
    r"[^/]+\.(?:pem|key|p12|pfx)|id_rsa|id_ed25519)$"
)

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("unix-home-path", HOME_PATH),
    ("windows-user-path", WINDOWS_USER_PATH),
    ("github-token", GITHUB_TOKEN),
    ("cloud-key", CLOUD_KEY),
    ("password-assignment", PASSWORD_ASSIGNMENT),
    ("email-address", EMAIL),
    ("ipv4-address", IPV4),
    ("ssh-alias", SSH_ALIAS),
)

#: Rules that must be evaluated over the whole text, not line by line.
MULTILINE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key-block", PRIVATE_KEY_BLOCK),
)

ALLOWED_EMAILS = frozenset(
    {
        "git@github.com",
        "noreply@github.com",
        "agent@localhost",
    }
)
ALLOWED_EMAIL_DOMAINS = frozenset(
    {
        "example.com",
        "example.org",
        "example.net",
        "localhost",
        "users.noreply.github.com",
        "im.bot",
    }
)
ALLOWED_IPS = frozenset({"127.0.0.1", "0.0.0.0", "255.255.255.255"})
DOCUMENTATION_NETWORKS = ("192.0.2.", "198.51.100.", "203.0.113.")
ALLOWED_PASSWORDS = frozenset(
    {"postgres", "localhost", "changeme", "secret", "dummy", "example", "test"}
)
PLACEHOLDER_MARKERS = (
    "example",
    "dummy",
    "fake",
    "placeholder",
    "0123456789abcdef",
    "changeme",
)
ALLOWED_SSH_TARGETS = frozenset(
    {"localhost", "127.0.0.1", "git@github.com"}
)
ALLOWED_LOCAL_FILES = frozenset({".env.example", "local_info.md.example"})


@dataclass(frozen=True)
class Finding:
    """One privacy finding with a masked rendering."""

    rule: str
    location: str
    masked: str


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def is_allowed(rule: str, value: str) -> bool:
    """Return True when a match is an accepted placeholder or safe value."""
    lowered = value.lower()

    if rule == "unix-home-path":
        return lowered.startswith(("/home/runner", "/users/runner"))
    if rule == "windows-user-path":
        normalized = re.sub(r"[\\/]+", "/", lowered)
        return normalized.startswith("c:/users/public")
    if rule in {"github-token", "private-key-block"}:
        return False
    if rule == "cloud-key":
        return _is_placeholder(value)
    if rule == "password-assignment":
        return lowered in ALLOWED_PASSWORDS or _is_placeholder(value)
    if rule == "email-address":
        if lowered in ALLOWED_EMAILS:
            return True
        _, _, domain = lowered.rpartition("@")
        return domain in ALLOWED_EMAIL_DOMAINS
    if rule == "ipv4-address":
        parts = value.split(".")
        if any(int(part) > 255 for part in parts):
            return True
        if value in ALLOWED_IPS:
            return True
        return value.startswith(DOCUMENTATION_NETWORKS)
    if rule == "ssh-alias":
        return lowered in ALLOWED_SSH_TARGETS or lowered.endswith("@github.com")
    return False


def _mask(line: str, spans: Sequence[tuple[int, int]]) -> str:
    """Replace every match span with ``***`` and truncate for CI logs."""
    pieces: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        if start < cursor:
            continue
        pieces.append(line[cursor:start])
        pieces.append("***")
        cursor = end
    pieces.append(line[cursor:])
    masked = "".join(pieces).strip()
    return masked[:160] + ("..." if len(masked) > 160 else "")


def scan_text(text: str) -> list[tuple[str, int, str]]:
    """Scan text and return ``(rule, line_number, masked_line)`` tuples."""
    findings: list[tuple[str, int, str]] = []
    for rule, pattern in MULTILINE_RULES:
        for match in pattern.finditer(text):
            if not is_allowed(rule, match.group(0)):
                line_number = text.count("\n", 0, match.start()) + 1
                findings.append((rule, line_number, "***"))
    for number, line in enumerate(text.splitlines(), start=1):
        spans: list[tuple[int, int, str]] = []
        for rule, pattern in RULES:
            for match in pattern.finditer(line):
                value = match.group(0)
                if is_allowed(rule, value):
                    continue
                spans.append((match.start(), match.end(), rule))
        if spans:
            rules = sorted({rule for _, _, rule in spans})
            findings.append(
                ("+".join(rules), number, _mask(line, [(s, e) for s, e, _ in spans]))
            )
    findings.sort(key=lambda item: item[1])
    return findings


def check_path(path: str) -> str | None:
    """Return the file-name rule when a local-only file is tracked."""
    if LOCAL_ONLY_PATH.search(path):
        name = path.rsplit("/", 1)[-1]
        if name in ALLOWED_LOCAL_FILES:
            return None
        return "local-only-file"
    return None


def _run_git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=True,
    ).stdout


def tracked_files(root: Path) -> Iterator[str]:
    raw = _run_git(root, "ls-files", "-z")
    for item in raw.split(b"\0"):
        if item:
            yield item.decode("utf-8", "surrogateescape")


def scan_files(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for relative in tracked_files(root):
        rule = check_path(relative)
        if rule:
            findings.append(Finding(rule, relative, "***"))
        path = root / relative
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        text = data.decode("utf-8", "replace")
        for rule, line, masked in scan_text(text):
            findings.append(Finding(rule, f"{relative}:{line}", masked))
    return findings


def scan_history(root: Path) -> list[Finding]:
    raw = _run_git(root, "log", "--all", "--format=%H%x1f%B%x1e", "--no-color")
    findings: list[Finding] = []
    for record in raw.split(b"\x1e"):
        record = record.strip(b"\n")
        if not record:
            continue
        sha, _, message = record.partition(b"\x1f")
        subject = sha.decode("ascii", "replace")[:12]
        for rule, line, masked in scan_text(message.decode("utf-8", "replace")):
            findings.append(Finding(rule, f"commit {subject} message:{line}", masked))
    return findings


# ---------------------------------------------------------------------------
# GitHub text scan
# ---------------------------------------------------------------------------


def _gh(args: Sequence[str]) -> str:
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=True
    ).stdout


def _gh_json(args: Sequence[str]):
    return json.loads(_gh(args))


def _repo_slug(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("GITHUB_REPOSITORY")
    if env:
        return env
    return _gh(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]).strip()


def _scan_field(text: str, location: str) -> list[Finding]:
    return [
        Finding(rule, f"{location}:{line}", masked)
        for rule, line, masked in scan_text(text or "")
    ]


def scan_github(repo: str) -> list[Finding]:
    findings: list[Finding] = []
    issues = _gh_json(
        ["issue", "list", "-R", repo, "--state", "all", "--limit", "200",
         "--json", "number,title,body"]
    )
    for issue in issues:
        number = issue["number"]
        findings.extend(_scan_field(issue.get("title", ""), f"issue #{number} title"))
        findings.extend(_scan_field(issue.get("body", ""), f"issue #{number} body"))
        comments = _gh_json(
            ["api", f"repos/{repo}/issues/{number}/comments", "--paginate"]
        )
        for comment in comments:
            findings.extend(
                _scan_field(
                    comment.get("body", ""),
                    f"issue #{number} comment {comment.get('id')}",
                )
            )

    pull_requests = _gh_json(
        ["pr", "list", "-R", repo, "--state", "all", "--limit", "200",
         "--json", "number,title,body"]
    )
    for pull in pull_requests:
        number = pull["number"]
        findings.extend(_scan_field(pull.get("title", ""), f"pr #{number} title"))
        findings.extend(_scan_field(pull.get("body", ""), f"pr #{number} body"))
        comments = _gh_json(
            ["api", f"repos/{repo}/issues/{number}/comments", "--paginate"]
        )
        for comment in comments:
            findings.extend(
                _scan_field(
                    comment.get("body", ""),
                    f"pr #{number} comment {comment.get('id')}",
                )
            )
        comments = _gh_json(
            ["api", f"repos/{repo}/pulls/{number}/comments", "--paginate"]
        )
        for comment in comments:
            findings.extend(
                _scan_field(
                    comment.get("body", ""),
                    f"pr #{number} review comment {comment.get('id')}",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _root_default() -> Path:
    return Path(__file__).resolve().parents[1]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=_root_default())
    parser.add_argument("--github", action="store_true", help="also scan issue/PR text")
    parser.add_argument("--repo", help="owner/name for --github")
    parser.add_argument("--no-history", action="store_true")
    parser.add_argument("--ci", action="store_true", help="CI mode (no color)")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    findings = scan_files(root)
    if not args.no_history:
        findings.extend(scan_history(root))
    if args.github:
        findings.extend(scan_github(_repo_slug(args.repo)))

    if findings:
        print(f"Privacy scan failed: {len(findings)} finding(s).")
        for finding in findings:
            print(f"[{finding.rule}] {finding.location}: {finding.masked}")
        return 1

    print("Privacy scan passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
