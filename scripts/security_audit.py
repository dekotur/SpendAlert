#!/usr/bin/env python3
"""Fail when files intended for publication look like secrets or local data.

This is a conservative guardrail, not a substitute for provider-side secret
scanning or credential rotation after an exposure. Findings intentionally do
not print matched values.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

TEXT_SUFFIXES = {
    "", ".bat", ".cfg", ".css", ".env", ".example", ".html", ".ini",
    ".js", ".json", ".md", ".py", ".sh", ".toml", ".txt", ".xml",
    ".yaml", ".yml",
}

FORBIDDEN_FILENAMES = {".env"}

# Whole categories of file that belong to a deployment rather than to a
# published repository. Named by shape, not by any specific private file, so
# the guard keeps working as the operator's own tooling changes — and so this
# list never becomes an inventory of someone's private infrastructure.
FORBIDDEN_FILENAME_PATTERNS = {
    "systemd unit": re.compile(r"\.(?:service|socket|timer|mount)$", re.IGNORECASE),
    "deployment driver": re.compile(r"^deploy(?:[._-].*)?\.(?:py|sh|ps1|bat)$", re.IGNORECASE),
    "credential module": re.compile(
        r"(?:^|[._-])(?:creds|credentials|secret|secrets|passwords?)(?:[._-]|\.)",
        re.IGNORECASE,
    ),
    "SSH private key": re.compile(r"^id_(?:rsa|dsa|ecdsa|ed25519)$", re.IGNORECASE),
    "shell history or profile": re.compile(r"^\.(?:bash_history|zsh_history|netrc|pgpass)$"),
}

FORBIDDEN_SUFFIXES = {
    ".db", ".key", ".log", ".p12", ".pem", ".pfx", ".sqlite", ".sqlite3",
}

PATTERNS = {
    "Telegram bot token": re.compile(r"(?<![\w-])\d{6,12}:[A-Za-z0-9_-]{30,}(?![\w-])"),
    "API token": re.compile(
        r"(?<![\w-])(?:sk-(?:or-v1-|proj-)?|ghp_|github_pat_|xox[baprs]-|AKIA)"
        r"[A-Za-z0-9_-]{16,}(?![\w-])"
    ),
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "hard-coded bearer token": re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}\b"),
    "Windows absolute path": re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]"),
    "user home path": re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+(?:/|\\)"),
    "public IPv4 address": re.compile(
        r"(?<![\d.])(?!(?:127|10|0)\.)(?!192\.168\.)(?!172\.(?:1[6-9]|2\d|3[01])\.)"
        r"(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"
    ),
    # Absolute paths into a host's service or web roots identify a specific
    # machine and its layout. Written as alternations rather than literals so
    # this file does not itself become the leak it is meant to prevent.
    "host deployment path": re.compile(
        r"/(?:opt|srv|var/www|etc/systemd)/[A-Za-z0-9._-]{2,}/"
    ),
    # A remote login target names a real user and a real host. A placeholder
    # written as <user>@<host> in documentation deliberately does not match.
    "SSH remote target": re.compile(
        r"\b(?:ssh|scp|rsync)\b[^\n]{0,40}?[A-Za-z0-9._-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
    ),
}


def repository_files() -> list[Path]:
    """Inspect tracked plus non-ignored untracked files when Git is available."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return [path for path in ROOT.rglob("*") if path.is_file() and ".git" not in path.parts]

    return [ROOT / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def main() -> int:
    findings: list[tuple[str, str, int | None]] = []

    for path in repository_files():
        relative = path.relative_to(ROOT).as_posix()

        if path.name in FORBIDDEN_FILENAMES:
            findings.append((relative, "forbidden publication file", None))
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            findings.append((relative, "credential or runtime-data file", None))
        for label, pattern in FORBIDDEN_FILENAME_PATTERNS.items():
            if pattern.search(path.name):
                findings.append((relative, label, None))

        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append((relative, label, line))

    if findings:
        print("Security audit failed. Matched values are intentionally hidden:")
        for relative, label, line in sorted(set(findings)):
            location = f"{relative}:{line}" if line else relative
            print(f"- {location}: {label}")
        return 1

    print(f"Security audit passed: {len(repository_files())} publication files checked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
