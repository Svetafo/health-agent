"""Medical document processing: parsing and saving to DB."""

import logging
from datetime import date, datetime

import asyncpg

from src.config import settings
from src.llm.client import analyze_medical_image, parse_medical_text

log = logging.getLogger(__name__)


def _extract_pdf_text(file_bytes: bytes) -> str:
    """Extracts text from PDF bytes via PyMuPDF (fitz)."""
    try:
        import fitz
    except ImportError as e:
        raise ImportError("PyMuPDF is not installed: pip install pymupdf") from e
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    pages = [page.get_text() for page in doc]
    doc.close()
    return "\n".join(pages)


def _render_pdf_as_image(file_bytes: bytes) -> bytes:
    """Renders the first PDF page as PNG (for scanned documents without a text layer)."""
    import fitz
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    page = doc[0]
    mat = fitz.Matrix(2, 2)  # 2x scale for readability
    pix = page.get_pixmap(matrix=mat)
    doc.close()
    return pix.tobytes("png")


def _parse_date(value: str | None) -> date:
    if not value:
        return date.today()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(str(value), fmt).date()
        except (ValueError, TypeError):
            continue
    return date.today()


async def process_medical_document(
    db: asyncpg.Pool,
    user_id: str,
    file_bytes: bytes,
    media_type: str | None,
    filename: str,
) -> str:
    """Main entry point: parses a document and saves it to the appropriate table.

    PDF  → extracts text via fitz → text LLM → structured dict.
    Photo → vision LLM → structured dict.

    Returns a text summary to display to the user.
    """
    is_pdf = filename.lower().endswith(".pdf")

    if is_pdf:
        text = _extract_pdf_text(file_bytes)
        if len(text.strip()) < 50:
            # Scan without a text layer — render as image
            log.info("PDF '%s' has no text layer, using vision LLM", filename)
            image_bytes = _render_pdf_as_image(file_bytes)
            parsed = await analyze_medical_image(image_bytes, "image/png")
        else:
            parsed = await parse_medical_text(text)
    else:
        parsed = await analyze_medical_image(file_bytes, media_type or "image/jpeg")

    doc_type = parsed.get("document_type")
    if doc_type == "lab":
        return await _save_lab_results(db, user_id, parsed, filename)
    elif doc_type == "report":
        return await _save_doctor_report(db, user_id, parsed, filename)
    else:
        raise ValueError(f"LLM returned unknown document_type: {doc_type!r}")


async def _save_lab_results(
    db: asyncpg.Pool,
    user_id: str,
    parsed: dict,
    source_file: str,
) -> str:
    """Saves numeric lab results into lab_sessions + lab_results."""
    test_date = _parse_date(parsed.get("test_date"))

    async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
        async with conn.transaction():
            session_id = await conn.fetchval(
                """
                INSERT INTO lab_sessions (user_id, test_date, lab_name, source_file, notes)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                """,
                user_id,
                test_date,
                parsed.get("lab_name"),
                source_file,
                parsed.get("notes"),
            )

            markers = parsed.get("markers") or []
            for m in markers:
                await conn.execute(
                    """
                    INSERT INTO lab_results (
                        session_id, user_id, test_date,
                        parameter_name, parameter_key, category,
                        value_numeric, value_text, unit,
                        ref_min, ref_max, ref_text, is_abnormal
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                    """,
                    session_id, user_id, test_date,
                    m.get("parameter_name"), m.get("parameter_key"), m.get("category", "other"),
                    m.get("value_numeric"), m.get("value_text"), m.get("unit"),
                    m.get("ref_min"), m.get("ref_max"), m.get("ref_text"),
                    m.get("is_abnormal"),
                )

    total = len(markers)
    abnormal = [m for m in markers if m.get("is_abnormal")]
    lines = [f"Saved {total} values ({test_date.strftime('%d.%m.%Y')})"]
    if abnormal:
        names = ", ".join(m["parameter_name"] for m in abnormal[:5])
        extra = f" and {len(abnormal) - 5} more" if len(abnormal) > 5 else ""
        lines.append(f"Out of range: {names}{extra}")
    return "\n".join(lines)


async def _save_doctor_report(
    db: asyncpg.Pool,
    user_id: str,
    parsed: dict,
    source_file: str,
) -> str:
    """Saves a doctor report into doctor_reports."""
    study_date = _parse_date(parsed.get("study_date"))

    async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
        await conn.execute(
            """
            INSERT INTO doctor_reports (
                user_id, study_date, study_type, body_area,
                description, conclusion, equipment, doctor, lab_name, source_file
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            user_id,
            study_date,
            parsed.get("study_type", "other"),
            parsed.get("body_area"),
            parsed.get("description"),
            parsed.get("conclusion"),
            parsed.get("equipment"),
            parsed.get("doctor"),
            parsed.get("lab_name"),
            source_file,
        )

    study_type = (parsed.get("study_type") or "").upper()
    area = parsed.get("body_area") or ""
    conclusion = parsed.get("conclusion") or ""
    preview = conclusion[:250] + ("..." if len(conclusion) > 250 else "")
    header = f"{study_type} {area}".strip()
    return f"Saved: {header} ({study_date.strftime('%d.%m.%Y')})\nConclusion: {preview}"
