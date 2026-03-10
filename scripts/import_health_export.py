#!/usr/bin/env python3
"""
scripts/import_health_export.py

Import historical data from Apple Health export.xml into PostgreSQL.
Streams the file via iterparse — works with files of any size.
Connects to DB via DATABASE_URL from .env.

Usage:
    python3 scripts/import_health_export.py ~/Downloads/export.xml
    python3 scripts/import_health_export.py ~/Downloads/export.xml --dry-run
    python3 scripts/import_health_export.py ~/Downloads/export.xml --from-date 2025-01-01

Dependencies (install once):
    pip install psycopg2-binary
"""

import sys
import os
import re
import argparse
from pathlib import Path
from xml.etree.ElementTree import iterparse
from datetime import datetime, date
from collections import defaultdict
from typing import Optional

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("Install dependency: pip install psycopg2-binary")
    sys.exit(1)


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------

def load_env(path: Path) -> None:
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


def find_and_load_env() -> None:
    here = Path(__file__).resolve().parent
    for p in [here, here.parent, here.parent.parent]:
        candidate = p / ".env"
        if candidate.exists():
            load_env(candidate)
            print(f"Loaded {candidate}")
            return
    print("Warning: .env not found, using environment variables")


# ---------------------------------------------------------------------------
# Apple Health → DB schema mappings
# ---------------------------------------------------------------------------

# hk_type → (field_name, aggregation)
# aggregation: "sum" | "avg" | "last"
HEALTH_TYPE_MAP: dict[str, tuple[str, str]] = {
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": ("hrv_ms",       "avg"),
    "HKQuantityTypeIdentifierVO2Max":                   ("vo2max",        "last"),
    "HKQuantityTypeIdentifierHeartRate":                ("heart_rate",    "avg"),
    "HKQuantityTypeIdentifierRestingHeartRate":         ("resting_hr",    "avg"),
    "HKQuantityTypeIdentifierStepCount":                ("steps",         "sum"),
    "HKQuantityTypeIdentifierFlightsClimbed":           ("flights",       "sum"),
    "HKQuantityTypeIdentifierActiveEnergyBurned":       ("active_kcal",   "sum"),
    "HKQuantityTypeIdentifierBasalEnergyBurned":        ("resting_kcal",  "sum"),
    "HKQuantityTypeIdentifierDistanceWalkingRunning":   ("distance_km",   "sum"),
    "HKQuantityTypeIdentifierWalkingSpeed":             ("walking_speed", "avg"),
}

# These types are taken only from Apple Watch — iPhone causes duplicates
WATCH_ONLY_TYPES = {
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN",
    "HKQuantityTypeIdentifierVO2Max",
    "HKQuantityTypeIdentifierHeartRate",
    "HKQuantityTypeIdentifierRestingHeartRate",
    "HKQuantityTypeIdentifierStepCount",
    "HKQuantityTypeIdentifierFlightsClimbed",
    "HKQuantityTypeIdentifierActiveEnergyBurned",
    "HKQuantityTypeIdentifierBasalEnergyBurned",
    "HKQuantityTypeIdentifierDistanceWalkingRunning",
}

# sleep value → table field
SLEEP_VALUE_MAP: dict[str, str] = {
    "HKCategoryValueSleepAnalysisInBed":             "in_bed",
    "HKCategoryValueSleepAnalysisAsleep":            "core",   # legacy before iOS 16
    "HKCategoryValueSleepAnalysisAsleepUnspecified": "core",
    "HKCategoryValueSleepAnalysisAsleepCore":        "core",
    "HKCategoryValueSleepAnalysisAsleepDeep":        "deep",
    "HKCategoryValueSleepAnalysisAsleepREM":         "rem",
    "HKCategoryValueSleepAnalysisAwake":             "awake",
}

WATCH_RE = re.compile(r"Apple[\s\u00a0]Watch", re.IGNORECASE)


def is_watch(source_name: str) -> bool:
    return bool(WATCH_RE.search(source_name))


def parse_dt(s: str) -> datetime:
    """"2026-02-26 23:59:00 +0300" → datetime without timezone."""
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")


def apply_unit(hk_type: str, val: float, unit: str) -> float:
    """Unit normalization to what is stored in DB."""
    if hk_type == "HKQuantityTypeIdentifierDistanceWalkingRunning":
        if unit == "m":
            return val / 1000  # meters → km
    return val


# ---------------------------------------------------------------------------
# XML Parsing
# ---------------------------------------------------------------------------

def parse_export(
    xml_path: Path,
    from_date: Optional[date] = None,
) -> tuple[dict, list]:
    """
    Streaming parse of export.xml.

    Returns:
        health_raw: dict[date, dict[hk_type, list[(float, datetime)]]]
        sleep_segs: list[dict]
    """
    # health_raw[day][hk_type] = [(value, datetime), ...]
    health_raw: dict = defaultdict(lambda: defaultdict(list))
    sleep_segs: list = []

    n_health = 0
    n_sleep = 0
    n_skipped = 0

    print(f"Reading {xml_path} ...")

    for _event, elem in iterparse(str(xml_path), events=("end",)):
        if elem.tag != "Record":
            elem.clear()
            continue

        rtype     = elem.get("type", "")
        source    = elem.get("sourceName", "")
        start_str = elem.get("startDate", "")
        end_str   = elem.get("endDate", "")

        if not start_str:
            elem.clear()
            continue

        start_dt = parse_dt(start_str)
        day = start_dt.date()

        if from_date and day < from_date:
            n_skipped += 1
            elem.clear()
            continue

        # --- health_metrics ---
        if rtype in HEALTH_TYPE_MAP:
            if rtype in WATCH_ONLY_TYPES and not is_watch(source):
                elem.clear()
                continue

            raw_val = elem.get("value", "")
            unit    = elem.get("unit", "")
            try:
                val = float(raw_val)
            except ValueError:
                elem.clear()
                continue

            val = apply_unit(rtype, val, unit)
            health_raw[day][rtype].append((val, start_dt))
            n_health += 1

            if n_health % 100_000 == 0:
                print(f"  ... {n_health:,} health records")

        # --- sleep_sessions ---
        elif rtype == "HKCategoryTypeIdentifierSleepAnalysis":
            sleep_type = SLEEP_VALUE_MAP.get(elem.get("value", ""))
            if sleep_type is None or not end_str:
                elem.clear()
                continue

            end_dt = parse_dt(end_str)
            duration_min = int((end_dt - start_dt).total_seconds() / 60)

            if duration_min <= 0:
                elem.clear()
                continue

            sleep_segs.append({
                "sleep_type":   sleep_type,
                "start":        start_dt,
                "end":          end_dt,
                "duration_min": duration_min,
            })
            n_sleep += 1

        elem.clear()

    print(
        f"  Health records: {n_health:,} | "
        f"Sleep segments: {n_sleep:,} | "
        f"Skipped (before --from-date): {n_skipped:,}"
    )
    return health_raw, sleep_segs


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate_health(raw: dict) -> list[dict]:
    """dict[date, dict[hk_type, [(val, dt)]]] → list[row] for upsert."""
    rows = []
    for day in sorted(raw.keys()):
        row: dict = {"recorded_date": day}
        for hk_type, vals in raw[day].items():
            field, agg = HEALTH_TYPE_MAP[hk_type]
            if agg == "sum":
                row[field] = round(sum(v for v, _ in vals), 3)
            elif agg == "avg":
                row[field] = round(sum(v for v, _ in vals) / len(vals), 2)
            elif agg == "last":
                # last value by time (VO2max updates rarely)
                row[field] = max(vals, key=lambda x: x[1])[0]
        rows.append(row)
    return rows


def aggregate_sleep(segments: list) -> list[dict]:
    """
    Groups sleep segments into nightly sessions.

    Algorithm:
    - Sort by startDate
    - Segments with gap < 4 hours → one session
    - sleep_date = session end date (morning)
    - If multiple sessions on one day (main sleep + nap) → keep the longest
    """
    if not segments:
        return []

    segments = sorted(segments, key=lambda s: s["start"])

    # Group into sessions
    sessions: list[list] = []
    current = [segments[0]]
    for seg in segments[1:]:
        gap_hours = (seg["start"] - current[-1]["end"]).total_seconds() / 3600
        if gap_hours < 4:
            current.append(seg)
        else:
            sessions.append(current)
            current = [seg]
    sessions.append(current)

    by_date: dict[date, list] = defaultdict(list)
    for segs in sessions:
        session_start = min(s["start"] for s in segs)
        session_end   = max(s["end"]   for s in segs)
        sleep_date    = session_end.date()

        deep_min  = sum(s["duration_min"] for s in segs if s["sleep_type"] == "deep")
        rem_min   = sum(s["duration_min"] for s in segs if s["sleep_type"] == "rem")
        core_min  = sum(s["duration_min"] for s in segs if s["sleep_type"] == "core")
        awake_min = sum(s["duration_min"] for s in segs if s["sleep_type"] == "awake")
        in_bed_min = sum(s["duration_min"] for s in segs if s["sleep_type"] == "in_bed")

        total_min = deep_min + rem_min + core_min

        # If no InBed segments (old format), use full session range
        if in_bed_min == 0:
            in_bed_min = int((session_end - session_start).total_seconds() / 60)

        efficiency_pct = (
            round(total_min / in_bed_min * 100, 1)
            if in_bed_min > 0 and total_min > 0 else None
        )

        by_date[sleep_date].append({
            "sleep_date":     sleep_date,
            "bedtime_start":  session_start,
            "bedtime_end":    session_end,
            "total_min":      total_min or None,
            "in_bed_min":     in_bed_min or None,
            "deep_min":       deep_min or None,
            "rem_min":        rem_min or None,
            "core_min":       core_min or None,
            "awake_min":      awake_min or None,
            "efficiency_pct": efficiency_pct,
        })

    # Multiple sessions on one day → keep the longest (discard naps)
    result = []
    for sleep_date in sorted(by_date.keys()):
        best = max(by_date[sleep_date], key=lambda r: r["total_min"] or 0)
        result.append(best)
    return result


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

HEALTH_FIELDS = [
    "hrv_ms", "vo2max", "heart_rate", "resting_hr",
    "steps", "flights", "active_kcal", "resting_kcal",
    "distance_km", "walking_speed",
]


def upsert_health(cur, user_id: str, rows: list[dict]) -> int:
    if not rows:
        return 0

    sql = """
        INSERT INTO health_metrics (
            user_id, recorded_date,
            hrv_ms, vo2max, heart_rate, resting_hr,
            steps, flights, active_kcal, resting_kcal,
            distance_km, walking_speed,
            source
        ) VALUES %s
        ON CONFLICT (user_id, recorded_date) DO UPDATE SET
            hrv_ms        = COALESCE(EXCLUDED.hrv_ms,        health_metrics.hrv_ms),
            vo2max        = COALESCE(EXCLUDED.vo2max,        health_metrics.vo2max),
            heart_rate    = COALESCE(EXCLUDED.heart_rate,    health_metrics.heart_rate),
            resting_hr    = COALESCE(EXCLUDED.resting_hr,    health_metrics.resting_hr),
            steps         = COALESCE(EXCLUDED.steps,         health_metrics.steps),
            flights       = COALESCE(EXCLUDED.flights,       health_metrics.flights),
            active_kcal   = COALESCE(EXCLUDED.active_kcal,  health_metrics.active_kcal),
            resting_kcal  = COALESCE(EXCLUDED.resting_kcal, health_metrics.resting_kcal),
            distance_km   = COALESCE(EXCLUDED.distance_km,  health_metrics.distance_km),
            walking_speed = COALESCE(EXCLUDED.walking_speed, health_metrics.walking_speed),
            source        = 'apple_health_export'
    """

    values = [
        (
            user_id,
            row["recorded_date"],
            row.get("hrv_ms"),
            row.get("vo2max"),
            row.get("heart_rate"),
            row.get("resting_hr"),
            row.get("steps"),
            row.get("flights"),
            row.get("active_kcal"),
            row.get("resting_kcal"),
            row.get("distance_km"),
            row.get("walking_speed"),
            "apple_health_export",
        )
        for row in rows
    ]

    psycopg2.extras.execute_values(cur, sql, values, page_size=500)
    return len(rows)


def upsert_sleep(cur, user_id: str, rows: list[dict]) -> int:
    if not rows:
        return 0

    sql = """
        INSERT INTO sleep_sessions (
            user_id, sleep_date,
            bedtime_start, bedtime_end,
            total_min, in_bed_min,
            deep_min, rem_min, core_min, awake_min,
            efficiency_pct, source
        ) VALUES %s
        ON CONFLICT (user_id, sleep_date) DO UPDATE SET
            bedtime_start  = EXCLUDED.bedtime_start,
            bedtime_end    = EXCLUDED.bedtime_end,
            total_min      = EXCLUDED.total_min,
            in_bed_min     = EXCLUDED.in_bed_min,
            deep_min       = COALESCE(EXCLUDED.deep_min,  sleep_sessions.deep_min),
            rem_min        = COALESCE(EXCLUDED.rem_min,   sleep_sessions.rem_min),
            core_min       = COALESCE(EXCLUDED.core_min,  sleep_sessions.core_min),
            awake_min      = COALESCE(EXCLUDED.awake_min, sleep_sessions.awake_min),
            efficiency_pct = EXCLUDED.efficiency_pct,
            source         = 'apple_health_export'
    """

    values = [
        (
            user_id,
            row["sleep_date"],
            row["bedtime_start"],
            row["bedtime_end"],
            row["total_min"],
            row["in_bed_min"],
            row["deep_min"],
            row["rem_min"],
            row["core_min"],
            row["awake_min"],
            row["efficiency_pct"],
            "apple_health_export",
        )
        for row in rows
    ]

    psycopg2.extras.execute_values(cur, sql, values, page_size=500)
    return len(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def print_health_sample(rows: list[dict]) -> None:
    if not rows:
        return
    last = rows[-1]
    print(f"\nLast health day: {last['recorded_date']}")
    for f in HEALTH_FIELDS:
        v = last.get(f)
        if v is not None:
            print(f"    {f}: {v}")


def print_sleep_sample(rows: list[dict]) -> None:
    if not rows:
        return
    last = rows[-1]
    print(f"\nLast sleep night: {last['sleep_date']}")
    print(
        f"    total={last['total_min']} min | "
        f"deep={last['deep_min']} | REM={last['rem_min']} | core={last['core_min']} | "
        f"in_bed={last['in_bed_min']} | efficiency={last['efficiency_pct']}%"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import Apple Health export.xml into PostgreSQL"
    )
    parser.add_argument("xml_path", help="Path to export.xml")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse and aggregate only, do not write to DB"
    )
    parser.add_argument(
        "--from-date", metavar="YYYY-MM-DD",
        help="Import only from this date (inclusive)"
    )
    parser.add_argument(
        "--user-id", default=None,
        help="User USER_ID (default — HEALTH_USER_ID from .env)"
    )
    args = parser.parse_args()

    find_and_load_env()

    xml_path = Path(args.xml_path).expanduser()
    if not xml_path.exists():
        print(f"File not found: {xml_path}")
        sys.exit(1)

    user_id = args.user_id or os.environ.get("HEALTH_USER_ID")
    if not user_id:
        print("Specify --user-id or set HEALTH_USER_ID in .env")
        sys.exit(1)
    print(f"User ID: {user_id}")

    from_date: Optional[date] = None
    if args.from_date:
        from_date = date.fromisoformat(args.from_date)
        print(f"Importing from {from_date} (set manually)")
    elif not args.dry_run:
        # Auto-detect: next day after last record in DB
        database_url = os.environ.get("DATABASE_URL")
        if database_url:
            try:
                conn_tmp = psycopg2.connect(database_url)
                with conn_tmp.cursor() as cur:
                    cur.execute(
                        "SELECT MAX(recorded_date) FROM health_metrics WHERE user_id = %s",
                        (user_id,)
                    )
                    last = cur.fetchone()[0]
                conn_tmp.close()
                if last:
                    from datetime import timedelta
                    from_date = last + timedelta(days=1)
                    print(f"Auto from_date: {from_date} (after last record {last})")
                else:
                    print("DB is empty — importing everything")
            except Exception as e:
                print(f"Could not determine last date: {e}, importing everything")

    # --- Parsing ---
    health_raw, sleep_segs = parse_export(xml_path, from_date)

    # --- Aggregation ---
    print("\nAggregating...")
    health_rows = aggregate_health(health_raw)
    sleep_rows  = aggregate_sleep(sleep_segs)

    print(f"  health_metrics: {len(health_rows)} days")
    print(f"  sleep_sessions: {len(sleep_rows)} nights")

    print_health_sample(health_rows)
    print_sleep_sample(sleep_rows)

    # --- Database ---
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("\nDATABASE_URL not set in .env")
        sys.exit(1)

    if args.dry_run:
        print("\n[DRY RUN] Not writing to DB.")
        return

    print(f"\nConnecting to DB...")
    try:
        conn = psycopg2.connect(database_url)
    except Exception as e:
        print(f"Connection error: {e}")
        sys.exit(1)

    try:
        with conn:
            with conn.cursor() as cur:
                print("Writing health_metrics...")
                n_health = upsert_health(cur, user_id, health_rows)
                print(f"  → upsert {n_health} days")

                print("Writing sleep_sessions...")
                n_sleep = upsert_sleep(cur, user_id, sleep_rows)
                print(f"  → upsert {n_sleep} nights")
    finally:
        conn.close()

    print("\nDone.")


if __name__ == "__main__":
    main()
