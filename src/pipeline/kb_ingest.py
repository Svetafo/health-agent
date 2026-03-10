"""Knowledge base: loading and indexing documents (PDF, TXT, MD) and URLs."""

import logging
import os
import re
from html.parser import HTMLParser

import asyncpg

from src.config import settings
from src.llm.client import embed_text

log = logging.getLogger(__name__)

CHUNK_SIZE = 800    # characters
CHUNK_OVERLAP = 100  # characters (sliding window)

_PUBMED_RE = re.compile(r'pubmed\.ncbi\.nlm\.nih\.gov/(\d+)')


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def _extract_text_from_file(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        try:
            import fitz  # PyMuPDF
        except ImportError as e:
            raise ImportError("PyMuPDF is not installed") from e
        doc = fitz.open(path)
        pages = [page.get_text() for page in doc]
        doc.close()
        return "\n".join(pages)
    else:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()


class _HtmlTextExtractor(HTMLParser):
    """Minimal parser: extracts text and title from HTML."""

    _SKIP_TAGS = {"script", "style", "nav", "footer", "header"}

    def __init__(self):
        super().__init__()
        self._skip = 0
        self._in_title = False
        self.title = ""
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip:
            return
        s = data.strip()
        if not s:
            return
        if self._in_title:
            self.title = s
        else:
            self.parts.append(s)

    def get_text(self) -> str:
        return "\n".join(self.parts)


async def _fetch_pubmed(pmid: str) -> tuple[str, str]:
    """Fetches a PubMed article abstract via the E-utilities API."""
    import aiohttp
    url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        f"?db=pubmed&id={pmid}&rettype=abstract&retmode=text"
    )
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            text = await resp.text(errors="replace")
    return text, f"PubMed {pmid}"


async def _fetch_url(url: str) -> tuple[str, str]:
    """Fetches a page and extracts text from HTML."""
    import aiohttp
    headers = {"User-Agent": f"Mozilla/5.0 (compatible; {settings.app_name}/1.0)"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            html = await resp.text(errors="replace")
    parser = _HtmlTextExtractor()
    parser.feed(html)
    return parser.get_text(), parser.title


# ---------------------------------------------------------------------------
# Core ingest logic
# ---------------------------------------------------------------------------

def _chunk_text(text: str) -> list[str]:
    chunks = []
    start = 0
    length = len(text)
    while start < length:
        end = start + CHUNK_SIZE
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= length:
            break
        start = end - CHUNK_OVERLAP
    return chunks


async def _ingest_text(db: asyncpg.Pool, source: str, title: str, text: str) -> int:
    """Core logic: text → chunks → embeddings → DB."""
    if not text.strip():
        log.warning("kb_ingest: empty text for source=%s", source)
        return 0

    chunks = _chunk_text(text)
    if not chunks:
        return 0

    log.info("kb_ingest: %d chunks for %s", len(chunks), source)

    async with db.acquire(timeout=settings.db_acquire_timeout) as conn:
        status = await conn.execute(
            "DELETE FROM knowledge_chunks WHERE source = $1", source
        )
        # status = "DELETE N"
        deleted = int(status.split()[-1])
        if deleted:
            log.info("kb_ingest: removed %d old chunks for %s", deleted, source)

        inserted = 0
        for idx, chunk in enumerate(chunks):
            try:
                vec = await embed_text(chunk)
            except Exception as e:
                log.warning("kb_ingest: embed failed chunk %d: %s", idx, e)
                continue
            vec_str = "[" + ",".join(str(x) for x in vec) + "]"
            await conn.execute(
                """
                INSERT INTO knowledge_chunks (source, title, chunk_idx, content, embedding)
                VALUES ($1, $2, $3, $4, $5::vector)
                """,
                source, title, idx, chunk, vec_str,
            )
            inserted += 1

    log.info("kb_ingest: inserted %d chunks for %s", inserted, source)
    return inserted


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def ingest_file(db: asyncpg.Pool, path: str, title: str | None = None) -> int:
    """Indexes a file (PDF/TXT/MD) into knowledge_chunks."""
    filename = os.path.basename(path)
    log.info("kb_ingest: reading file %s", path)
    text = _extract_text_from_file(path)
    return await _ingest_text(db, filename, title or filename, text)


async def ingest_url(db: asyncpg.Pool, url: str, title: str | None = None) -> str:
    """Indexes a URL into knowledge_chunks. Returns the document title."""
    log.info("kb_ingest: fetching url %s", url)
    m = _PUBMED_RE.search(url)
    if m:
        text, doc_title = await _fetch_pubmed(m.group(1))
    else:
        text, doc_title = await _fetch_url(url)
    final_title = title or doc_title or url
    await _ingest_text(db, url, final_title, text)
    return final_title
