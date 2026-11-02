#!/usr/bin/env python3
"""HTML sanity check: balanced tags and a few structural expectations.

Uses only html.parser from the standard library. Void elements are allowed to
stand alone; everything else must be closed in order. Also reports:

  * missing <html lang>, <title>, meta description, viewport
  * stray closing tags
  * tags left open at end of file

Usage:  python3 scripts/check_html.py [site_dir]
Exit:   0 clean, 1 problems found.
"""
from __future__ import annotations

import sys
from html.parser import HTMLParser
from pathlib import Path

VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
# Elements whose end tag HTML makes optional. We still expect them to be closed
# in this codebase, but a missing close for one of these is a warning, not an
# error, so a browser would not restructure the page badly.
OPTIONAL = {"p", "li", "td", "th", "tr", "thead", "tbody", "option", "dt", "dd"}


class Balance(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, int]] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.has_title = False
        self.lang = None
        self.meta: dict[str, str] = {}
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "html":
            self.lang = d.get("lang")
        if tag == "title":
            self._in_title = True
            self.has_title = True
        if tag == "meta":
            if d.get("name"):
                self.meta[d["name"]] = d.get("content", "")
            if d.get("charset"):
                self.meta["charset"] = d["charset"]
        if tag in VOID:
            return
        self.stack.append((tag, self.getpos()[0]))

    def handle_startendtag(self, tag, attrs):
        return

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in VOID:
            return
        if not self.stack:
            self.errors.append(f"line {self.getpos()[0]}: stray closing </{tag}>")
            return
        if self.stack[-1][0] == tag:
            self.stack.pop()
            return
        # search down for a match
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                unclosed = [t for t, _ in self.stack[i + 1:]]
                line = self.getpos()[0]
                self.errors.append(
                    f"line {line}: </{tag}> closes while {', '.join('<' + u + '>' for u in unclosed)} still open"
                )
                del self.stack[i:]
                return
        self.errors.append(f"line {self.getpos()[0]}: closing </{tag}> with no matching open tag")


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
    pages = sorted(root.glob("*.html"))
    if not pages:
        print(f"no .html files under {root}")
        return 1

    bad = 0
    for page in pages:
        p = Balance()
        p.feed(page.read_text(encoding="utf-8", errors="replace"))
        p.close()
        errs = list(p.errors)
        for tag, line in p.stack:
            if tag in OPTIONAL:
                p.warnings.append(f"line {line}: <{tag}> left open at end of file (allowed, but close it)")
            else:
                errs.append(f"line {line}: <{tag}> never closed")
        if not p.lang:
            errs.append("<html> is missing a lang attribute")
        if not p.has_title:
            errs.append("missing <title>")
        if not p.meta.get("description"):
            errs.append("missing meta description")
        if not p.meta.get("viewport"):
            errs.append("missing viewport meta tag")
        if p.meta.get("charset", "").lower() != "utf-8":
            errs.append("missing <meta charset=\"utf-8\">")

        if errs:
            bad += 1
            print(f"FAIL {page.name}")
            for e in errs:
                print("  -", e)
        else:
            extra = f" ({len(p.warnings)} warning(s))" if p.warnings else ""
            print(f"ok   {page.name}{extra}")
        for w in p.warnings:
            print("  ~", w)

    print()
    if bad:
        print(f"{bad} of {len(pages)} pages failed")
        return 1
    print(f"OK: {len(pages)} pages balanced, with required head metadata")
    return 0


if __name__ == "__main__":
    sys.exit(main())
