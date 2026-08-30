from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO, StringIO
from pathlib import Path
import hashlib
import re
import zipfile

import pandas as pd

from pipeline.db import get_connection


def _read_csv(zf: zipfile.ZipFile, filename: str) -> pd.DataFrame:
    raw = zf.read(filename)
    return pd.read_csv(StringIO(raw.decode("utf-8-sig")))


def _normalize_bool(value):
    if pd.isna(value):
        return None
    value = str(value).strip().lower()
    if value in {"true", "1", "yes"}:
        return True
    if value in {"false", "0", "no"}:
        return False
    return None


def _parse_numeric(raw_value):
    if raw_value is None or pd.isna(raw_value):
        return None
    s = str(raw_value).strip()
    if s in {"", "UNKNOWN", "NO", "YES", "YES_MANUAL"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _status(raw_value, habit_type):
    if raw_value is None or pd.isna(raw_value):
        return "missing"
    s = str(raw_value).strip()
    if s == "UNKNOWN":
        return "unknown"
    if s == "NO":
        return "not_completed"
    if s in {"YES", "YES_MANUAL"}:
        return "completed"
    if habit_type == "NUMERICAL":
        return "measured"
    return "recorded"


def _habit_from_filename(filename: str):
    # e.g. "001 Morning/Checkmarks.csv" -> ("001", "Morning")
    parent = Path(filename).parent.name
    match = re.match(r"^(\d+)\s+(.*)$", parent)
    if not match:
        return None, None
    return match.group(1), match.group(2)


def _canonical_zip_hash(zip_bytes: bytes) -> str:
    return hashlib.sha256(zip_bytes).hexdigest()


def import_loop_zip(zip_bytes: bytes, source_filename: str) -> dict:
    result = {
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "habits": 0,
        "min_date": None,
        "max_date": None,
        "changed_records": [],
        "errors": [],
    }

    source_hash = _canonical_zip_hash(zip_bytes)

    try:
        zf = zipfile.ZipFile(BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        result["errors"].append("The uploaded file is not a valid ZIP file.")
        return result

    names = set(zf.namelist())

    if "Habits.csv" not in names:
        result["errors"].append("Habits.csv was not found in the ZIP.")
        return result

    try:
        habits_df = _read_csv(zf, "Habits.csv")
    except Exception as exc:
        result["errors"].append(f"Could not read Habits.csv: {exc}")
        return result

    habits_df.columns = [str(c).strip() for c in habits_df.columns]
    required = {"Position", "Name", "Type"}
    missing = required - set(habits_df.columns)
    if missing:
        result["errors"].append(
            f"Habits.csv is missing required columns: {sorted(missing)}"
        )
        return result

    con = get_connection()

    try:
        # Use an explicit transaction so a failed import can be rolled back
        # safely. DuckDB otherwise auto-commits individual statements.
        con.execute("BEGIN TRANSACTION")

        # Import/update habit definitions from Loop.
        for row in habits_df.to_dict("records"):
            habit_id = str(row["Position"]).strip()
            if not habit_id or habit_id.lower() == "nan":
                continue

            name = str(row["Name"]).strip()
            record = {
                "habit_id": habit_id,
                "name": name,
                "position": habit_id,
                "habit_type": str(row.get("Type", "")).strip(),
                "question": None if pd.isna(row.get("Question")) else str(row.get("Question")),
                "description": None if pd.isna(row.get("Description")) else str(row.get("Description")),
                "frequency_numerator": int(row["FrequencyNumerator"]) if pd.notna(row.get("FrequencyNumerator")) else None,
                "frequency_denominator": int(row["FrequencyDenominator"]) if pd.notna(row.get("FrequencyDenominator")) else None,
                "color": None if pd.isna(row.get("Color")) else str(row.get("Color")),
                "unit": None if pd.isna(row.get("Unit")) else str(row.get("Unit")),
                "target_type": None if pd.isna(row.get("Target Type")) else str(row.get("Target Type")),
                "target_value": float(row["Target Value"]) if pd.notna(row.get("Target Value")) else None,
                "archived": _normalize_bool(row.get("Archived?")),
            }

            con.execute("""
                INSERT INTO habits (
                    habit_id, name, position, habit_type, question, description,
                    frequency_numerator, frequency_denominator, color, unit,
                    target_type, target_value, archived, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (habit_id) DO UPDATE SET
                    name = excluded.name,
                    position = excluded.position,
                    habit_type = excluded.habit_type,
                    question = excluded.question,
                    description = excluded.description,
                    frequency_numerator = excluded.frequency_numerator,
                    frequency_denominator = excluded.frequency_denominator,
                    color = excluded.color,
                    unit = excluded.unit,
                    target_type = excluded.target_type,
                    target_value = excluded.target_value,
                    archived = excluded.archived,
                    updated_at = excluded.updated_at
            """, list(record.values()) + [datetime.now(timezone.utc)])

        result["habits"] = len(habits_df)

        # Individual habit Checkmarks.csv files are the canonical observations.
        checkmark_files = [
            n for n in names
            if n.lower().endswith("/checkmarks.csv") and "/" in n
        ]

        all_dates = []

        for filename in sorted(checkmark_files):
            habit_id, _ = _habit_from_filename(filename)
            if not habit_id:
                continue

            habit_type_row = con.execute(
                "SELECT habit_type FROM habits WHERE habit_id = ?",
                [habit_id],
            ).fetchone()
            habit_type = habit_type_row[0] if habit_type_row else None

            df = _read_csv(zf, filename)
            df.columns = [str(c).strip() for c in df.columns]

            if "Date" not in df.columns or "Value" not in df.columns:
                result["errors"].append(
                    f"{filename}: expected Date and Value columns."
                )
                continue

            for row in df.to_dict("records"):
                try:
                    activity_date = pd.to_datetime(row["Date"]).date()
                except Exception:
                    result["errors"].append(
                        f"{filename}: invalid date {row.get('Date')!r}."
                    )
                    continue

                raw = None if pd.isna(row["Value"]) else str(row["Value"]).strip()
                notes = None if "Notes" not in df.columns or pd.isna(row.get("Notes")) else str(row.get("Notes"))

                existing = con.execute("""
                    SELECT raw_value, numeric_value, value_status, notes
                    FROM observations
                    WHERE activity_date = ? AND habit_id = ?
                """, [activity_date, habit_id]).fetchone()

                numeric_value = _parse_numeric(raw)
                value_status = _status(raw, habit_type)

                if existing is None:
                    con.execute("""
                        INSERT INTO observations (
                            activity_date, habit_id, raw_value, numeric_value,
                            value_status, notes, source_file, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """, [
                        activity_date, habit_id, raw, numeric_value,
                        value_status, notes, filename
                    ])
                    result["inserted"] += 1

                else:
                    old = tuple(existing)
                    new = (raw, numeric_value, value_status, notes)

                    if old != new:
                        con.execute("""
                            UPDATE observations
                            SET raw_value = ?,
                                numeric_value = ?,
                                value_status = ?,
                                notes = ?,
                                source_file = ?,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE activity_date = ? AND habit_id = ?
                        """, [
                            raw, numeric_value, value_status, notes, filename,
                            activity_date, habit_id
                        ])
                        result["updated"] += 1
                        if len(result["changed_records"]) < 100:
                            result["changed_records"].append({
                                "date": activity_date,
                                "habit_id": habit_id,
                                "old_value": old[0],
                                "new_value": raw,
                            })
                    else:
                        result["unchanged"] += 1

                all_dates.append(activity_date)

        if all_dates:
            result["min_date"] = min(all_dates)
            result["max_date"] = max(all_dates)

        # Record the import itself. Repeated uploads are allowed and are useful
        # because the underlying data can change.
        next_id = con.execute(
            "SELECT COALESCE(MAX(import_id), 0) + 1 FROM imports"
        ).fetchone()[0]

        con.execute("""
            INSERT INTO imports (
                import_id, imported_at, source_filename, source_hash,
                inserted_count, updated_count, unchanged_count,
                min_date, max_date
            )
            VALUES (?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?)
        """, [
            next_id,
            source_filename,
            source_hash,
            result["inserted"],
            result["updated"],
            result["unchanged"],
            result["min_date"],
            result["max_date"],
        ])

        con.execute("COMMIT")

    except Exception as exc:
        # The explicit BEGIN above guarantees that rollback is valid here.
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        result["errors"].append(f"Import failed: {exc}")
    finally:
        con.close()

    return result