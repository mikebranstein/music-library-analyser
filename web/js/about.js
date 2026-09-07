/* about.js - "About this report" page: what this is, how it was produced, and how much to trust it.
 * Mirrors the plain-language explanation Script 10 writes to definitions.md, generalized for the
 * web site (not Excel-specific) and stamped with this run's real counts. */
(function () {
  "use strict";
  var MLG = (window.MLG = window.MLG || {});
  var el = MLG.el;
  var fmt = MLG.fmt;

  MLG.renderPage = function () {
    var app = document.getElementById("app");
    MLG.breadcrumbs([{ label: "Dashboard", href: MLG.rel("index.html") }, { label: "About this report" }]);
    if (MLG.warnIfNoData(app)) return;

    var m = MLG.get("manifest", {});
    var d = MLG.get("dashboard", {});
    var pieces = MLG.get("pieces", []);
    var documents = MLG.get("documents", []);

    app.appendChild(MLG.pageHead("Reference", "About this report"));
    app.appendChild(el("p", { class: "muted", text:
      "What this collection review is, how it was produced, and how much to trust it \u2014 in plain language." }));

    var pieceCount = pieces.length || 0;
    var docCount = documents.length || 0;
    var statLine = el("div", { class: "card stack" }, [
      el("p", { html:
        "This copy of the report covers <strong>" + fmt.num(pieceCount) +
        " pieces of music</strong> and <strong>" + fmt.num(docCount) + " individual sheet-music files</strong>, " +
        "generated on <strong>" + fmt.date(m.generated_at) + "</strong>. These numbers will change the next time the " +
        "report is refreshed, as more of the library is reviewed or corrections are made." }),
    ]);
    app.appendChild(statLine);

    app.appendChild(el("h2", { text: "What this is" }));
    app.appendChild(el("div", { class: "prose" }, [
      el("p", { html:
        "This site is an automated review of a large sheet-music library \u2014 hundreds of pieces, each made up of " +
        "individual instrument-part PDF files (Flute 1, Trombone 2, a conductor's score, and so on). For every piece it " +
        "tries to answer three questions: <em>what instrument parts does this piece actually have on file, what parts " +
        "<strong>should</strong> it have according to its real published edition, and how good are the scans</em>? The " +
        "gap between the first two answers is what shows up as &ldquo;missing parts&rdquo; throughout the site." }),
      el("p", { html:
        "It exists because checking a library this size by hand \u2014 opening every PDF, reading every part label, " +
        "looking up every piece's published instrumentation \u2014 is not realistic for a person to do page by page. " +
        "Instead, a computer pipeline worked through the whole collection automatically, and everything in this site is " +
        "the output of that process." }),
    ]));

    app.appendChild(el("h2", { text: "How it was put together" }));
    app.appendChild(el("div", { class: "prose" }, [
      el("p", { html:
        "For each file, the process looked at the file name and the contents of the pages to work out which instrument " +
        "part it was. Where a file was a scanned image rather than typed text, it used optical character recognition " +
        "(<strong>OCR</strong>) \u2014 technology that reads text out of a picture of a page \u2014 to figure out what was " +
        "written on it. OCR can misread smudged, faint, handwritten, or poor-quality scans, so its results are not always " +
        "accurate." }),
      el("p", { html:
        "For each piece, the process also tried to work out what instrument parts the piece is supposed to have " +
        "altogether. To do this it read the piece's own conductor's score when one was on file, looked the piece up on a " +
        "community reference site for band/wind-ensemble music, and, failing that, searched more broadly online and used " +
        "an artificial intelligence (AI) system to read and interpret what it found \u2014 catalog listings, publisher " +
        "pages, or images of the score itself \u2014 in order to identify the piece and decide what its complete, correct " +
        "set of instrument parts should be. The same AI system also helped judge the scanned pages themselves, including " +
        "checking scan quality and deciding whether a page was printed or handwritten." }),
      el("p", { html:
        "The full step-by-step breakdown of each stage \u2014 what it does and why it works that way \u2014 is written up " +
        "on each stage's own page; start from the <a href=\"" + MLG.rel("index.html") + "\">pipeline strip on the " +
        "dashboard</a> or use the <strong>Pipeline</strong> menu in the header." }),
    ]));

    app.appendChild(el("h2", { text: "How difficult this actually was" }));
    app.appendChild(el("div", { class: "prose" }, [
      el("p", { html:
        "This is not a simple lookup problem. Two things make it genuinely hard, and the pipeline was built around both:" }),
      el("ul", {}, [
        el("li", { html:
          "<strong>What a piece should contain is not written down anywhere.</strong> There is no master list of " +
          "correct instrumentation for a given piece and edition \u2014 it has to be discovered, per piece, from whatever " +
          "evidence exists: a score sitting in the folder, a reference website, or an online search. Different " +
          "publishers issue different editions of the same piece with different part counts, so even a confident answer " +
          "can be confidently wrong if it matched the wrong edition." }),
        el("li", { html:
          "<strong>The source material itself is inconsistent.</strong> Decades of scans, photocopies, handwritten " +
          "parts, inconsistent filenames, and misfiled pages mean no single method (filename, OCR, or AI reading) is " +
          "reliable on its own. The pipeline layers several signals with an explicit precedence \u2014 trusting file " +
          "names only when nothing better exists, and letting the page's own printed label override it \u2014 specifically " +
          "because none of them can be trusted in isolation." }),
      ]),
      el("p", { html:
        "AI systems and OCR are generally reliable but not perfect, and can occasionally be confidently wrong \u2014 for " +
        "example, matching a piece to the wrong edition, or misjudging a page's quality. Please treat everything in this " +
        "site as a well-informed starting point rather than a guaranteed, error-free record." }),
    ]));

    app.appendChild(el("h2", { text: "Where it can be wrong" }));
    app.appendChild(el("div", { class: "prose" }, [
      el("ul", {}, [
        el("li", { text: "A piece marked \u201Cmissing\u201D a part may already have that part somewhere in the library, if the file was misnamed, misread, or not matched correctly." }),
        el("li", { text: "A piece marked \u201Ccomplete\u201D may still be missing something the automated process did not catch." }),
        el("li", { text: "Composer names, titles, and other identifying details are the process's best judgment and should be treated as likely, not certain." }),
        el("li", { text: "Scan-quality and handwriting judgments are automated estimates and may not match what a person would decide by looking at the page directly." }),
        el("li", { text: "The library may hold a different arrangement (school, simplified, custom) than the published edition the process matched, which would make the expected part list not match the real files even though nothing was actually \u201Cmissing.\u201D" }),
      ]),
      el("p", { html:
        "If anything on this site looks surprising or does not match what you know about the collection, it is worth " +
        "checking the actual file(s) in question before relying on the report alone." }),
    ]));

    app.appendChild(el("h2", { text: "Where to go next" }));
    app.appendChild(el("div", { class: "grid grid--2" }, [
      el("a", { class: "card card--link", href: MLG.rel("pages/guide.html") }, [
        el("div", { class: "card__label", text: "New here?" }),
        el("div", { style: "font-weight:600;margin-top:var(--space-1)", text: "Read the user guide \u2192" }),
        el("div", { class: "card__hint", text: "How to navigate the site, read the badges, and find what needs attention." }),
      ]),
      el("a", { class: "card card--link", href: MLG.rel("index.html") }, [
        el("div", { class: "card__label", text: "Ready to dig in?" }),
        el("div", { style: "font-weight:600;margin-top:var(--space-1)", text: "Back to the dashboard \u2192" }),
        el("div", { class: "card__hint", text: "Collection health, flagged pieces, and the pipeline overview." }),
      ]),
    ]));
  };
})();
