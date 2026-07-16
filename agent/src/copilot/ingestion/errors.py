class ExtractionError(RuntimeError):
    """Raised when an OCR response cannot be mapped into the strict ingestion schema.

    Carries enough context to log and let the agent degrade (report that the document could not be
    read, never fabricate facts), without leaking transport detail into user-facing output.

    Lives in its own leaf module so the geometry layer can raise it without importing
    ``extractor`` (which imports geometry). ``extractor`` re-exports it, so
    ``from copilot.ingestion.extractor import ExtractionError`` keeps working.
    """
