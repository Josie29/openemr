# Document ingestion fixtures

Synthetic clinical documents for the Week-2 ingestion/extraction pipeline, plus the sources
that build them and the extractor outputs they produce. Every document is **fictional demo data**
for the showcase patient **Sergio Angulo** (MRN `OE-000023`) and carries a visible
"synthetic demo data" banner — no real PHI.

## Layout

| Dir | Role | Consumed by |
|-----|------|-------------|
| `pdfs/` | The ingestible PDFs — the **only** things the ingestion pipeline reads. | ingestion flow / integration tests |
| `src/` | How the PDFs are generated: HTML templates + the `make-scanned-variant.py` generator. | nobody at runtime (build-time only) |
| `extractions/` | Golden / stub extractor outputs (`*.ocr.json`) for the corresponding PDF. | integration tests (stubbed extractor), extraction goldens |

Basenames are aligned across dirs: `pdfs/<name>.pdf` ↔ `src/<name>.html` ↔
`extractions/<name>.ocr.json`. The `-scanned` / `-scanned-heavy` PDFs have **no own HTML** — the
generator derives them from the clean report's HTML (see below).

## The fixtures

Clinical narrative (shared across the lab, intake, and medication list so cross-document reasoning
has something to resolve): aspirin allergy alongside NSAID exposure, declining renal function,
eosinophilia / allergic asthma, and a family history of CKD + hypertension + type-2 diabetes.

| PDF | What it is |
|-----|------------|
| `sergio-angulo-lab-report.pdf` | Clean digital CMP + CBC report. Extracts near-perfectly (happy path). Prints a **LOINC** column (JOS-87): every code is real, sourced from loinc.org's panel pages and checksum-verified — never write one from memory, and never invent one for a new analyte. The write-back needs a code (JOS-81) and the extractor refuses any that the page does not print. |
| `sergio-angulo-lab-report-scanned.pdf` | Lightly degraded scan — legible but imperfect. Tests graceful degradation. |
| `sergio-angulo-lab-report-scanned-heavy.pdf` | Heavily degraded scan with localized damage (coffee ring over the renal values, dropout streak + dark edge over the Prior column, fold crease). **Failure-path fixture** — specific fields go low-confidence / missing so the confidence gate / `safe_refusal` cases have something to catch. |
| `sergio-angulo-intake-form.pdf` | Patient-completed intake (handwriting-styled, tabular). Allergies + family history + demographics — **no medications** (those live on the medication list; JOS-91). |
| `sergio-angulo-intake-form-v2.pdf` | Intake with a strictly linear single-column `Label: value` layout (no tables) — one discrete text region per field for OCR. |
| `sergio-angulo-medication-list.pdf` | A Wells Branch Pharmacy medication profile — the **third document type** (`medication_list`, JOS-91). Six medications on single-line drug-name rows (locatable): Budesonide, Albuterol, Fexofenadine, Epinephrine, Ibuprofen, Naproxen — the asthma controllers/rescue + the NSAID exposure the narrative turns on. Owns medications; the intake form no longer does. |

## Regenerating

**Clean PDFs** — `src/make-clean-pdf.py` renders the HTML through headless Chrome. It wraps the
invocation that produced the committed PDFs (verified: re-rendering an unchanged source reproduces
its PDF byte-for-byte), so use it rather than retyping the flags — the page's own
`@page { margin: 0 }` owns the geometry, and adding Chrome's default margin back shifts every box
on the page, silently invalidating the recorded boxes.

```sh
python src/make-clean-pdf.py src/sergio-angulo-lab-report.html pdfs/sergio-angulo-lab-report.pdf
```

**Scanned variants** — `src/make-scanned-variant.py` renders the clean HTML, bakes in scan
artifacts, and re-wraps as a PDF. Two intensity profiles: `light` (legible, happy path) and
`heavy` (localized damage, failure path). Requires `Pillow`, `numpy`, and Chrome:

```sh
python src/make-scanned-variant.py --profile light \
  src/sergio-angulo-lab-report.html pdfs/sergio-angulo-lab-report-scanned.pdf
python src/make-scanned-variant.py --profile heavy \
  src/sergio-angulo-lab-report.html pdfs/sergio-angulo-lab-report-scanned-heavy.pdf
```

The `heavy` profile's knobs (blur, `black_lift`, stain/streak/crease placement) live in the
`PROFILES` dict in that script.

**Extractions** (`extractions/*.ocr.json`) are produced by running the ingestion extractor over a
PDF; regenerate them when the extractor or a source document changes so the goldens stay in sync.
