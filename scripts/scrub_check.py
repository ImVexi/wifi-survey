#!/usr/bin/env python3
"""Fail the build if household identifiers leak into the public repository.

Usage:
    python3 scripts/scrub_check.py [--root PATH] [--json]

The denylist is deliberately blunt: a false positive is a prompt to make a
string generic, which is cheaper than shipping somebody's network details.
Test files may contain documentation-range addresses and example BSSIDs;
everything else must use placeholder text.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", "node_modules", "dist", "build", "vendor",
}
SKIP_SUFFIXES = {
    ".pyc", ".pyo", ".so", ".dylib", ".png", ".jpg", ".jpeg", ".gif", ".ico",
    ".woff", ".woff2", ".ttf", ".zip", ".gz", ".tar", ".whl",
}
TEXT_SUFFIXES = {
    "", ".py", ".js", ".mjs", ".css", ".html", ".htm", ".md", ".txt", ".toml",
    ".json", ".yml", ".yaml", ".sh", ".service", ".ini", ".cfg", ".ino",
    ".example", ".template", ".xml", ".svg", ".gitignore", ".dockerignore",
}

I = re.IGNORECASE
# (pattern, rule, flags). Case-insensitive only where a name could be typed in
# any case; environment variables and paths stay case-sensitive.
DENY_PATTERNS: list[tuple[str, str, int]] = [
    (r"\b10\.0\.\d{1,3}\.\d{1,3}\b", "private 10.0.0.0/8 address", 0),
    (r"\b192\.168\.\d{1,3}\.\d{1,3}\b", "private 192.168.0.0/16 address", 0),
    (r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b", "private 172.16/12 address", 0),
    (r"\bcachyos\b", "household hostname", I),
    (r"\bLLM\b", "household hostname", 0),
    (r"\b[a-z0-9][a-z0-9-]*\.local\b", "mDNS name (only survey.local is allowed)", I),
    (r"\b(?:vex|veex|jayden87|jayden)\b", "household user name", I),
    (r"\bVextroboomin\b", "household SSID", I),
    (r"\bVextroIOT\b", "household SSID", I),
    (r"\bpullen[ _-]?crib\b", "household SSID", I),
    (r"\bxfinity\w*\b", "household/neighbour SSID", I),
    (r"\bBedroom U7\b", "household AP name", I),
    (r"\bFamily room AC pro\b", "household AP name", I),
    (r"\bBasement AC pro\b", "household AP name", I),
    (r"/root(?:/|\b)", "absolute path under /root", 0),
    (r"/var/lib/wifi-survey\b", "household data directory", 0),
    (r"/etc/wifi-survey\b", "household config directory", 0),
    (r"\bhouse_shell\.glb\b", "household GLB", I),
    (r"\b11_1_2026\.glb\b", "household scan file", 0),
    (r"\bgeom2d\.pkl\b", "household reconstruction artifact", 0),
    (r"\bnodes\.pkl\b", "household reconstruction artifact", 0),
    (r"\bunifi\.local\b", "household controller host", I),
    # Room names from the household model, outside tests.
    (r"\bBasement\b", "household room name", I),
    (r"\bMain floor\b", "household room name", I),
    (r"\bUpper floor\b", "household room name", I),
    (r"\bLiving room\b", "household room name", I),
    (r"\bFamily room\b", "household room name", I),
    (r"\bGarage\b", "household room name", I),
    (r"\bDining room\b", "household room name", I),
    (r"\bMechanical room\b", "household room name", I),
]

ALLOWED_LOCAL_NAMES = {
    "survey.local", "name.local", "host.local", "example.local", "s.local",
}
TEST_MARKERS = ("/tests/", "/test_", "fake_nmcli", "/fixtures/")
MAC_RE = re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b")
HOUSE_GLB_RE = re.compile(r"(?:house[_-]?shell|house|scan|home)[-_0-9]*\.glb$", re.IGNORECASE)
SELF = Path(__file__).resolve()


@dataclass
class Finding:
    path: str
    line: int
    rule: str
    text: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.text.strip()[:160]}"


def is_test_file(path: Path, root: Path) -> bool:
    rel = "/" + path.relative_to(root).as_posix()
    return any(marker in rel for marker in TEST_MARKERS)


def iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in SKIP_DIRS and not name.startswith(".tmp-")
        )
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink() or path == SELF:
                continue
            suffix = path.suffix.lower()
            if suffix in SKIP_SUFFIXES and suffix != ".svg":
                continue
            if suffix not in TEXT_SUFFIXES and path.name not in {"Dockerfile", "Makefile", "LICENSE"}:
                continue
            yield path


def check_text(path: Path, root: Path, findings: list[Finding]) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    is_test = is_test_file(path, root)
    rel = path.relative_to(root).as_posix()
    for line_no, line in enumerate(text.splitlines(), 1):
        for pattern, rule, flags in DENY_PATTERNS:
            if is_test and rule == "household room name":
                continue
            for match in re.finditer(pattern, line, flags=flags):
                value = match.group(0)
                if rule.startswith("mDNS name") and value.lower() in ALLOWED_LOCAL_NAMES:
                    continue
                findings.append(Finding(rel, line_no, rule, line))
                break
        if not is_test:
            for match in MAC_RE.finditer(line):
                findings.append(Finding(rel, line_no, "MAC address outside a test", match.group(0)))
    if path.suffix.lower() == ".glb":
        if not rel.startswith("tests/fixtures/"):
            findings.append(Finding(rel, 0, "GLB outside tests/fixtures", rel))
        elif HOUSE_GLB_RE.match(path.name) and path.name != "sample_house.glb":
            findings.append(Finding(rel, 0, "non-sample house model", rel))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check for household identifiers")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    findings: list[Finding] = []
    files = 0
    for path in iter_files(root):
        files += 1
        check_text(path, root, findings)
    if args.json:
        print(json.dumps({"files": files, "findings": [f.__dict__ for f in findings]}, indent=2))
    else:
        for finding in findings:
            print(finding.render())
        print(f"\nscrub_check: {files} files scanned, {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
