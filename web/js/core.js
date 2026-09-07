/*
 * core.js - shared foundation for the Music Library static site.
 *
 * Runs from file:// with plain <script> tags (no ES modules, no fetch). Every data file in
 * web/data/*.js calls MLG.register(key, value) to populate MLG.data before this boots the page.
 *
 * Responsibilities:
 *   - the window.MLG namespace + data registry
 *   - settings persisted in localStorage (theme, library base directory)
 *   - the shared header / footer chrome, nav highlighting, breadcrumbs
 *   - formatters and label/badge helpers shared by every renderer
 *   - the global cross-entity search
 *   - client-side composition of absolute file:// links to the original PDFs
 */
(function () {
  "use strict";

  var MLG = (window.MLG = window.MLG || {});
  MLG.data = MLG.data || {};

  // --- data registry -------------------------------------------------------
  MLG.register = function (key, value) {
    MLG.data[key] = value;
    return value;
  };
  MLG.get = function (key, fallback) {
    return Object.prototype.hasOwnProperty.call(MLG.data, key) ? MLG.data[key] : fallback;
  };

  // --- the 8 pipeline phases (static metadata; metrics come from MLG.data.phases) ---
  // `blurb` is the one-line summary shown on the dashboard phase cards. `detail` is the richer,
  // multi-paragraph explanation rendered at the top of each phase page: what the stage does, how
  // it works, and why the approach was chosen.
  MLG.PHASES = [
    {
      n: 1, slug: "phase-01-inventory", title: "Inventory",
      blurb: "Scan the library and group every PDF into pieces.",
      detail:
        "<p><strong>What it does.</strong> Walks the entire library tree and records every PDF exactly once, " +
        "grouping files into <em>pieces</em> by their containing folder. Each file is fingerprinted " +
        "(content hash + size + modified time) and written to a checkpoint.</p>" +
        "<p><strong>Why it works this way.</strong> The rest of the pipeline is expensive &mdash; it renders pages, " +
        "runs OCR, and calls language models &mdash; so we never want to reprocess a file that has not changed. The " +
        "fingerprint lets later runs skip untouched files and only redo the pieces that were actually added or edited.</p>" +
        "<p><strong>The problem it solves.</strong> A real library is a messy pile of nested folders with duplicate and " +
        "renamed files. This phase turns that pile into one stable, de-duplicated inventory keyed by piece and document, " +
        "so every downstream stage has a single source of truth to join against instead of re-scanning the disk.</p>",
    },
    {
      n: 2, slug: "phase-02-extract", title: "Extract & Read",
      blurb: "Render pages, OCR the scans, and vision-analyse each document.",
      detail:
        "<p><strong>What it does.</strong> For every PDF it pulls any embedded (born-digital) text, renders each page " +
        "to an image, and OCRs the pages that have no usable text layer. Alongside the text it measures objective " +
        "image-quality signals per page &mdash; resolution/DPI, blur, skew, contrast, and blankness &mdash; and consolidates the " +
        "noisy OCR reads into a clean list of instrument tokens.</p>" +
        "<p><strong>Why it works this way.</strong> Much of the collection is scanned paper, so the PDFs carry no real " +
        "text to search. We render and OCR those pages to recover their words, but OCR on old, skewed, or faint scans is " +
        "unreliable &mdash; so we also record how trustworthy each page looks and reconcile the raw OCR through a model pass " +
        "rather than trusting a single noisy read.</p>" +
        "<p><strong>The problem it solves.</strong> Every later phase needs to <em>read</em> the music &mdash; the part name in " +
        "the corner, the title, the instrument list on a score. This stage is the one place that turns pixels into text " +
        "and quality metrics, so nothing downstream ever has to open a PDF again.</p>",
    },
    {
      n: 3, slug: "phase-03-classify", title: "Part Classification",
      blurb: "Predict the instrument / part each document represents.",
      detail:
        "<p><strong>What it does.</strong> Decides which instrument/part each document is (Flute 1, Trombone 2, a full " +
        "score, &hellip;) using a rule-first, deterministic cascade. It starts from a baseline read of the filename and then " +
        "lets more trustworthy in-file evidence override it, in a fixed order of precedence:</p>" +
        "<ol>" +
        "<li><strong>OCR&rarr;LLM consolidation</strong> &mdash; the document-level instrument list reconciled in Phase 2. " +
        "This is the strongest in-file signal, because it already merged and cleaned the raw OCR.</li>" +
        "<li><strong>Printed label in the upper-left corner</strong> &mdash; the part name engravers print at the top-left of " +
        "the page. It is read <em>earliest-match-first</em> so the header label wins over anything further down, and it may " +
        "override the filename even across instrument families (flagged for review when it does).</li>" +
        "<li><strong>Same-section footer / credit instrument</strong> &mdash; a name in the footer or engraver credit may only " +
        "relabel within the same section (e.g. Baritone &rarr; Euphonium); it can refine, but not overturn, the filename.</li>" +
        "</ol>" +
        "<p><strong>Why a cascade instead of one method.</strong> No single signal is reliable on its own. Filenames are " +
        "convenient but full of typos, generic names, and mislabels. The printed label is authoritative but comes through " +
        "noisy OCR. So we combine several signals with an explicit precedence and a confidence tier: trust the filename when " +
        "nothing better exists, but let the page itself override it when they disagree.</p>" +
        "<p><strong>The heuristics we guard against.</strong> Parts routinely print <em>cue</em> notes from another instrument " +
        '(an "Oboe cue" inside a clarinet part), which would fool a naive keyword match &mdash; so <code>&lt;instrument&gt; cue</code> ' +
        "annotations are stripped before matching and can never win. The instrument vocabulary lives in an editable lexicon " +
        "(<code>config/regex_rules.yaml</code>) with a built-in fallback, and every decision records the evidence source that " +
        "produced it so the result is auditable rather than a black box.</p>",
    },
    {
      n: 4, slug: "phase-04-expected", title: "Expected Instrumentation",
      blurb: "Infer the expected part set for each piece.",
      detail:
        "<p><strong>What it does.</strong> Works out which parts a piece <em>should</em> contain &mdash; its instrumentation " +
        "contract &mdash; so missing parts can be detected. It runs up to three stages and stops at the first that returns a " +
        "confident answer:</p>" +
        "<ol>" +
        "<li><strong>Local score OCR</strong> &mdash; if the piece already includes a full score PDF, read its instrument list " +
        "directly (re-OCRing only the leading pages when the existing text is too thin) and summarise it into the contract.</li>" +
        "<li><strong>Online authority lookup</strong> &mdash; ask an assistant to find the actual published score online and " +
        "return its real instrumentation from authoritative sources, plus candidate score-image URLs.</li>" +
        "<li><strong>Remote image OCR</strong> &mdash; when the lookup finds score images but no text, download and OCR those " +
        "images locally, then summarise them into the contract.</li>" +
        "</ol>" +
        "<p><strong>Why it works this way.</strong> To know what is <em>missing</em> you first have to know what should be " +
        "present, and there is no manifest that tells us. So we try the cheapest trustworthy source first (a score sitting in " +
        "the folder), fall back to an online authority, and only then to OCR of images found online &mdash; spending effort in " +
        "proportion to how hard the answer is to get.</p>" +
        "<p><strong>The problem it solves.</strong> When no stage is confident, the piece degrades to a conservative " +
        "observed-only record flagged for review. It never invents a &ldquo;missing&rdquo; part without an authoritative " +
        "source, so completeness numbers stay honest.</p>",
    },
    {
      n: 5, slug: "phase-05-quality", title: "Quality Checks",
      blurb: "Score scan quality and flag legibility issues.",
      detail:
        "<p><strong>What it does.</strong> Scores how legible each document is and classifies its notation source " +
        "(printed/engraved vs. handwritten). It <em>reuses</em> the objective per-page metrics Phase 2 already measured " +
        "&mdash; resolution, skew, contrast, blur, OCR confidence, blankness &mdash; evaluates each page against configurable " +
        "thresholds (<code>config/quality_thresholds.yaml</code>), and rolls the findings up into a per-document quality band.</p>" +
        "<p><strong>Why it works this way.</strong> The engine is deterministic and threshold-driven rather than model-based, " +
        "so the same scan always gets the same score and every flag can be traced to a concrete measurement. Crucially, it " +
        "never raises an issue from a missing (null) metric &mdash; absence of a measurement is not evidence of a defect.</p>" +
        "<p><strong>The problem it solves.</strong> It separates &ldquo;we could not read this well&rdquo; from &ldquo;a part " +
        "is actually missing.&rdquo; Quality, legibility, and handwriting are informational (good / fair / poor, plus a " +
        "handwritten flag): they help a librarian judge a scan, but they never on their own mark a piece incomplete.</p>" +
        "<p><strong>What the bands mean.</strong> Every document earns a 0&ndash;100 quality score from its per-page " +
        "measurements (resolution, skew, contrast, blur, OCR confidence, blankness/noise); the score maps to one band:</p>" +
        "<ul>" +
        "<li><strong>Good &mdash; score 80&ndash;100.</strong> <em>Technically:</em> pages clear the strict thresholds on " +
        "every metric &mdash; high resolution, near-zero skew, strong contrast, low blur, confident OCR. <em>In plain terms:</em> " +
        "a clean, sharp scan you can read and play from as-is; no attention needed.</li>" +
        "<li><strong>Fair &mdash; score 50&ndash;79.</strong> <em>Technically:</em> one or more metrics fall into the middle " +
        "range &mdash; some blur, a slight tilt, softer contrast, marginal resolution, or a little page noise &mdash; without " +
        "any single metric failing outright. <em>In plain terms:</em> readable but visibly imperfect; usable in a pinch, worth " +
        "re-scanning when convenient.</li>" +
        "<li><strong>Poor &mdash; score below 50.</strong> <em>Technically:</em> at least one metric fails badly (heavy blur, " +
        "strong skew, faint/low contrast, illegible OCR) or several are weak at once. <em>In plain terms:</em> hard to read; " +
        "re-scan or replace the source before relying on it.</li>" +
        "</ul>" +
        "<p>A document with no scoreable pages (e.g. all blank) is marked <strong>unknown</strong> rather than judged, and " +
        "handwriting is recorded as a separate flag &mdash; a neat hand-copied part can still be &ldquo;good&rdquo;.</p>",
    },
    {
      n: 6, slug: "phase-06-pieces", title: "Piece Reports",
      blurb: "Score completeness and severity for every piece.",
      detail:
        "<p><strong>What it does.</strong> Joins every upstream signal for a piece &mdash; observed parts, expected/missing " +
        "parts, completeness, scan quality, notation source, confidence tiers, and review flags &mdash; into one report per " +
        "piece, and derives a prioritised list of recommended manual actions.</p>" +
        "<p><strong>Why it works this way.</strong> This stage computes nothing new about the music; it only surfaces the " +
        "deterministic facts Phases 1&ndash;5 already produced. That keeps it fully offline (no AI, no network, no rendering), " +
        "so it is cheap, fast, and easy to unit-test, and its output can never disagree with the phases it summarises.</p>" +
        "<p><strong>The problem it solves.</strong> Everything the pipeline knows about a single piece is scattered across " +
        "several datasets. This gives a librarian one page per piece that answers &ldquo;is it complete, how good are the " +
        "scans, and what should I do next?&rdquo;</p>",
    },
    {
      n: 7, slug: "phase-07-collection", title: "Collection Report",
      blurb: "Roll up collection-wide statistics.",
      detail:
        "<p><strong>What it does.</strong> Aggregates every per-piece report into one collection-wide view: library size, " +
        "completeness, scan quality, lookup coverage, the instruments that are most often missing, and the pieces that need " +
        "attention.</p>" +
        "<p><strong>Why it works this way.</strong> Like the piece reports, it is a pure reporting stage &mdash; it only " +
        "counts, groups, and orders facts the earlier phases produced, so it stays deterministic and offline. No number here " +
        "is re-derived; it is simply a roll-up of Phase 6.</p>" +
        "<p><strong>The problem it solves.</strong> It turns hundreds of individual piece reports into the handful of " +
        "headline numbers and rankings needed to understand the health of the whole library at a glance and to decide where " +
        "to focus.</p>",
    },
    {
      n: 8, slug: "phase-08-review", title: "Manual Review Pack",
      blurb: "Prioritize the pieces that most need human review.",
      detail:
        "<p><strong>What it does.</strong> Turns the per-piece reports into a <em>prioritised</em> work queue, so limited " +
        "librarian time targets the highest-value fixes first. It carries each piece's reason codes and recommended actions " +
        "through verbatim.</p>" +
        "<p><strong>Why it works this way.</strong> Unlike the pure reporting phases, this stage does compute one new thing: " +
        "a transparent, auditable priority ordering. It still re-derives nothing &mdash; it only weights and orders the " +
        "existing facts &mdash; so the ranking is reproducible and every position can be explained.</p>" +
        "<p><strong>The problem it solves.</strong> A flat list of problems is not actionable when there are more issues than " +
        "hours. This phase answers &ldquo;what should I fix first?&rdquo; by putting the most consequential, most fixable " +
        "pieces at the top of the queue.</p>",
    },
  ];
  MLG.phaseBySlug = function (slug) {
    return MLG.PHASES.filter(function (p) { return p.slug === slug; })[0] || null;
  };
  MLG.phaseByNumber = function (n) {
    return MLG.PHASES.filter(function (p) { return p.n === n; })[0] || null;
  };

  // --- settings (localStorage) ---------------------------------------------
  var LS_THEME = "mlg.theme";
  var LS_LIBROOT = "mlg.libraryRoot";

  MLG.settings = {
    getTheme: function () {
      try { return localStorage.getItem(LS_THEME); } catch (e) { return null; }
    },
    setTheme: function (t) {
      try { localStorage.setItem(LS_THEME, t); } catch (e) { /* ignore */ }
      document.documentElement.setAttribute("data-theme", t);
    },
    getLibraryRoot: function () {
      var stored;
      try { stored = localStorage.getItem(LS_LIBROOT); } catch (e) { stored = null; }
      if (stored) return stored;
      var m = MLG.get("manifest", {});
      return (m && m.library_root) || "";
    },
    setLibraryRoot: function (v) {
      try { localStorage.setItem(LS_LIBROOT, v || ""); } catch (e) { /* ignore */ }
    },
  };

  // Apply the stored theme as early as possible (this script is in <head> defer).
  (function applyTheme() {
    var t = MLG.settings.getTheme();
    if (t === "light" || t === "dark") {
      document.documentElement.setAttribute("data-theme", t);
    }
  })();

  // --- small DOM helpers ---------------------------------------------------
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === "class") node.className = attrs[k];
        else if (k === "html") node.innerHTML = attrs[k];
        else if (k === "text") node.textContent = attrs[k];
        else if (k in node && k !== "list") node[k] = attrs[k];
        else node.setAttribute(k, attrs[k]);
      });
    }
    (children || []).forEach(function (c) {
      if (c == null) return;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }
  MLG.el = el;
  MLG.$ = function (sel, root) { return (root || document).querySelector(sel); };
  MLG.$$ = function (sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  };

  // --- formatters ----------------------------------------------------------
  var fmt = (MLG.fmt = {});
  fmt.num = function (n) {
    if (n == null || isNaN(n)) return "\u2014";
    return Number(n).toLocaleString();
  };
  fmt.pct = function (part, whole, digits) {
    if (!whole) return "\u2014";
    var v = (100 * part) / whole;
    return v.toFixed(digits == null ? 0 : digits) + "%";
  };
  fmt.pctValue = function (v, digits) {
    if (v == null || isNaN(v)) return "\u2014";
    return Number(v).toFixed(digits == null ? 0 : digits) + "%";
  };
  fmt.date = function (iso) {
    if (!iso) return "\u2014";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleString();
  };
  fmt.text = function (v) {
    return v == null || v === "" ? "\u2014" : String(v);
  };
  fmt.title = function (s) {
    return String(s || "").replace(/[_-]+/g, " ").replace(/\b\w/g, function (c) {
      return c.toUpperCase();
    });
  };

  // --- badges (color is always paired with a text label) -------------------
  var SEVERITY_CLASS = { high: "high", review: "review", ok: "ok" };
  var QUALITY_CLASS = { good: "good", fair: "fair", poor: "poor", unknown: "unknown" };

  MLG.badge = function (text, variant) {
    return el("span", { class: "badge badge--" + (variant || "muted"), text: fmt.text(text) });
  };
  MLG.severityBadge = function (sev) {
    var key = String(sev || "").toLowerCase();
    return MLG.badge(fmt.title(sev), SEVERITY_CLASS[key] || "muted");
  };
  MLG.qualityBadge = function (q) {
    var key = String(q || "").toLowerCase();
    return MLG.badge(fmt.title(q), QUALITY_CLASS[key] || "unknown");
  };

  // --- original-PDF links (composed client-side from the base dir + relative path) ---
  MLG.pdfUrl = function (relPath) {
    if (!relPath) return null;
    var base = MLG.settings.getLibraryRoot();
    if (!base) return null;
    var joined = String(base).replace(/[\\/]+$/, "") + "/" + String(relPath).replace(/\\/g, "/");
    var normalized = joined.replace(/\\/g, "/");
    if (!/^file:/i.test(normalized)) {
      // Windows drive path -> file:///C:/...  ;  POSIX -> file:///...
      normalized = "file:///" + normalized.replace(/^\/+/, "");
    }
    return encodeURI(normalized);
  };

  // Render a "open original PDF" control: link (if base set) + always-copyable path.
  MLG.pdfControl = function (relPath) {
    var wrap = el("div", { class: "stack" });
    if (!relPath) {
      wrap.appendChild(el("p", { class: "muted", text: "No source PDF recorded." }));
      return wrap;
    }
    var url = MLG.pdfUrl(relPath);
    if (url) {
      wrap.appendChild(
        el("a", { class: "btn btn--accent", href: url, target: "_blank", rel: "noopener", text: "Open original PDF" })
      );
      wrap.appendChild(el("p", { class: "pdf-path", text: decodeURI(url.replace(/^file:\/\/\//, "")) }));
    } else {
      wrap.appendChild(
        el("p", { class: "muted", text: "Set the library location (\u2699) to enable the PDF link." })
      );
      wrap.appendChild(el("p", { class: "pdf-path", text: String(relPath).replace(/\\/g, "/") }));
    }
    return wrap;
  };

  // --- query string helpers ------------------------------------------------
  MLG.param = function (name) {
    var m = new RegExp("[?&]" + name + "=([^&]*)").exec(window.location.search);
    return m ? decodeURIComponent(m[1].replace(/\+/g, " ")) : null;
  };
  MLG.href = function (page, params) {
    var qs = Object.keys(params || {})
      .filter(function (k) { return params[k] != null; })
      .map(function (k) { return k + "=" + encodeURIComponent(params[k]); })
      .join("&");
    return page + (qs ? "?" + qs : "");
  };

  // --- breadcrumbs ---------------------------------------------------------
  MLG.breadcrumbs = function (trail) {
    var host = document.getElementById("breadcrumbs");
    if (!host) return;
    host.innerHTML = "";
    trail.forEach(function (item, i) {
      if (i > 0) host.appendChild(el("span", { class: "sep", text: "/" }));
      if (item.href) host.appendChild(el("a", { href: item.href, text: item.label }));
      else host.appendChild(el("span", { text: item.label }));
    });
  };

  // --- header / footer chrome (injected so all pages stay DRY) -------------
  function brandMark() {
    return (
      '<svg class="brand__mark" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
      '<path d="M9 18V5l10-2v13" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>' +
      '<circle cx="6" cy="18" r="3" stroke="currentColor" stroke-width="2"/>' +
      '<circle cx="16" cy="16" r="3" stroke="currentColor" stroke-width="2"/></svg>'
    );
  }

  function buildHeader(activePage) {
    var header = el("div", { class: "site-header__inner" });

    header.appendChild(
      el("a", { class: "brand", href: MLG.rel("index.html"), html: brandMark() + "<span>Music Library</span>" })
    );

    var nav = el("nav", { class: "nav", "aria-label": "Primary" });
    function link(page, label, key) {
      var a = el("a", { href: MLG.rel(page), html: '<span class="nav-label">' + label + "</span>" });
      if (activePage === key) a.setAttribute("aria-current", "page");
      return a;
    }
    nav.appendChild(link("index.html", "Dashboard", "dashboard"));

    // Pipeline dropdown
    var menu = el("div", { class: "menu" });
    var toggle = el("button", { class: "btn", type: "button", "aria-haspopup": "true", "aria-expanded": "false", text: "Pipeline \u25be" });
    var panel = el("div", { class: "menu__panel" });
    MLG.PHASES.forEach(function (p) {
      panel.appendChild(
        el("a", { href: MLG.rel("pages/" + p.slug + ".html"), text: p.n + ". " + p.title })
      );
    });
    toggle.addEventListener("click", function (e) {
      e.stopPropagation();
      var open = panel.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    document.addEventListener("click", function () {
      panel.classList.remove("open");
      toggle.setAttribute("aria-expanded", "false");
    });
    menu.appendChild(toggle);
    menu.appendChild(panel);
    nav.appendChild(menu);

    nav.appendChild(link("pages/pieces.html", "Pieces", "pieces"));
    nav.appendChild(link("pages/documents.html", "Documents", "documents"));
    nav.appendChild(link("pages/guide.html", "Guide", "guide"));
    nav.appendChild(link("pages/about.html", "About", "about"));
    header.appendChild(nav);

    // Tools: search + theme + settings
    var tools = el("div", { class: "header-tools" });
    tools.appendChild(buildSearch());
    tools.appendChild(buildThemeToggle());
    tools.appendChild(buildSettingsButton());
    header.appendChild(tools);

    return header;
  }

  function buildThemeToggle() {
    var btn = el("button", {
      class: "btn btn--icon",
      type: "button",
      title: "Toggle light / dark",
      "aria-label": "Toggle light or dark theme",
      text: "\u25D0",
    });
    btn.addEventListener("click", function () {
      var current = document.documentElement.getAttribute("data-theme");
      if (!current) {
        current = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
          ? "dark" : "light";
      }
      MLG.settings.setTheme(current === "dark" ? "light" : "dark");
    });
    return btn;
  }

  function buildSettingsButton() {
    var btn = el("button", {
      class: "btn btn--icon",
      type: "button",
      title: "Settings",
      "aria-label": "Settings",
      text: "\u2699",
    });
    btn.addEventListener("click", openSettings);
    return btn;
  }

  function openSettings() {
    var dlg = document.getElementById("mlg-settings");
    if (!dlg) {
      dlg = el("dialog", { id: "mlg-settings", class: "settings" });
      dlg.innerHTML =
        "<h2>Settings</h2>" +
        '<div class="settings__field">' +
        '<label for="mlg-libroot">Library location (base directory)</label>' +
        '<input id="mlg-libroot" class="input" type="text" placeholder="e.g. C:\\Temp\\NSCB" />' +
        '<p class="settings__hint">Absolute path to the folder that holds the original PDFs. ' +
        "Links to source files are built from this + each document\u2019s relative path, so they keep " +
        "working if this site folder moves.</p></div>" +
        '<div class="row" style="justify-content:flex-end">' +
        '<button class="btn" id="mlg-settings-cancel" type="button">Close</button>' +
        '<button class="btn btn--accent" id="mlg-settings-save" type="button">Save</button></div>';
      document.body.appendChild(dlg);
      dlg.querySelector("#mlg-settings-cancel").addEventListener("click", function () { dlg.close(); });
      dlg.querySelector("#mlg-settings-save").addEventListener("click", function () {
        MLG.settings.setLibraryRoot(dlg.querySelector("#mlg-libroot").value.trim());
        dlg.close();
        window.location.reload();
      });
    }
    dlg.querySelector("#mlg-libroot").value = MLG.settings.getLibraryRoot();
    if (typeof dlg.showModal === "function") dlg.showModal();
    else dlg.setAttribute("open", "");
  }

  // --- global search -------------------------------------------------------
  function buildSearch() {
    var wrap = el("div", { class: "search" });
    var input = el("input", {
      class: "input search__input",
      type: "search",
      placeholder: "Search\u2026",
      "aria-label": "Search pieces, documents and pages",
      autocomplete: "off",
    });
    var results = el("div", { class: "search__results", role: "listbox" });
    wrap.appendChild(input);
    wrap.appendChild(results);

    function close() { results.classList.remove("open"); }
    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      results.innerHTML = "";
      if (q.length < 2) { close(); return; }
      var hits = MLG.search(q, 8);
      if (!hits.length) {
        results.appendChild(el("div", { class: "search__result", text: "No matches." }));
      } else {
        var lastGroup = null;
        hits.forEach(function (h) {
          if (h.group !== lastGroup) {
            results.appendChild(el("div", { class: "search__group", text: h.group }));
            lastGroup = h.group;
          }
          results.appendChild(
            el("a", { class: "search__result", href: MLG.rel(h.href), html: h.label + " <small>" + h.sub + "</small>" })
          );
        });
      }
      results.classList.add("open");
    });
    input.addEventListener("blur", function () { setTimeout(close, 150); });
    return wrap;
  }

  MLG.search = function (q, limit) {
    var out = [];
    var pieces = MLG.get("pieces", []);
    var docs = MLG.get("documents", []);
    var pages = MLG.get("pages", []);

    pieces.forEach(function (p) {
      var hay = ((p.title || "") + " " + (p.catalog_number || "") + " " + (p.piece_id || "")).toLowerCase();
      if (hay.indexOf(q) >= 0) {
        out.push({
          group: "Pieces",
          label: MLG.fmt.text(p.title || p.piece_id),
          sub: p.catalog_number ? "#" + p.catalog_number : "",
          href: MLG.href("pages/piece.html", { id: p.piece_id }),
        });
      }
    });
    docs.forEach(function (d) {
      var hay = ((d.pdf_filename || "") + " " + (d.instrument || "") + " " + (d.doc_id || "")).toLowerCase();
      if (hay.indexOf(q) >= 0) {
        out.push({
          group: "Documents",
          label: MLG.fmt.text(d.pdf_filename || d.doc_id),
          sub: MLG.fmt.text(d.instrument || ""),
          href: MLG.href("pages/document.html", { id: d.doc_id }),
        });
      }
    });
    pages.forEach(function (pg) {
      var hay = ((pg.page_id || "") + " " + (pg.first_text || "")).toLowerCase();
      if (hay.indexOf(q) >= 0) {
        out.push({
          group: "Pages",
          label: "Page " + MLG.fmt.text(pg.page_num),
          sub: MLG.fmt.text(pg.doc_filename || pg.doc_id),
          href: MLG.href("pages/page.html", { id: pg.page_id }),
        });
      }
    });
    return out.slice(0, limit || 12);
  };

  // --- relative-path resolver (pages/ live one level below the site root) ---
  MLG.rel = function (pathFromRoot) {
    var depth = document.body.getAttribute("data-depth") || "0";
    var prefix = depth === "1" ? "../" : "";
    return prefix + pathFromRoot;
  };

  // --- boot ----------------------------------------------------------------
  MLG.bootChrome = function () {
    var activePage = document.body.getAttribute("data-page");
    var headerHost = document.getElementById("site-header");
    if (headerHost) {
      headerHost.className = "site-header";
      headerHost.appendChild(buildHeader(activePage));
    }
    var footerHost = document.getElementById("site-footer");
    if (footerHost) {
      var m = MLG.get("manifest", {});
      footerHost.className = "site-footer";
      footerHost.innerHTML =
        "<span>Generated by Script 09 \u00b7 site schema <code>" +
        fmt.text(m.site_schema_version) + "</code></span>" +
        "<span>run <code>" + fmt.text(m.run_id) + "</code></span>" +
        "<span>" + fmt.date(m.generated_at) + "</span>";
    }
  };

  MLG.warnIfNoData = function (host) {
    if (MLG.get("manifest") == null) {
      host.appendChild(
        el("div", { class: "callout", html:
          "No generated data found. Run <code>09_static_site.py</code> to populate " +
          "<code>web/data/</code>, or open with the bundled sample fixture." })
      );
      return true;
    }
    return false;
  };

  document.addEventListener("DOMContentLoaded", function () {
    MLG.bootChrome();
    if (typeof MLG.renderPage === "function") {
      try { MLG.renderPage(); } catch (e) {
        var app = document.getElementById("app");
        if (app) app.appendChild(el("div", { class: "callout", text: "Render error: " + e.message }));
        if (window.console) console.error(e);
      }
    }
  });
})();
