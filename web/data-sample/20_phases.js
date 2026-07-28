/*
 * Per-phase data, keyed by phase number (as a string). Every phase entry follows the
 * same shape so js/phase.js can render any phase generically:
 *   summary: { records, unit, note, timestamp }
 *   stats:   [{ label, value, display? }]       -> KPI cards
 *   charts:  [{ title, rows:[{label,value,display?,variant?}] }]
 *   table:   { caption, entity, columns:[{key,label,num?,link?,badge?,display?}], source, filter? }
 * Timing is intentionally not shown yet (deferred): note it as "not captured yet".
 * Phases mirror the real pipeline scripts 01-08.
 */
MLG.register("phases", {
  "1": {
    summary: { records: 3, unit: "documents", note: "3 PDFs scanned and grouped into 2 pieces.", timestamp: "2025-01-01T12:00:00Z" },
    stats: [
      { label: "PDFs found", value: 3 },
      { label: "Pieces", value: 2 },
    ],
    table: {
      caption: "Inventory", entity: "document",
      columns: [
        { key: "pdf_filename", label: "Document", link: "document" },
        { key: "piece_title", label: "Piece" },
        { key: "page_count", label: "Pages", num: true },
      ],
      source: "documents",
    },
  },
  "2": {
    summary: { records: 5, unit: "pages", note: "Pages rendered, scans OCR'd, and each document vision-analysed.", timestamp: "2025-01-01T12:00:00Z" },
    stats: [
      { label: "Pages rendered", value: 5 },
      { label: "Render DPI", value: 150, display: "150" },
      { label: "With staves", value: 5 },
    ],
    charts: [
      { title: "Text density (higher = more ink)", rows: [
        { label: "d1 p1", value: 42, display: "0.42" },
        { label: "d1 p2", value: 38, display: "0.38" },
        { label: "d2 p1", value: 55, display: "0.55" },
        { label: "d3 p1", value: 61, display: "0.61" },
        { label: "d3 p2", value: 58, display: "0.58" },
      ] },
    ],
    table: {
      caption: "Analysed pages", entity: "page",
      columns: [
        { key: "page_num", label: "Page", num: true, link: "page" },
        { key: "doc_filename", label: "Document" },
        { key: "orientation", label: "Orientation" },
        { key: "staff_line_count", label: "Staves", num: true },
      ],
      source: "pages",
    },
  },
  "3": {
    summary: { records: 3, unit: "documents", note: "Each document classified to a predicted instrument / part.", timestamp: "2025-01-01T12:00:00Z" },
    stats: [
      { label: "Classified", value: 3 },
      { label: "Needs review", value: 1 },
    ],
    table: {
      caption: "Predicted parts", entity: "document",
      columns: [
        { key: "pdf_filename", label: "Document", link: "document" },
        { key: "instrument", label: "Instrument" },
        { key: "section", label: "Section" },
      ],
      source: "documents",
    },
  },
  "4": {
    summary: { records: 2, unit: "pieces", note: "Expected instrumentation inferred for each piece.", timestamp: "2025-01-01T12:00:00Z" },
    stats: [
      { label: "Pieces", value: 2 },
      { label: "Expected parts", value: 32 },
      { label: "Missing required", value: 2 },
    ],
    charts: [
      { title: "Missing required parts by piece", rows: [
        { label: "Mexican Hat Dance", value: 2, variant: "warning" },
        { label: "Amazing Grace", value: 0, variant: "success" },
      ] },
    ],
  },
  "5": {
    summary: { records: 3, unit: "documents", note: "Scan quality scored; legibility issues flagged.", timestamp: "2025-01-01T12:00:00Z" },
    stats: [
      { label: "Good", value: 2 },
      { label: "Fair", value: 1 },
      { label: "Poor", value: 0 },
    ],
    charts: [
      { title: "Quality bands", rows: [
        { label: "Good", value: 2, variant: "success" },
        { label: "Fair", value: 1, variant: "warning" },
        { label: "Poor", value: 0, variant: "error" },
      ] },
    ],
  },
  "6": {
    summary: { records: 2, unit: "pieces", note: "Parts matched to expected sets; completeness scored.", timestamp: "2025-01-01T12:00:00Z" },
    stats: [
      { label: "Pieces", value: 2 },
      { label: "Complete", value: 1 },
      { label: "Missing parts", value: 2 },
    ],
    charts: [
      { title: "Completeness by piece", rows: [
        { label: "Mexican Hat Dance", value: 62, display: "62%", variant: "warning" },
        { label: "Amazing Grace", value: 95, display: "95%", variant: "success" },
      ] },
    ],
    table: {
      caption: "Piece completeness", entity: "piece",
      columns: [
        { key: "title", label: "Piece", link: "piece" },
        { key: "completeness_score", label: "Score", num: true, display: "pct" },
        { key: "completeness_tier", label: "Tier" },
        { key: "missing_required_count", label: "Missing", num: true },
      ],
      source: "pieces",
    },
  },
  "7": {
    summary: { records: 1, unit: "report", note: "Collection-wide report rolled up from all pieces.", timestamp: "2025-01-01T12:00:00Z" },
    stats: [
      { label: "Overall completeness", value: 78, display: "78%" },
      { label: "Pieces at risk", value: 1 },
    ],
    charts: [
      { title: "Top missing sections", rows: [
        { label: "Low Brass", value: 1 },
        { label: "Percussion", value: 1 },
      ] },
    ],
  },
  "8": {
    summary: { records: 1, unit: "pieces flagged", note: "Prioritized manual-review queue.", timestamp: "2025-01-01T12:00:00Z" },
    stats: [
      { label: "In queue", value: 1 },
      { label: "Top priority", value: 131 },
    ],
    table: {
      caption: "Review queue", entity: "piece",
      columns: [
        { key: "title", label: "Piece", link: "piece" },
        { key: "severity", label: "Severity", badge: "severity" },
        { key: "completeness_score", label: "Completeness", num: true, display: "pct" },
      ],
      source: "pieces",
      filter: { field: "severity", in: ["high", "review"] },
    },
  },
});
