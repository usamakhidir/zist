from __future__ import annotations

import os
import sys
import tempfile
from datetime import date

import duckdb


# ------------------------------------------------------------------
# Make zist_local importable when this script is run directly.
# ------------------------------------------------------------------

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(
        os.path.abspath(__file__)
    )
)

if PROJECT_ROOT not in sys.path:
    sys.path.insert(
        0,
        PROJECT_ROOT,
    )


from pipeline.db import SCHEMA_SQL
from pipeline.calculation import (
    calculate_raw_metric_values,
    calculate_derived_metric_values,
    calculate_metric_values,
    calculate_metric_scores,
    calculate_metric_progression,
    persist_metric_xp,
    calculate_category_total_xp,
    calculate_category_levels,
)


TEST_DATE = date(2026, 8, 24)


# ================================================================
# Helpers
# ================================================================

def assert_close(
    actual,
    expected,
    tolerance=1e-9,
    message="",
):
    if actual is None:
        raise AssertionError(
            f"{message} Expected {expected}, got None."
        )

    if abs(float(actual) - float(expected)) > tolerance:
        raise AssertionError(
            f"{message} Expected {expected}, got {actual}."
        )


def assert_equal(
    actual,
    expected,
    message="",
):
    if actual != expected:
        raise AssertionError(
            f"{message} Expected {expected}, got {actual}."
        )


def section(title):
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


# ================================================================
# Test database
# ================================================================

def create_test_database():
    """
    Create a completely isolated temporary DuckDB.

    The production initialize_database() function operates on
    the application's configured database, so it is deliberately
    not used here.

    Instead, the exact production SCHEMA_SQL is applied to the
    temporary connection.
    """

    fd, db_path = tempfile.mkstemp(
        suffix=".duckdb"
    )

    os.close(fd)
    os.remove(db_path)

    con = duckdb.connect(db_path)

    # Apply the actual Zist production schema to the
    # isolated test database.
    con.execute(SCHEMA_SQL)

    return con, db_path
# ================================================================
# Controlled test configuration
# ================================================================

def seed_test_data(con):
    """
    Create a deliberately small but complete test scenario.

    Category:
        Health

    RAW metrics:
        Exercise Minutes
        Sleep Hours

    DERIVED metric:
        Exercise Efficiency

    Scoring:
        Exercise Minutes -> TARGET
        Sleep Hours      -> TARGET
        Exercise Efficiency -> RATING

    XP:
        Exercise Minutes -> PROPORTIONAL
        Sleep Hours      -> FIXED
        Exercise Efficiency -> PROPORTIONAL
    """

    # ------------------------------------------------------------
    # Category
    # ------------------------------------------------------------

    con.execute(
        """
        INSERT INTO categories (
            category_id,
            name,
            description,
            start_date
        )
        VALUES (
            1,
            'Health',
            'Validation category',
            ?
        )
        """,
        [TEST_DATE],
    )

    # ------------------------------------------------------------
    # Habits
    # ------------------------------------------------------------

    con.execute(
        """
        INSERT INTO habits (
            habit_id,
            name,
            habit_type,
            question,
            unit
        )
        VALUES
            (
                'exercise',
                'Exercise',
                'duration',
                'How many minutes did you exercise?',
                'minutes'
            ),
            (
                'sleep',
                'Sleep',
                'duration',
                'How many hours did you sleep?',
                'hours'
            )
        """
    )

    # ------------------------------------------------------------
    # RAW metrics
    # ------------------------------------------------------------

    con.execute(
        """
        INSERT INTO metrics (
            metric_id,
            name,
            measurement_type,
            unit,
            metric_kind,
            category_id,
            start_date
        )
        VALUES
            (
                1,
                'Exercise Minutes',
                'NUMERIC',
                'minutes',
                'RAW',
                1,
                ?
            ),
            (
                2,
                'Sleep Hours',
                'NUMERIC',
                'hours',
                'RAW',
                1,
                ?
            )
        """,
        [TEST_DATE, TEST_DATE],
    )

    # ------------------------------------------------------------
    # Derived metric
    #
    # Exercise Efficiency
    # = Exercise Minutes / 60
    #
    # This deliberately tests formula evaluation.
    # ------------------------------------------------------------

    con.execute(
        """
        INSERT INTO metrics (
            metric_id,
            name,
            measurement_type,
            unit,
            metric_kind,
            formula,
            category_id,
            start_date
        )
        VALUES (
            3,
            'Exercise Efficiency',
            'NUMERIC',
            'ratio',
            'DERIVED',
            'Exercise_Minutes / 60',
            1,
            ?
        )
        """,
        [TEST_DATE],
    )

    con.execute(
        """
        INSERT INTO metric_formula_inputs (
            formula_input_id,
            derived_metric_id,
            source_metric_id,
            variable_name
        )
        VALUES (
            1,
            3,
            1,
            'Exercise_Minutes'
        )
        """
    )

    # ------------------------------------------------------------
    # Habit -> Metric mappings
    # ------------------------------------------------------------

    con.execute(
        """
        INSERT INTO metric_habit_mapping (
            mapping_id,
            metric_id,
            habit_id,
            weight,
            start_date
        )
        VALUES
            (
                1,
                1,
                'exercise',
                1.0,
                ?
            ),
            (
                2,
                2,
                'sleep',
                1.0,
                ?
            )
        """,
        [TEST_DATE, TEST_DATE],
    )

    # ------------------------------------------------------------
    # Observations
    #
    # Exercise = 45 minutes
    # Sleep    = 8 hours
    # ------------------------------------------------------------

    con.execute(
        """
        INSERT INTO observations (
            activity_date,
            habit_id,
            numeric_value,
            value_status
        )
        VALUES
            (?, 'exercise', 45, 'completed'),
            (?, 'sleep', 8, 'completed')
        """,
        [TEST_DATE, TEST_DATE],
    )

    # ------------------------------------------------------------
    # Scoring rules
    # ------------------------------------------------------------

    # Exercise:
    # 45 / 60 = 75%
    # max_points = 20
    # points = 15

    con.execute(
        """
        INSERT INTO scoring_rules (
            rule_id,
            metric_id,
            scoring_method,
            target_value,
            max_points,
            start_date
        )
        VALUES (
            1,
            1,
            'TARGET',
            60,
            20,
            ?
        )
        """,
        [TEST_DATE],
    )

    # Sleep:
    # 8 / 8 = 100%
    # max_points = 10

    con.execute(
        """
        INSERT INTO scoring_rules (
            rule_id,
            metric_id,
            scoring_method,
            target_value,
            max_points,
            start_date
        )
        VALUES (
            2,
            2,
            'TARGET',
            8,
            10,
            ?
        )
        """,
        [TEST_DATE],
    )

    # Exercise Efficiency:
    # 45 / 60 = 0.75
    # Rating maximum = 1
    # achievement = 75%
    # max_points = 10

    con.execute(
        """
        INSERT INTO scoring_rules (
            rule_id,
            metric_id,
            scoring_method,
            rating_max,
            max_points,
            start_date
        )
        VALUES (
            3,
            3,
            'RATING',
            1,
            10,
            ?
        )
        """,
        [TEST_DATE],
    )

    # ------------------------------------------------------------
    # Metric XP
    # ------------------------------------------------------------

    # Exercise:
    # 20 base × 75% = 15 XP

    con.execute(
        """
        INSERT INTO metric_xp_config (
            metric_id,
            base_xp,
            xp_method,
            multiplier,
            daily_cap
        )
        VALUES (
            1,
            20,
            'PROPORTIONAL',
            1,
            25
        )
        """
    )

    # Sleep:
    # FIXED:
    # 15 × 1 = 15 XP

    con.execute(
        """
        INSERT INTO metric_xp_config (
            metric_id,
            base_xp,
            xp_method,
            multiplier,
            daily_cap
        )
        VALUES (
            2,
            15,
            'FIXED',
            1,
            25
        )
        """
    )

    # Exercise Efficiency:
    # 10 × 75% = 7.5 XP

    con.execute(
        """
        INSERT INTO metric_xp_config (
            metric_id,
            base_xp,
            xp_method,
            multiplier,
            daily_cap
        )
        VALUES (
            3,
            10,
            'PROPORTIONAL',
            1,
            25
        )
        """
    )

    # ------------------------------------------------------------
    # Category progression
    # ------------------------------------------------------------

    con.execute(
        """
        INSERT INTO category_xp_config (
            category_id,
            progression_method,
            base_xp,
            growth_rate
        )
        VALUES (
            1,
            'LINEAR',
            100,
            1.25
        )
        """
    )


# ================================================================
# STEP 1 TEST
# ================================================================

def test_step_1_raw_metrics(con):

    section("STEP 1 — RAW METRIC CALCULATION")

    result = calculate_raw_metric_values(
        con,
        start_date=TEST_DATE,
        end_date=TEST_DATE,
    )

    print(result.to_string(index=False))

    assert_equal(
        len(result),
        2,
        "Expected two RAW metrics.",
    )

    exercise = result[
        result["metric_id"] == 1
    ].iloc[0]

    sleep = result[
        result["metric_id"] == 2
    ].iloc[0]

    assert_close(
        exercise["value"],
        45,
        message="Exercise RAW value.",
    )

    assert_close(
        sleep["value"],
        8,
        message="Sleep RAW value.",
    )

    print("✓ RAW metric values correct")


# ================================================================
# STEP 2 TEST
# ================================================================

def test_step_2_derived_metrics(con):

    section("STEP 2 — DERIVED METRICS")

    raw = calculate_raw_metric_values(
        con,
        start_date=TEST_DATE,
        end_date=TEST_DATE,
    )

    result = calculate_derived_metric_values(
        raw,
        con,
        start_date=TEST_DATE,
        end_date=TEST_DATE,
    )

    print(result.to_string(index=False))

    assert_equal(
        len(result),
        1,
        "Expected one derived metric.",
    )

    efficiency = result[
        result["metric_id"] == 3
    ].iloc[0]

    assert_close(
        efficiency["value"],
        0.75,
        message="Exercise Efficiency.",
    )

    print("✓ Derived metric correct")


# ================================================================
# STEP 3 TEST
# ================================================================

def test_step_3_scoring(con):

    section("STEP 3 — SCORING")

    metric_values = calculate_metric_values(
        con,
        start_date=TEST_DATE,
        end_date=TEST_DATE,
    )

    result = calculate_metric_scores(
        metric_values,
        con,
        start_date=TEST_DATE,
        end_date=TEST_DATE,
    )

    print(result.to_string(index=False))

    assert_equal(
        len(result),
        3,
        "Expected three scored metrics.",
    )

    exercise = result[
        result["metric_id"] == 1
    ].iloc[0]

    sleep = result[
        result["metric_id"] == 2
    ].iloc[0]

    efficiency = result[
        result["metric_id"] == 3
    ].iloc[0]

    assert_close(
        exercise["performance_ratio"],
        0.75,
        message="Exercise performance ratio.",
    )

    assert_close(
        exercise["performance_points"],
        15,
        message="Exercise performance points.",
    )

    assert_close(
        sleep["performance_ratio"],
        1.0,
        message="Sleep performance ratio.",
    )

    assert_close(
        sleep["performance_points"],
        10,
        message="Sleep performance points.",
    )

    assert_close(
        efficiency["performance_ratio"],
        0.75,
        message="Efficiency performance ratio.",
    )

    assert_close(
        efficiency["performance_points"],
        7.5,
        message="Efficiency performance points.",
    )

    print("✓ Scoring correct")


# ================================================================
# STEP 4 TEST
# ================================================================

def test_step_4_metric_xp(con):

    section("STEP 4 — METRIC XP")

    result = calculate_metric_progression(
        con,
        start_date=TEST_DATE,
        end_date=TEST_DATE,
    )

    print(result.to_string(index=False))

    assert_equal(
        len(result),
        3,
        "Expected XP results for three metrics.",
    )

    exercise = result[
        result["metric_id"] == 1
    ].iloc[0]

    sleep = result[
        result["metric_id"] == 2
    ].iloc[0]

    efficiency = result[
        result["metric_id"] == 3
    ].iloc[0]

    assert_close(
        exercise["xp_amount"],
        15,
        message="Exercise XP.",
    )

    assert_close(
        sleep["xp_amount"],
        15,
        message="Sleep fixed XP.",
    )

    assert_close(
        efficiency["xp_amount"],
        7.5,
        message="Efficiency XP.",
    )

    print("✓ Metric XP correct")


# ================================================================
# STEP 5 TEST
# ================================================================

def test_step_5_ledger_idempotency(con):

    section("STEP 5 — XP LEDGER + IDEMPOTENCY")

    metric_xp = calculate_metric_progression(
        con,
        start_date=TEST_DATE,
        end_date=TEST_DATE,
    )

    # First persistence.
    persisted_first = persist_metric_xp(
        metric_xp,
        con,
    )

    count_first = con.execute(
        """
        SELECT COUNT(*)
        FROM xp_ledger
        """
    ).fetchone()[0]

    print(
        f"First persistence: "
        f"{persisted_first} entries"
    )

    assert_equal(
        count_first,
        3,
        "Expected three ledger entries.",
    )

    # Second persistence.
    persisted_second = persist_metric_xp(
        metric_xp,
        con,
    )

    count_second = con.execute(
        """
        SELECT COUNT(*)
        FROM xp_ledger
        """
    ).fetchone()[0]

    print(
        f"Second persistence: "
        f"{persisted_second} entries"
    )

    # IMPORTANT:
    # Count must remain three.
    assert_equal(
        count_second,
        3,
        "XP ledger duplicated entries.",
    )

    # Check actual XP total.
    total_xp = con.execute(
        """
        SELECT SUM(xp_amount)
        FROM xp_ledger
        """
    ).fetchone()[0]

    assert_close(
        total_xp,
        37.5,
        message="Total ledger XP.",
    )

    print("✓ XP ledger persisted correctly")
    print("✓ Re-running did not create duplicates")


# ================================================================
# STEP 6 TEST
# ================================================================

def test_step_6_category_progression(con):

    section("STEP 6 — CATEGORY XP + LEVEL")

    category_xp = calculate_category_total_xp(
        con
    )

    print(
        "\nCategory totals:"
    )

    print(
        category_xp.to_string(
            index=False
        )
    )

    health = category_xp[
        category_xp["category_id"] == 1
    ].iloc[0]

    assert_close(
        health["total_xp"],
        37.5,
        message="Health total XP.",
    )

    levels = calculate_category_levels(
        con
    )

    print(
        "\nCategory levels:"
    )

    print(
        levels.to_string(
            index=False
        )
    )

    health_level = levels[
        levels["category_id"] == 1
    ].iloc[0]

    assert_equal(
        health_level["current_level"],
        1,
        "Health should still be Level 1.",
    )

    assert_close(
        health_level["current_level_xp"],
        37.5,
        message="Health Level 1 XP.",
    )

    assert_close(
        health_level["next_level_xp"],
        100,
        message="Health XP required for Level 2.",
    )

    assert_close(
        health_level["progress_ratio"],
        0.375,
        message="Health level progress.",
    )

    print("✓ Category XP correct")
    print("✓ Category level correct")


# ================================================================
# Full validation
# ================================================================

def run_validation():

    print()
    print("ZIST LOCAL — CALCULATION PIPELINE VALIDATION")
    print(f"Test date: {TEST_DATE}")

    con = None
    db_path = None

    try:

        # --------------------------------------------------------
        # Create isolated database.
        # --------------------------------------------------------

        con, db_path = create_test_database()

        # --------------------------------------------------------
        # Seed controlled configuration/data.
        # --------------------------------------------------------

        seed_test_data(con)

        # --------------------------------------------------------
        # Run tests.
        # --------------------------------------------------------

        test_step_1_raw_metrics(con)

        test_step_2_derived_metrics(con)

        test_step_3_scoring(con)

        test_step_4_metric_xp(con)

        test_step_5_ledger_idempotency(con)

        test_step_6_category_progression(con)

        # --------------------------------------------------------
        # Final database check.
        # --------------------------------------------------------

        section("FINAL DATABASE CHECK")

        ledger = con.execute(
            """
            SELECT
                xp_entry_id,
                entry_date,
                source_type,
                metric_id,
                category_id,
                performance_points,
                xp_amount
            FROM xp_ledger
            ORDER BY metric_id
            """
        ).df()

        print(
            ledger.to_string(
                index=False
            )
        )

        assert_equal(
            len(ledger),
            3,
            "Final ledger should contain exactly 3 entries.",
        )

        print()
        print("=" * 70)
        print("✓ ALL PIPELINE VALIDATION TESTS PASSED")
        print("=" * 70)
        print()

    except Exception as exc:

        print()
        print("=" * 70)
        print("✗ PIPELINE VALIDATION FAILED")
        print("=" * 70)
        print()
        print(
            f"{type(exc).__name__}: {exc}"
        )
        print()

        raise

    finally:

        if con is not None:
            con.close()

        if db_path and os.path.exists(db_path):
            os.remove(db_path)


if __name__ == "__main__":
    run_validation()