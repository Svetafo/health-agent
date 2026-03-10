"""Apple Health data processing: normalization and saving to DB."""

import json
from decimal import Decimal, InvalidOperation
from datetime import date, datetime

import asyncpg

WORKOUT_TYPE_MAP: dict[str, str] = {
    "HKWorkoutActivityTypeFunctionalStrengthTraining": "strength",
    "HKWorkoutActivityTypeTraditionalStrengthTraining": "strength",
    "HKWorkoutActivityTypeCrossTraining": "strength",
    "HKWorkoutActivityTypeCoreTraining": "strength",
    "HKWorkoutActivityTypeRunning": "cardio",
    "HKWorkoutActivityTypeCycling": "cardio",
    "HKWorkoutActivityTypeSwimming": "cardio",
    "HKWorkoutActivityTypeHighIntensityIntervalTraining": "cardio",
    "HKWorkoutActivityTypeElliptical": "cardio",
    "HKWorkoutActivityTypeRowing": "cardio",
    "HKWorkoutActivityTypeStairClimbing": "cardio",
    "HKWorkoutActivityTypeWalking": "low_intensity",
    "HKWorkoutActivityTypeHiking": "low_intensity",
    "HKWorkoutActivityTypeYoga": "low_intensity",
    "HKWorkoutActivityTypePilates": "low_intensity",
    "HKWorkoutActivityTypeMindAndBody": "low_intensity",
    "HKWorkoutActivityTypeBarre": "low_intensity",
    "HKWorkoutActivityTypeFlexibility": "low_intensity",
    "HKWorkoutActivityTypeCooldown": "low_intensity",
}


def normalize_decimal(value) -> Decimal | None:
    """Comma → period, empty string → None."""
    if value is None:
        return None
    s = str(value).strip().replace(",", ".")
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def normalize_int(value) -> int | None:
    d = normalize_decimal(value)
    return int(d) if d is not None else None


def normalize_sleep_min(value) -> int | None:
    """Normalizes sleep minutes: if value > 1440 — it's in seconds, convert to minutes."""
    d = normalize_decimal(value)
    if d is None:
        return None
    v = int(d)
    return v // 60 if v > 1440 else v


def parse_datetime(value) -> datetime | None:
    """Parses ISO datetime from Shortcuts. Strips timezone (DB stores naive datetimes).
    Supports: '2026-03-05T23:15:00+03:00', '2026-03-05T23:15:00+0300', no tz.
    """
    if not value:
        return None
    s = str(value).strip()
    # Normalize +0300 → +03:00 (Python 3.12 fromisoformat doesn't handle missing colon)
    if len(s) >= 24 and s[-5] in "+-" and ":" not in s[-6:]:
        s = s[:-2] + ":" + s[-2:]
    try:
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=None)
    except (ValueError, AttributeError):
        # Fallback: take first 19 characters
        try:
            return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None


def parse_date(value) -> date:
    """Parses a date from a string. Supports ISO and common formats.
    Handles the Shortcuts nested object: {"": "2026-02-25"} → "2026-02-25".
    """
    if not value:
        return datetime.now().date()
    # Shortcuts wraps date in {"": "YYYY-MM-DD"} — extract the string
    if isinstance(value, dict):
        value = next(iter(value.values()), None)
    if not value:
        return datetime.now().date()
    s = str(value).strip()
    # iOS Shortcuts may send date+time: "07.03.2026, 12:00" — take only the date part
    if ", " in s:
        s = s.split(", ")[0]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {s!r}")


async def save_health_metrics(
    db: asyncpg.Pool,
    data: dict,
    user_id: str,
) -> None:
    """Saves a raw event and performs an upsert into health_metrics."""

    # 1. Save raw event for tracing
    raw_event_id = await db.fetchval(
        """
        INSERT INTO raw_events (source, event_type, payload)
        VALUES ('apple_health', 'health_metrics', $1)
        RETURNING id
        """,
        json.dumps(data),
    )

    # 2. Parse date
    recorded_date = parse_date(data.get("date"))

    # 3. Normalize metrics
    hrv_ms = normalize_decimal(data.get("hrv_ms"))
    vo2max = normalize_decimal(data.get("vo2max"))
    heart_rate = normalize_decimal(data.get("heart_rate"))
    resting_hr = normalize_decimal(data.get("resting_hr"))
    steps = normalize_int(data.get("steps"))
    flights = normalize_int(data.get("flights"))
    active_kcal = normalize_decimal(data.get("active_kcal"))
    resting_kcal = normalize_decimal(data.get("resting_kcal"))
    distance_km = normalize_decimal(data.get("distance_km"))
    walking_speed = normalize_decimal(data.get("walking_speed"))

    # 4. Upsert: repeated submission for the same day — update existing record
    await db.execute(
        """
        INSERT INTO health_metrics (
            user_id, recorded_date,
            hrv_ms, vo2max, heart_rate, resting_hr,
            steps, flights,
            active_kcal, resting_kcal, distance_km, walking_speed,
            source, raw_event_id
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
            'apple_health', $13
        )
        ON CONFLICT (user_id, recorded_date) DO UPDATE SET
            hrv_ms        = EXCLUDED.hrv_ms,
            vo2max        = EXCLUDED.vo2max,
            heart_rate    = EXCLUDED.heart_rate,
            resting_hr    = EXCLUDED.resting_hr,
            steps         = EXCLUDED.steps,
            flights       = EXCLUDED.flights,
            active_kcal   = EXCLUDED.active_kcal,
            resting_kcal  = EXCLUDED.resting_kcal,
            distance_km   = EXCLUDED.distance_km,
            walking_speed = EXCLUDED.walking_speed,
            raw_event_id  = EXCLUDED.raw_event_id
        """,
        user_id,
        recorded_date,
        hrv_ms,
        vo2max,
        heart_rate,
        resting_hr,
        steps,
        flights,
        active_kcal,
        resting_kcal,
        distance_km,
        walking_speed,
        raw_event_id,
    )


_SLEEP_VALUE_MAP = {
    "asleepdeep": "deep", "deep": "deep",
    "asleeprem": "rem", "rem": "rem",
    "asleepcore": "core", "core": "core", "asleep": "core",
    "awake": "awake",
    "inbed": "in_bed",
}


def _parse_sleep_segments(segments: list) -> dict:
    """Aggregates an array of segments [{value, duration}, ...] into sleep phases."""
    totals: dict[str, float] = {}
    for seg in segments:
        raw = str(seg.get("value", "")).lower()
        # strip the hkcategoryvaluesleepanalysis prefix
        raw = raw.replace("hkcategoryvaluesleepanalysis", "")
        phase = _SLEEP_VALUE_MAP.get(raw)
        if not phase:
            continue
        dur = seg.get("duration") or seg.get("duration_min") or 0
        try:
            totals[phase] = totals.get(phase, 0) + float(dur)
        except (TypeError, ValueError):
            pass
    return {k: round(v) for k, v in totals.items()}


async def save_sleep_session(
    db: asyncpg.Pool,
    data: dict,
    user_id: str,
) -> None:
    """Upserts a sleep record from Shortcuts into sleep_sessions.
    Accepts either pre-computed fields deep_min/rem_min/...,
    or a segments array: [{value, duration}, ...].
    """
    sleep_date = parse_date(data.get("sleep_date"))
    bedtime_start = parse_datetime(data.get("bedtime_start"))
    bedtime_end = parse_datetime(data.get("bedtime_end"))

    segments = data.get("segments")
    if segments and isinstance(segments, list):
        phases = _parse_sleep_segments(segments)
        deep_min = phases.get("deep")
        rem_min = phases.get("rem")
        core_min = phases.get("core")
        awake_min = phases.get("awake")
        in_bed_min = phases.get("in_bed")
    else:
        deep_min = normalize_sleep_min(data.get("deep_min"))
        rem_min = normalize_sleep_min(data.get("rem_min"))
        core_min = normalize_sleep_min(data.get("core_min"))
        awake_min = normalize_sleep_min(data.get("awake_min"))
        in_bed_min = normalize_sleep_min(data.get("in_bed_min"))

    # total = deep + REM + core (excluding awake and in_bed)
    parts = [x for x in [deep_min, rem_min, core_min] if x is not None]
    total_min = sum(parts) if parts else None

    efficiency_pct = None
    if total_min and in_bed_min and in_bed_min > 0:
        efficiency_pct = round(total_min / in_bed_min * 100, 1)

    await db.execute(
        """
        INSERT INTO sleep_sessions (
            user_id, sleep_date, bedtime_start, bedtime_end,
            total_min, in_bed_min, deep_min, rem_min, core_min, awake_min,
            efficiency_pct
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        ON CONFLICT (user_id, sleep_date) DO UPDATE SET
            bedtime_start  = EXCLUDED.bedtime_start,
            bedtime_end    = EXCLUDED.bedtime_end,
            total_min      = EXCLUDED.total_min,
            in_bed_min     = EXCLUDED.in_bed_min,
            deep_min       = EXCLUDED.deep_min,
            rem_min        = EXCLUDED.rem_min,
            core_min       = EXCLUDED.core_min,
            awake_min      = EXCLUDED.awake_min,
            efficiency_pct = EXCLUDED.efficiency_pct
        """,
        user_id, sleep_date, bedtime_start, bedtime_end,
        total_min, in_bed_min, deep_min, rem_min, core_min, awake_min,
        efficiency_pct,
    )


async def save_workout_session(
    db: asyncpg.Pool,
    data: dict,
    user_id: str,
) -> None:
    """Upserts a single workout from Shortcuts into workout_sessions."""
    workout_date = parse_date(data.get("workout_date"))
    started_at = parse_datetime(data.get("started_at"))
    ended_at = parse_datetime(data.get("ended_at"))
    duration_min = normalize_int(data.get("duration_min"))
    workout_source = str(data.get("workout_type", ""))
    workout_type = WORKOUT_TYPE_MAP.get(workout_source, "other")
    active_kcal = normalize_decimal(data.get("active_kcal"))
    avg_heart_rate = normalize_int(data.get("avg_heart_rate"))
    distance_km = normalize_decimal(data.get("distance_km"))

    # started_at — uniqueness key. If not provided, generate from workout_date.
    if started_at is None:
        started_at = datetime.combine(workout_date, datetime.min.time())

    await db.execute(
        """
        INSERT INTO workout_sessions (
            user_id, started_at, ended_at, workout_date,
            duration_min, workout_type, workout_source,
            active_kcal, avg_heart_rate, distance_km, source
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'apple_health_shortcut')
        ON CONFLICT (user_id, started_at) DO UPDATE SET
            ended_at       = EXCLUDED.ended_at,
            duration_min   = EXCLUDED.duration_min,
            workout_type   = EXCLUDED.workout_type,
            workout_source = EXCLUDED.workout_source,
            active_kcal    = EXCLUDED.active_kcal,
            avg_heart_rate = EXCLUDED.avg_heart_rate,
            distance_km    = EXCLUDED.distance_km
        """,
        user_id, started_at, ended_at, workout_date,
        duration_min, workout_type, workout_source,
        active_kcal, avg_heart_rate, distance_km,
    )
