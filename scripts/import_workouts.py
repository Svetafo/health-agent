#!/usr/bin/env python3
"""
scripts/import_workouts.py

Import workouts from Apple Health export.xml into workout_sessions table.
Streams the file — works with files of any size.

Usage:
    python3 scripts/import_workouts.py ~/Downloads/export.xml
    python3 scripts/import_workouts.py ~/Downloads/export.xml --dry-run
    python3 scripts/import_workouts.py ~/Downloads/export.xml --from-date 2025-01-01

Dependencies (install once):
    pip install psycopg2-binary
"""

import sys
import os
import argparse
from pathlib import Path
import xml.sax
import xml.sax.handler
from datetime import datetime, date
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
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


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
# Apple Health workout type → our categories mapping
# ---------------------------------------------------------------------------

WORKOUT_TYPE_MAP: dict[str, str] = {
    # Strength
    "HKWorkoutActivityTypeFunctionalStrengthTraining": "strength",
    "HKWorkoutActivityTypeTraditionalStrengthTraining": "strength",
    "HKWorkoutActivityTypeCrossTraining": "strength",
    "HKWorkoutActivityTypeCoreTraining": "strength",
    # Cardio
    "HKWorkoutActivityTypeRunning": "cardio",
    "HKWorkoutActivityTypeCycling": "cardio",
    "HKWorkoutActivityTypeSwimming": "cardio",
    "HKWorkoutActivityTypeHighIntensityIntervalTraining": "cardio",
    "HKWorkoutActivityTypeElliptical": "cardio",
    "HKWorkoutActivityTypeRowing": "cardio",
    "HKWorkoutActivityTypeStairClimbing": "cardio",
    "HKWorkoutActivityTypeCardioDance": "cardio",
    "HKWorkoutActivityTypeMixedMetabolicCardioTraining": "cardio",
    "HKWorkoutActivityTypeMixedCardio": "cardio",
    "HKWorkoutActivityTypeDance": "cardio",
    "HKWorkoutActivityTypeCrossCountrySkiing": "cardio",
    "HKWorkoutActivityTypeSnowSports": "cardio",
    "HKWorkoutActivityTypeSquash": "cardio",
    # Low intensity
    "HKWorkoutActivityTypeWalking": "low_intensity",
    "HKWorkoutActivityTypeHiking": "low_intensity",
    "HKWorkoutActivityTypeYoga": "low_intensity",
    "HKWorkoutActivityTypePilates": "low_intensity",
    "HKWorkoutActivityTypeMindAndBody": "low_intensity",
    "HKWorkoutActivityTypeBarre": "low_intensity",
    "HKWorkoutActivityTypeFlexibility": "low_intensity",
    "HKWorkoutActivityTypeCooldown": "low_intensity",
    "HKWorkoutActivityTypeTaiChi": "low_intensity",
    "HKWorkoutActivityTypePreparationAndRecovery": "low_intensity",
}


def map_workout_type(hk_type: str) -> str:
    return WORKOUT_TYPE_MAP.get(hk_type, "other")


def parse_dt(s: str) -> datetime:
    """'2026-02-15 10:00:00 +0300' → datetime without timezone."""
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# XML Parsing
# ---------------------------------------------------------------------------

class WorkoutHandler(xml.sax.handler.ContentHandler):
    """
    SAX handler: does not build tree in memory, reads file as stream.
    Memory = O(workouts count), not O(file size).
    """

    def __init__(self, from_date: Optional[date] = None) -> None:
        self.from_date = from_date
        self.workouts: list[dict] = []
        self._current: Optional[dict] = None

    def startElement(self, name: str, attrs) -> None:  # noqa: N802
        if name == "Workout":
            started_at = parse_dt(attrs.getValue("startDate"))
            if self.from_date and started_at.date() < self.from_date:
                self._current = None
                return

            hk_type = attrs.getValue("workoutActivityType") if attrs.getNames().count("workoutActivityType") else ""
            duration = attrs.getValue("duration") if "duration" in attrs.getNames() else None
            energy = attrs.getValue("totalEnergyBurned") if "totalEnergyBurned" in attrs.getNames() else None
            distance = attrs.getValue("totalDistance") if "totalDistance" in attrs.getNames() else None
            distance_unit = attrs.getValue("totalDistanceUnit") if "totalDistanceUnit" in attrs.getNames() else "km"

            dist_val: Optional[float] = None
            if distance:
                d = float(distance)
                if d > 0:
                    dist_val = d / 1000 if distance_unit == "m" else d

            self._current = {
                "started_at": started_at,
                "ended_at": parse_dt(attrs.getValue("endDate")),
                "workout_date": started_at.date(),
                "duration_min": round(float(duration)) if duration else None,
                "workout_type": map_workout_type(hk_type),
                "workout_source": hk_type,
                "active_kcal": float(energy) if energy else None,
                "avg_heart_rate": None,
                "max_heart_rate": None,
                "distance_km": dist_val,
            }

        elif name == "WorkoutStatistics" and self._current is not None:
            if attrs.getValue("type") == "HKQuantityTypeIdentifierHeartRate":
                avg = attrs.getValue("average") if "average" in attrs.getNames() else None
                maximum = attrs.getValue("maximum") if "maximum" in attrs.getNames() else None
                if avg:
                    self._current["avg_heart_rate"] = round(float(avg))
                if maximum:
                    self._current["max_heart_rate"] = round(float(maximum))

    def endElement(self, name: str) -> None:  # noqa: N802
        if name == "Workout" and self._current is not None:
            self.workouts.append(self._current)
            self._current = None


def parse_workouts(
    xml_path: Path,
    from_date: Optional[date] = None,
) -> list[dict]:
    """
    SAX parse <Workout> elements from export.xml.
    Uses O(workouts count) memory regardless of file size.
    """
    handler = WorkoutHandler(from_date=from_date)
    xml.sax.parse(str(xml_path), handler)
    return handler.workouts


# ---------------------------------------------------------------------------
# Write to DB
# ---------------------------------------------------------------------------

UPSERT_SQL = """
INSERT INTO workout_sessions (
    user_id, started_at, ended_at, workout_date,
    duration_min, workout_type, workout_source,
    active_kcal, avg_heart_rate, max_heart_rate, distance_km,
    source
) VALUES (
    %(user_id)s, %(started_at)s, %(ended_at)s, %(workout_date)s,
    %(duration_min)s, %(workout_type)s, %(workout_source)s,
    %(active_kcal)s, %(avg_heart_rate)s, %(max_heart_rate)s, %(distance_km)s,
    'apple_health_export'
)
ON CONFLICT (user_id, started_at) DO UPDATE SET
    ended_at        = EXCLUDED.ended_at,
    duration_min    = EXCLUDED.duration_min,
    workout_type    = EXCLUDED.workout_type,
    workout_source  = EXCLUDED.workout_source,
    active_kcal     = EXCLUDED.active_kcal,
    avg_heart_rate  = EXCLUDED.avg_heart_rate,
    max_heart_rate  = EXCLUDED.max_heart_rate,
    distance_km     = EXCLUDED.distance_km
"""


def save_workouts(conn, user_id: str, workouts: list[dict], dry_run: bool) -> int:
    if not workouts:
        return 0
    rows = [{**w, "user_id": user_id} for w in workouts]
    if dry_run:
        return len(rows)
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, UPSERT_SQL, rows, page_size=200)
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def print_stats(workouts: list[dict]) -> None:
    from collections import Counter
    types = Counter(w["workout_type"] for w in workouts)
    print(f"\nWorkout types:")
    for t, n in sorted(types.items(), key=lambda x: -x[1]):
        print(f"  {t}: {n}")
    with_hr = sum(1 for w in workouts if w["avg_heart_rate"])
    print(f"\nWith HR data: {with_hr}/{len(workouts)}")
    if workouts:
        dates = [w["workout_date"] for w in workouts]
        print(f"Period: {min(dates)} — {max(dates)}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Import workouts from Apple Health export.xml")
    parser.add_argument("xml_path", help="Path to export.xml")
    parser.add_argument("--dry-run", action="store_true", help="Parse only, no DB write")
    parser.add_argument("--from-date", help="Import only from this date (YYYY-MM-DD)")
    args = parser.parse_args()

    find_and_load_env()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("Error: DATABASE_URL not set")
        sys.exit(1)

    user_id = os.environ.get("HEALTH_USER_ID")
    if not user_id:
        print("Error: HEALTH_USER_ID not set in .env")
        sys.exit(1)

    from_date: Optional[date] = None
    if args.from_date:
        from_date = date.fromisoformat(args.from_date)
        print(f"Importing from {from_date} (set manually)")
    elif not args.dry_run:
        # Auto-detect: next day after last workout in DB
        try:
            conn_tmp = psycopg2.connect(db_url)
            with conn_tmp.cursor() as cur:
                cur.execute(
                    "SELECT MAX(workout_date) FROM workout_sessions WHERE user_id = %s",
                    (user_id,)
                )
                last = cur.fetchone()[0]
            conn_tmp.close()
            if last:
                from datetime import timedelta
                from_date = last + timedelta(days=1)
                print(f"Auto from_date: {from_date} (after last workout {last})")
            else:
                print("DB is empty — importing everything")
        except Exception as e:
            print(f"Could not determine last date: {e}, importing everything")

    xml_path = Path(args.xml_path).expanduser()
    if not xml_path.exists():
        print(f"File not found: {xml_path}")
        sys.exit(1)

    print(f"Parsing {xml_path} ...")
    if from_date:
        print(f"Filter: from {from_date}")

    workouts = parse_workouts(xml_path, from_date)
    print(f"Found workouts: {len(workouts)}")
    print_stats(workouts)

    if args.dry_run:
        print("\n[DRY RUN] Write skipped.")
        return

    print(f"\nWriting to DB (user_id={user_id}) ...")
    conn = psycopg2.connect(db_url)
    saved = save_workouts(conn, user_id, workouts, dry_run=False)
    conn.close()
    print(f"Saved/updated: {saved}")


if __name__ == "__main__":
    main()
