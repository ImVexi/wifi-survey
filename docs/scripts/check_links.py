#!/usr/bin/env python3
"""Static link checker for the Wi-Fi Survey docs site.

Checks, for every .html file in the site root:
  * every relative href/src resolves to a file that exists
  * every "#fragment" (same-page or cross-page) matches an element id
  * no duplicate id attributes on a page
  * no page is orphaned (unreachable from index.html)

External URLs (http/https/mailto/tel/data/javascript) are ignored — this site
ships no external assets, so they should only appear as prose links.

Usage:  python3 scripts/check_links.py [site_dir]
Exit:   0 clean, 1 problems found.
"""
from __future__ import annotations

import re
import sys
from collections import deque
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

SKIP_SCHEMES = ("http://", "https://", "mailto:", "tel:", "data:", "javascript:", "//")


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.links: list[tuple[str, int]] = []
        self._in_article = 0
        self.article_ids: list[str] = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if "id" in d and d["id"]:
            self.ids.append(d["id"])
            if self._in_article:
                self.article_ids.append(d["id"])
        if tag == "article":
            self._in_article += 1
        for key in ("href", "src"):
            if key in d and d[key] is not None:
                self.links.append((d[key], self.getpos()[0]))

    def handle_endtag(self, tag):
        if tag == "article" and self._in_article:
            self._in_article -= 1


def parse(path: Path) -> PageParser:
    p = PageParser()
    p.feed(path.read_text(encoding="utf-8", errors="replace"))
    p.close()
    return p


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
    pages = sorted(root.glob("*.html"))
    if not pages:
        print(f"no .html files under {root}")
        return 1

    parsed = {p.name: parse(p) for p in pages}
    problems: list[str] = []
    edges: dict[str, set[str]] = {p.name: set() for p in pages}

    for name, page in parsed.items():
        seen: dict[str, int] = {}
        for anchor in page.ids:
            if anchor in seen:
                problems.append(f"{name}: duplicate id #{anchor}")
            seen[anchor] = 1

        for raw, line in page.links:
            url = raw.strip()
            if not url or url.startswith("#") and url == "#":
                continue
            low = url.lower()
            if low.startswith(SKIP_SCHEMES):
                continue

            parts = urlsplit(url)
            target_name = unquote(parts.path)
            fragment = unquote(parts.fragment)

            if target_name in ("", "."):
                target = name
            else:
                resolved = (root / target_name).resolve()
                try:
                    resolved.relative_to(root.resolve())
                except ValueError:
                    problems.append(f"{name}:{line}: link escapes site root: {raw}")
                    continue
                if not resolved.exists():
                    problems.append(f"{name}:{line}: missing file: {raw}")
                    continue
                if resolved.is_dir():
                    problems.append(f"{name}:{line}: links to a directory, not a file: {raw}")
                    continue
                target = resolved.name
                if target not in parsed:
                    # A stylesheet, script, JSON index or download: existence is
                    # all that can be checked, and that has already passed.
                    continue
                edges[name].add(target)

            if fragment:
                ids = parsed[target].ids if target in parsed else []
                if fragment not in ids:
                    problems.append(f"{name}:{line}: missing anchor #{fragment} in {target}")

    # orphan check — the chapter list is rendered client-side by app.js, so its
    # PAGES table counts as navigation edges too.
    nav_files = set()
    app_js = root / "assets" / "app.js"
    if app_js.exists():
        nav_files = set(re.findall(r'\["([a-z0-9-]+\.html)"', app_js.read_text(encoding="utf-8")))
    reachable = {"index.html"} | nav_files
    q = deque(sorted(reachable))
    while q:
        cur = q.popleft()
        for nxt in edges.get(cur, ()):
            if nxt not in reachable:
                reachable.add(nxt)
                q.append(nxt)
    for name in parsed:
        if name not in reachable:
            problems.append(f"{name}: orphaned (not reachable from index.html)")

    total_links = sum(len(p.links) for p in parsed.values())
    print(f"scanned {len(pages)} pages, {total_links} href/src attributes")
    if problems:
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print("  -", p)
        return 1
    print("OK: all relative links, anchors and ids resolve; no orphan pages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
