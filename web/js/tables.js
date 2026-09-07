/*
 * tables.js - sortable, filterable data tables shared by index and detail pages.
 * columns: [{ key, label, num?, render?(row)->Node|string, sortValue?(row)->any }]
 */
(function () {
  "use strict";
  var MLG = (window.MLG = window.MLG || {});
  var el = MLG.el;

  MLG.table = function (rows, columns, opts) {
    opts = opts || {};
    var state = { sortKey: opts.sortKey || null, dir: opts.dir || "asc", filter: (opts.initialFilter || "").trim().toLowerCase() };
    var container = el("div", { class: "stack" });

    var filterInput = null;
    if (opts.filter !== false) {
      filterInput = el("input", {
        class: "input",
        type: "search",
        placeholder: opts.filterPlaceholder || "Filter this table\u2026",
        "aria-label": "Filter table rows",
        style: "max-width:320px",
        value: opts.initialFilter || "",
      });
      filterInput.addEventListener("input", function () {
        state.filter = filterInput.value.trim().toLowerCase();
        render();
      });
      container.appendChild(filterInput);
    }

    var wrap = el("div", { class: "table-wrap" });
    var table = el("table", { class: "data" });
    var thead = el("thead");
    var tbody = el("tbody");
    table.appendChild(thead);
    table.appendChild(tbody);
    wrap.appendChild(table);
    container.appendChild(wrap);

    var countLine = el("p", { class: "muted" });
    container.appendChild(countLine);

    function sortVal(row, col) {
      if (col.sortValue) return col.sortValue(row);
      return row[col.key];
    }

    function buildHead() {
      thead.innerHTML = "";
      var tr = el("tr");
      columns.forEach(function (col) {
        var th = el("th", { text: col.label, "data-sort": "" });
        if (col.num) th.className = "num";
        if (state.sortKey === col.key) {
          th.setAttribute("aria-sort", state.dir === "asc" ? "ascending" : "descending");
        }
        th.addEventListener("click", function () {
          if (state.sortKey === col.key) state.dir = state.dir === "asc" ? "desc" : "asc";
          else { state.sortKey = col.key; state.dir = "asc"; }
          render();
        });
        tr.appendChild(th);
      });
      thead.appendChild(tr);
    }

    function filtered() {
      if (!state.filter) return rows.slice();
      return rows.filter(function (row) {
        return columns.some(function (col) {
          var v = col.filterText ? col.filterText(row) : row[col.key];
          return String(v == null ? "" : v).toLowerCase().indexOf(state.filter) >= 0;
        });
      });
    }

    function sorted(list) {
      if (!state.sortKey) return list;
      var col = columns.filter(function (c) { return c.key === state.sortKey; })[0];
      var sign = state.dir === "asc" ? 1 : -1;
      return list.slice().sort(function (a, b) {
        var av = sortVal(a, col), bv = sortVal(b, col);
        if (av == null) return 1;
        if (bv == null) return -1;
        if (typeof av === "number" && typeof bv === "number") return (av - bv) * sign;
        return String(av).localeCompare(String(bv)) * sign;
      });
    }

    function render() {
      buildHead();
      tbody.innerHTML = "";
      var list = sorted(filtered());
      list.forEach(function (row) {
        var tr = el("tr");
        columns.forEach(function (col) {
          var td = el("td");
          if (col.num) td.className = "num";
          var content = col.render ? col.render(row) : row[col.key];
          if (content == null) td.textContent = "\u2014";
          else if (typeof content === "string" || typeof content === "number") td.textContent = content;
          else td.appendChild(content);
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
      countLine.textContent =
        list.length + " of " + rows.length + (rows.length === 1 ? " row" : " rows");
    }

    render();
    return container;
  };
})();
