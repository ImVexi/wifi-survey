/* Wi-Fi Survey docs — shared client behaviour.
 *
 * Everything here is progressive enhancement over plain HTML:
 *   - builds the sidebar chapter list from the PAGES table below
 *   - builds an "On this page" table of contents from h2/h3 ids
 *   - highlights the section you are reading
 *   - filters search-index.json in the search box
 *   - adds copy buttons to every <pre>
 *   - drives the mobile off-canvas navigation
 *
 * No dependencies, no network access except the same-origin search-index.json.
 */
(function () {
  "use strict";

  /* Chapter order — also the prev/next order. Keep in sync with the files. */
  var PAGES = [
    { group: "Getting started", items: [
      ["index.html", "Overview"],
      ["install.html", "Install"],
      ["quickstart.html", "Quickstart"]
    ]},
    { group: "Fundamentals", items: [
      ["concepts.html", "Concepts"],
      ["scanner-nodes.html", "Scanner nodes"],
      ["model-import.html", "3D model import"]
    ]},
    { group: "Surveying", items: [
      ["survey.html", "Running a survey"],
      ["heatmaps.html", "Heatmaps"],
      ["propagation-model.html", "Propagation model"],
      ["position.html", "Live position"]
    ]},
    { group: "Network & tuning", items: [
      ["unifi.html", "UniFi integration"],
      ["tuning.html", "Tuning recommendations"]
    ]},
    { group: "Help", items: [
      ["troubleshooting.html", "Troubleshooting"],
      ["faq.html", "FAQ"]
    ]},
    { group: "Project", items: [
      ["privacy.html", "Privacy"],
      ["contributing.html", "Contributing"],
      ["changelog.html", "Changelog"]
    ]}
  ];

  var FLAT = [];
  PAGES.forEach(function (g) {
    g.items.forEach(function (it) { FLAT.push({ file: it[0], title: it[1] }); });
  });

  var body = document.body;
  var current = body.getAttribute("data-page") || "index";
  /* Pages set data-page to the file stem ("heatmaps"); the nav table and the
     pager use file names ("heatmaps.html"). Normalise once, here. */
  var currentFile = current.indexOf(".") === -1 ? current + ".html" : current;


  /* ---------------------------------------------------------------- sidebar */

  var sidebar = document.getElementById("sidebar");
  if (sidebar) {
    var frag = document.createDocumentFragment();
    PAGES.forEach(function (group) {
      var wrap = document.createElement("div");
      wrap.className = "nav-group";
      var h = document.createElement("p");
      h.className = "nav-group-title";
      h.textContent = group.group;
      wrap.appendChild(h);

      var ul = document.createElement("ul");
      ul.className = "nav-list";
      group.items.forEach(function (it) {
        var li = document.createElement("li");
        var a = document.createElement("a");
        a.className = "nav-link";
        a.href = it[0];
        a.textContent = it[1];
        if (it[0] === currentFile) {
          a.classList.add("active");
          a.setAttribute("aria-current", "page");
        }
        li.appendChild(a);
        ul.appendChild(li);
      });
      wrap.appendChild(ul);
      frag.appendChild(wrap);
    });
    sidebar.appendChild(frag);
  }

  /* -------------------------------------------------------------------- TOC */

  var article = document.querySelector(".content > article");
  var tocHost = null;
  if (sidebar && article) {
    var heads = article.querySelectorAll("h2[id], h3[id]");
    if (heads.length > 1) {
      tocHost = document.createElement("nav");
      tocHost.className = "toc";
      tocHost.setAttribute("aria-label", "On this page");
      var t = document.createElement("p");
      t.className = "toc-title";
      t.textContent = "On this page";
      tocHost.appendChild(t);
      var list = document.createElement("ul");
      list.className = "toc-list";
      Array.prototype.forEach.call(heads, function (h) {
        var li = document.createElement("li");
        var a = document.createElement("a");
        a.href = "#" + h.id;
        a.className = "toc-link level-" + h.tagName.charAt(1);
        a.textContent = h.textContent.replace(/¶$/, "").trim();
        li.appendChild(a);
        list.appendChild(li);
      });
      tocHost.appendChild(list);
      sidebar.appendChild(tocHost);
    }
  }

  /* -------------------------------------------------------------- scrollspy */

  var tocLinks = tocHost ? Array.prototype.slice.call(tocHost.querySelectorAll("a")) : [];
  var spyTargets = tocLinks.map(function (a) {
    return document.getElementById(a.getAttribute("href").slice(1));
  }).filter(Boolean);

  function syncToc() {
    if (!spyTargets.length) return;
    var line = 110;
    var best = 0;
    for (var i = 0; i < spyTargets.length; i++) {
      if (spyTargets[i].getBoundingClientRect().top <= line) best = i;
    }
    /* If we are at the very bottom, highlight the last heading. */
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) {
      best = spyTargets.length - 1;
    }
    tocLinks.forEach(function (a, i) {
      if (i === best) a.classList.add("active");
      else a.classList.remove("active");
    });
  }

  var ticking = false;
  window.addEventListener("scroll", function () {
    if (ticking) return;
    ticking = true;
    window.requestAnimationFrame(function () { syncToc(); ticking = false; });
  }, { passive: true });
  window.addEventListener("resize", syncToc, { passive: true });
  syncToc();

  /* ------------------------------------------------------------------ pager */

  var idx = -1;
  for (var i = 0; i < FLAT.length; i++) {
    if (FLAT[i].file === currentFile) { idx = i; break; }
  }
  if (article && idx !== -1) {
    var prev = idx > 0 ? FLAT[idx - 1] : null;
    var next = idx < FLAT.length - 1 ? FLAT[idx + 1] : null;
    if (prev || next) {
      var pager = document.createElement("nav");
      pager.className = "pager";
      pager.setAttribute("aria-label", "Chapter navigation");
      if (prev) {
        pager.insertAdjacentHTML("beforeend",
          '<a class="pager-prev" href="' + prev.file + '"><span class="pager-label">Previous</span>' +
          '<span class="pager-title">' + prev.title + "</span></a>");
      }
      if (next) {
        pager.insertAdjacentHTML("beforeend",
          '<a class="pager-next" href="' + next.file + '"><span class="pager-label">Next</span>' +
          '<span class="pager-title">' + next.title + "</span></a>");
      }
      article.appendChild(pager);
    }
  }

  /* ----------------------------------------------------------------- mobile */

  var toggle = document.getElementById("navToggle");
  var scrim = null;

  function closeNav() {
    body.classList.remove("nav-open");
    if (toggle) toggle.setAttribute("aria-expanded", "false");
    if (scrim) scrim.hidden = true;
  }
  function openNav() {
    if (!scrim) {
      scrim = document.createElement("div");
      scrim.className = "scrim";
      scrim.hidden = true;
      scrim.addEventListener("click", closeNav);
      document.body.appendChild(scrim);
    }
    body.classList.add("nav-open");
    scrim.hidden = false;
    if (toggle) toggle.setAttribute("aria-expanded", "true");
  }

  if (toggle) {
    toggle.addEventListener("click", function () {
      if (body.classList.contains("nav-open")) closeNav();
      else openNav();
    });
  }
  if (sidebar) {
    sidebar.addEventListener("click", function (e) {
      if (e.target.closest("a") && window.matchMedia("(max-width: 940px)").matches) closeNav();
    });
  }
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { closeNav(); closeSearch(); }
  });

  /* ------------------------------------------------------------ copy buttons */

  Array.prototype.forEach.call(document.querySelectorAll("pre"), function (pre) {
    if (pre.querySelector("button.copy-btn")) return;
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copy-btn";
    btn.textContent = "Copy";
    btn.setAttribute("aria-label", "Copy code to clipboard");
    btn.addEventListener("click", function () {
      var code = pre.querySelector("code");
      var text = code ? code.textContent : pre.textContent;
      var done = function (ok) {
        btn.textContent = ok ? "Copied" : "Press Ctrl+C";
        btn.classList.toggle("copied", ok);
        setTimeout(function () {
          btn.textContent = "Copy";
          btn.classList.remove("copied");
        }, 1600);
      };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
      } else {
        try {
          var ta = document.createElement("textarea");
          ta.value = text;
          ta.style.position = "fixed";
          ta.style.opacity = "0";
          document.body.appendChild(ta);
          ta.select();
          var ok = document.execCommand("copy");
          document.body.removeChild(ta);
          done(ok);
        } catch (err) { done(false); }
      }
    });
    pre.appendChild(btn);
  });

  /* ----------------------------------------------------------------- search */

  var input = document.getElementById("searchInput");
  var panel = document.getElementById("searchResults");
  var indexPromise = null;
  var hits = [];
  var activeHit = -1;

  function loadIndex() {
    if (!indexPromise) {
      indexPromise = fetch("search-index.json", { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : { entries: [] }; })
        .then(function (data) { return Array.isArray(data.entries) ? data.entries : []; })
        .catch(function () { return []; });
    }
    return indexPromise;
  }

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function highlight(text, q) {
    var safe = escapeHtml(text);
    if (!q) return safe;
    var i = safe.toLowerCase().indexOf(q.toLowerCase());
    if (i === -1) return safe;
    return safe.slice(0, i) + "<mark>" + safe.slice(i, i + q.length) + "</mark>" + safe.slice(i + q.length);
  }

  function closeSearch() {
    if (panel) panel.hidden = true;
    activeHit = -1;
  }

  function render(q) {
    if (!panel) return;
    var query = q.trim().toLowerCase();
    if (query.length < 2) { closeSearch(); return; }

    var terms = query.split(/\s+/).filter(Boolean);
    var results = [];

    hits.forEach(function (e) {
      var title = (e.title || "").toLowerCase();
      var text = (e.text || "").toLowerCase();
      var page = (e.page || "").toLowerCase();
      var score = 0;
      var matchedAll = terms.every(function (tm) {
        var s = 0;
        if (title.indexOf(tm) !== -1) s += 12;
        if (page.indexOf(tm) !== -1) s += 5;
        if (text.indexOf(tm) !== -1) s += 3;
        score += s;
        return s > 0;
      });
      if (!matchedAll) return;
      if (e.level === 1) score += 4;
      results.push({ e: e, score: score });
    });

    results.sort(function (a, b) { return b.score - a.score; });
    results = results.slice(0, 12);

    if (!results.length) {
      panel.innerHTML = '<div class="search-empty">No matches for “' + escapeHtml(q.trim()) + '”. Try “kriging”, “pairing” or “rollback”.</div>';
      panel.hidden = false;
      activeHit = -1;
      return;
    }

    panel.innerHTML = results.map(function (r, i) {
      var e = r.e;
      var url = e.page + (e.anchor ? "#" + e.anchor : "");
      return '<a class="search-hit' + (i === 0 ? " is-active" : "") + '" href="' + url + '">' +
        '<span class="hit-page">' + escapeHtml(e.pageTitle || e.page) + "</span>" +
        '<span class="hit-title">' + highlight(e.title, q.trim()) + "</span>" +
        '<span class="hit-snippet">' + highlight(e.text || "", q.trim()) + "</span></a>";
    }).join("");
    panel.hidden = false;
    activeHit = 0;
  }

  function moveHit(delta) {
    if (!panel || panel.hidden) return;
    var links = panel.querySelectorAll(".search-hit");
    if (!links.length) return;
    if (activeHit >= 0 && links[activeHit]) links[activeHit].classList.remove("is-active");
    activeHit = (activeHit + delta + links.length) % links.length;
    links[activeHit].classList.add("is-active");
    links[activeHit].scrollIntoView({ block: "nearest" });
  }

  if (input && panel) {
    loadIndex().then(function (entries) {
      hits = entries.filter(function (e) { return e && e.page && e.title; });
    });

    input.addEventListener("input", function () { render(input.value); });
    input.addEventListener("focus", function () {
      loadIndex().then(function () { if (input.value.trim().length >= 2) render(input.value); });
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); moveHit(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); moveHit(-1); }
      else if (e.key === "Enter") {
        var links = panel.querySelectorAll(".search-hit");
        if (links.length) {
          e.preventDefault();
          (links[activeHit] || links[0]).click();
        }
      } else if (e.key === "Escape") {
        closeSearch();
        input.blur();
      }
    });
    document.addEventListener("click", function (e) {
      if (!e.target.closest(".search")) closeSearch();
    });
  }

  /* Keyboard shortcut: "/" focuses search, like most doc sites. */
  document.addEventListener("keydown", function (e) {
    if (e.key === "/" && input && document.activeElement !== input) {
      var tag = (document.activeElement && document.activeElement.tagName) || "";
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      e.preventDefault();
      input.focus();
      input.select();
    }
  });
})();
