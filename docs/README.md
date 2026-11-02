# docs/ — the documentation site

This folder is the published documentation for Wi-Fi Survey: seventeen
hand-written HTML pages, one stylesheet, one script. **No build step, no CDN, no
fonts to download, no analytics, no cookies.** GitHub Pages serves it from branch
`main`, folder `/docs` — <https://jpullen724-stack.github.io/wifi-survey/>.

## Layout

```
docs/
├── index.html … changelog.html   17 pages, one per chapter
├── assets/
│   ├── style.css                 the whole design system
│   └── app.js                    sidebar, TOC, search, copy buttons, pager
├── search-index.json             generated, do not hand-edit
├── _headers                      Cloudflare Pages headers (unused on GitHub Pages)
├── .nojekyll                     GitHub Pages: skip Jekyll
├── scripts/
│   ├── check_links.py            relative links, anchors, ids, orphans
│   ├── check_html.py             balanced tags + required head metadata
│   └── build_search_index.py     regenerates search-index.json
└── README.md
```

## Previewing

It is static HTML. Serve the folder (the search box needs `http://`, not
`file://`):

```bash
python3 -m http.server 8080 --directory docs
# then open http://127.0.0.1:8080/
```

## Editing

- Every page is self-contained HTML at the site root, so `assets/…` and
  `page.html` links work from any subpath — no leading `/` anywhere.
- Page chrome (sidebar, TOC, prev/next, copy buttons) is rendered by
  `assets/app.js`. Chapter order lives in the `PAGES` array at the top of that
  file; add a page there and to the nav, then give the new page
  `<body data-page="your-page">`.
- Use only the classes documented at the top of `style.css`. Tables must be
  wrapped in `<div class="table-wrap">`.
- Say **planned** for anything that is not in the release. The project ships a
  scaffold; several chapters describe the intended survey engine and carry a
  `.callout.limit` banner saying so. Do not remove those banners until the code
  behind them exists.

After editing, run all three — they exit non-zero on failure, so they can gate CI:

```bash
python3 scripts/build_search_index.py
python3 scripts/check_links.py
python3 scripts/check_html.py
```

## Style rules

- British/neutral English, second person, no marketing adjectives.
- Prefer a table or a numbered procedure over a wall of prose.
- If something is not supported, say so in a `.callout.limit` block instead of
  softening it.
- No images are shipped: diagrams are inline SVG or CSS, screenshots are
  placeholders until real captures exist.
- Never commit household identifiers: no private IPs, MAC addresses, real SSIDs
  or hostnames. `python3 scripts/scrub_check.py` (repository root) enforces it.
