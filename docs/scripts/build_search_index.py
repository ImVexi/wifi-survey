#!/usr/bin/env python3
"""Generate search-index.json from the hand-written HTML pages.

Run this after editing any page:  python3 scripts/build_search_index.py
The output is plain JSON fetched by assets/app.js; there is no runtime build.

Each entry is one searchable line:
  page      file name, used verbatim as the URL
  pageTitle chapter name shown above the hit
  title     the heading text (or the page title)
  anchor    heading id, empty for the page itself
  level     1 = page, 2/3 = heading depth
  text      up to ~200 chars of the section body, for context
"""
from __future__ import annotations

import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

MAX_TEXT = 200


class Indexer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self.entries: list[dict] = []
        self._in_article = 0
        self._in_title = False
        self._heading: dict | None = None
        self._heading_level = 0
        self._skip = 0
        self._page_title = ""

    # -- helpers ---------------------------------------------------------
    def _push(self, entry: dict) -> None:
        self.entries.append(entry)

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "meta" and d.get("name") == "description":
            self.description = (d.get("content") or "").strip()
        if tag == "article":
            self._in_article += 1
        if tag in ("nav", "footer", "script", "style", "header"):
            self._skip += 1
        if tag in ("h1", "h2", "h3") and self._in_article and self._skip == 0:
            anchor = d.get("id") or ""
            level = int(tag[1])
            if level == 1 and not anchor:
                anchor = "top"
            if anchor:
                self._heading = {"page": "", "pageTitle": "", "title": "", "anchor": anchor,
                                 "level": level, "text": ""}
                self._heading_level = level

    def handle_endtag(self, tag):
        if tag in ("nav", "footer", "script", "style", "header") and self._skip:
            self._skip -= 1
        if tag == "article" and self._in_article:
            self._in_article -= 1
        if tag in ("h1", "h2", "h3") and self._heading is not None:
            self._heading["title"] = re.sub(r"\s+", " ", self._heading["title"]).strip()
            if self._heading["level"] == 1:
                # The page-level entry already carries the h1 text, and the h1
                # has no id to anchor to — indexing it would create dead links.
                self._page_title = self._heading["title"]
                self._heading = None
                return
            self._push(self._heading)
            self._heading = None

    def handle_data(self, data):
        if not self._in_article or self._skip:
            return
        text = data.strip()
        if not text:
            return
        if self._heading is not None:
            self._heading["title"] += (" " if self._heading["title"] else "") + text
        elif self.entries:
            cur = self.entries[-1]
            if len(cur["text"]) < MAX_TEXT:
                sep = " " if cur["text"] else ""
                cur["text"] = (cur["text"] + sep + text)[:MAX_TEXT].strip()


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
    pages = sorted(p for p in root.glob("*.html"))
    if not pages:
        print(f"no .html files under {root}")
        return 1

    out: list[dict] = []
    for page in pages:
        p = Indexer()
        p.feed(page.read_text(encoding="utf-8", errors="replace"))
        p.close()
        page_title = clean(p._page_title or page.stem)
        desc = clean(p.description)
        out.append({
            "page": page.name,
            "pageTitle": page_title,
            "title": page_title,
            "anchor": "",
            "level": 1,
            "text": desc,
        })
        for e in p.entries:
            if e["level"] == 1:
                continue  # already represented by the page entry
            e["page"] = page.name
            e["pageTitle"] = page_title
            e["title"] = clean(e["title"])
            e["text"] = clean(e["text"])
            if e["title"]:
                out.append(e)

    target = root / "search-index.json"
    target.write_text(
        json.dumps({"generated_by": "scripts/build_search_index.py", "entries": out},
                   ensure_ascii=False, indent=0) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {target} — {len(out)} entries from {len(pages)} pages, {target.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
