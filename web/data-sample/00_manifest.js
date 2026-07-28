/*
 * SAMPLE FIXTURE - hand-authored so the scaffold renders before Script 09 exists.
 * Script 09 will overwrite every web/data/*.js file with real generated data using
 * these exact shapes. Keep this small; it is a contract reference, not real data.
 *
 * Load order (see the <script> tags in each HTML page): manifest, dashboard, phases,
 * pieces, documents, pages. Registration order does not matter - renderers run on
 * DOMContentLoaded after all data files have registered.
 */
MLG.register("manifest", {
  site_schema_version: "1.0",
  run_id: "sample-fixture",
  generated_at: "2025-01-01T12:00:00Z",
  library_root: "C:\\Temp\\NSCB",
  thumbnails: false,
  counts: {
    pieces: 2,
    documents: 3,
    pages: 5,
    render_images: 5,
  },
});
