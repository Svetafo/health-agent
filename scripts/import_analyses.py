#!/usr/bin/env python3
"""
scripts/import_analyses.py

Batch import of medical lab results from a directory into PostgreSQL.
Uses the same parser as the /lab command in the bot.

Run from project root:
    python3 scripts/import_analyses.py private/analyses/
    python3 scripts/import_analyses.py private/analyses/ --dry-run
    python3 scripts/import_analyses.py private/analyses/ --file krov_obshiy.pdf

Dependencies (should already be in requirements.txt):
    asyncpg, openai, anthropic, pymupdf
"""

import asyncio
import os
import sys
import argparse
from pathlib import Path

# Script runs inside Docker container where all dependencies are installed.
# /app/.env is mounted into the container automatically.

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import asyncpg
import src.config as _cfg_module
from src.health.analyses import process_medical_document

# ---------------------------------------------------------------------------
# Supported formats
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS = {
    ".pdf": None,          # media_type not needed, processed via text
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def _collect_files(directory: Path, single_file: str | None) -> list[Path]:
    """Collects list of files to process."""
    if single_file:
        p = directory / single_file
        if not p.exists():
            print(f"File not found: {p}")
            sys.exit(1)
        return [p]

    files = []
    for f in sorted(directory.iterdir()):
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS:
            files.append(f)
    return files


async def _already_imported(conn: asyncpg.Connection, user_id: str, filename: str) -> bool:
    """Checks if the file was already imported (by filename)."""
    exists_lab = await conn.fetchval(
        "SELECT 1 FROM lab_sessions WHERE user_id = $1 AND source_file = $2 LIMIT 1",
        user_id, filename,
    )
    if exists_lab:
        return True
    exists_report = await conn.fetchval(
        "SELECT 1 FROM doctor_reports WHERE user_id = $1 AND source_file = $2 LIMIT 1",
        user_id, filename,
    )
    return bool(exists_report)


async def import_analyses(
    directory: Path,
    user_id: str,
    single_file: str | None = None,
    dry_run: bool = False,
    skip_existing: bool = True,
) -> None:
    files = _collect_files(directory, single_file)

    if not files:
        print("No files to process.")
        return

    print(f"Found files: {len(files)}")
    if dry_run:
        print("— DRY RUN, nothing is written to DB —\n")
        for f in files:
            print(f"  {f.name}")
        return

    db_url = _cfg_module.settings.database_url
    if not db_url:
        print("Error: DATABASE_URL not set in .env")
        sys.exit(1)

    pool = await asyncpg.create_pool(db_url)
    print()

    ok = 0
    skipped = 0
    errors = []

    for i, filepath in enumerate(files, 1):
        filename = filepath.name
        ext = filepath.suffix.lower()
        media_type = SUPPORTED_EXTENSIONS.get(ext)

        print(f"[{i:2}/{len(files)}] {filename}", end=" ... ", flush=True)

        async with pool.acquire() as conn:
            if skip_existing and await _already_imported(conn, user_id, filename):
                print("skipped (already in DB)")
                skipped += 1
                continue

        try:
            file_bytes = filepath.read_bytes()
            summary = await process_medical_document(pool, user_id, file_bytes, media_type, filename)
            # Remove newlines for clean output
            summary_short = summary.replace("\n", " | ")
            print(f"✓ {summary_short}")
            ok += 1
        except Exception as e:
            print(f"✗ Error: {e}")
            errors.append((filename, str(e)))

    await pool.close()

    print(f"\n{'─' * 50}")
    print(f"Done: {ok} loaded, {skipped} skipped, {len(errors)} errors")
    if errors:
        print("\nErrors:")
        for name, err in errors:
            print(f"  {name}: {err}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import medical lab results to DB")
    parser.add_argument("directory", help="Path to folder with lab files")
    parser.add_argument("--dry-run", action="store_true", help="Show file list without writing to DB")
    parser.add_argument("--file", metavar="FILENAME", help="Process only one specific file")
    parser.add_argument("--user-id", default=os.environ.get("HEALTH_USER_ID"), help="Telegram user_id (default: HEALTH_USER_ID from .env)")
    parser.add_argument("--no-skip", action="store_true", help="Do not skip already imported files")
    args = parser.parse_args()

    directory = Path(args.directory)
    if not directory.exists():
        print(f"Folder not found: {directory}")
        sys.exit(1)

    asyncio.run(import_analyses(
        directory=directory,
        user_id=args.user_id,
        single_file=args.file,
        dry_run=args.dry_run,
        skip_existing=not args.no_skip,
    ))


if __name__ == "__main__":
    main()
