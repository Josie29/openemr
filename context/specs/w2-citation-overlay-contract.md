# Citation → click-to-source overlay contract (JOS-57 seam)

**Status:** Implemented (frontend + stub) on `feature/w2-jos57-click-to-source`. This is the **shared seam** the document-extraction path (JOS-54 extractor + JOS-56 intake-extractor worker) must emit so the sidebar's click-to-source overlay renders. Documented so the producer and the renderer don't drift.

## The wire shape

A `/chat` answer's `Claim.source` (and each `supporting[]`) is a `SourceRef` (`agent/src/copilot/schemas.py`). For a fact **derived from an uploaded document**, three optional, **system-stamped** fields are populated (they extend the existing `value`/`label`/`date` system-stamped pattern — the model never authors them):

| Field | Meaning |
|---|---|
| `document_id` | The source document's **FHIR `DocumentReference`/`Binary` UUID**. The sidebar fetches bytes via `GET {fhirBaseUrl}/Binary/{document_id}` with the SMART token. |
| `page` | 1-based source page the value was read from. |
| `bounding_box` | `{page, x, y, width, height}` — the rectangle of the cited value on that page. |

`bounding_box` **absent → no rectangle** is rendered (citation still shows quote + page). A box is never fabricated. Ordinary FHIR-record citations leave all three unset and are unchanged.

## ⚠️ Coordinate space — bounding_box must be in PDF POINTS (72-DPI), not native pixels

The overlay renders the page with pdf.js and scales by `canvasWidth / viewport(scale 1).width`, i.e. it treats `bounding_box` coordinates as **PDF user-space points (72 DPI)**. **Verified:** point-space boxes land exactly on the cited values.

- The frozen `ingestion.BoundingBox` docstring says "native page pixels." **The extractor renders at some DPI (Mistral ≈ its own; the Claude-vision spike used 200), so its native-pixel boxes must be converted to points before emitting:** `point = pixel × 72 / render_dpi`. (The stub converts the 200-DPI spike boxes ×72/200.)
- **Decision for JOS-54:** the intake-extractor must emit `bounding_box` in **PDF points**. Alternative (not chosen): normalize to [0,1] of page dims, or carry the render dimensions in the citation and make the overlay DPI-aware. Points is simplest and already wired.

## Provenance path (bbox bypasses FHIR)

The derived FHIR `Observation` carries the value but **not** the box (no native column — see the write-experiment findings). So `bounding_box`/`page` are stamped onto the `SourceRef` from the **extraction sidecar** (`W2_ARCHITECTURE §3.4/§6`; the write-experiment stored it in `procedure_result.comments`), not read from FHIR. JOS-54/56 must surface the sidecar box onto the claim's `SourceRef` when the cited `Observation` derives from a document.

## Renderer side (done — for reference)

- `agent/src/copilot/schemas.py` — `SourceRef` extended (backward-compatible).
- `.../oe-module-ai-copilot/public/assets/js/ai-copilot.js` — `renderCitation` adds a "View source" button when `bounding_box` is present; click → in-panel preview pane → pdf.js render → positioned rectangle.
- `.../public/assets/css/ai-copilot.css` — `.ai-copilot__preview` + `.ai-copilot__bbox`.
- `.../public/assets/vendor/pdfjs/` — vendored pdf.js (CSP forbids CDN).
- `CopilotSidebarController.php` — config island adds `fhirBaseUrl`, `pdfWorkerUrl`.
- `Bootstrap.php` — enqueues pdf.js before the sidebar script. `version.php` `$v_js_includes` bumped (88→89).
- `main.py` — a **removable** stub (`COPILOT_STUB_DOC_ID`) returns a canned document-fact answer for build/verify before the real worker lands.

## To wire the real producer (JOS-54/56)
1. When a claim cites a document-derived `Observation`, stamp `document_id` (the stored `DocumentReference` UUID), `page`, and `bounding_box` (**in points**) from the sidecar onto the `SourceRef`.
2. Delete the `main.py` stub + `COPILOT_STUB_DOC_ID`.
