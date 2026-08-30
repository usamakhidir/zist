from __future__ import annotations

from datetime import date

import pandas as pd

from pipeline.db import get_connection
from pipeline.scoring import evaluate_formula
from pipeline.scoring import calculate_score, evaluate_formula
from pipeline.db import (
    get_connection,
    next_id,
)

METRIC_COLUMNS = [
    "activity_date",
    "metric_id",
    "metric_name",
    "value",
    "status",
]


# ============================================================
# Common helpers
# ============================================================

def _is_active(
    activity_date,
    start_date,
    end_date,
) -> bool:
    """Return True when a configuration is active on a date."""

    if start_date is not None and activity_date < start_date:
        return False

    if end_date is not None and activity_date > end_date:
        return False

    return True


def _observation_value(
    numeric_value,
    value_status,
    measurement_type,
):
    """
    Convert an observation into a numeric value.

    Numeric metrics:
        numeric_value -> numeric value

    Binary metrics:
        completed     -> 1
        not_completed -> 0

    Missing/unsupported values:
        None
    """

    if numeric_value is not None:
        return float(numeric_value)

    if str(measurement_type).strip().upper() == "BINARY":
        status = str(value_status or "").strip().lower()

        if status == "completed":
            return 1.0

        if status == "not_completed":
            return 0.0

    return None


# ============================================================
# STEP 1
# Observation -> RAW Metric
# ============================================================

def calculate_raw_metric_values(
    con=None,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> pd.DataFrame:
    """
    Calculate daily values for RAW metrics.

    Metric value:

        SUM(observation_value * mapping_weight)

    Missing observations are NOT treated as zero.
    """

    owns_connection = con is None

    if owns_connection:
        con = get_connection()

    try:
        query = """
            SELECT
                m.metric_id,
                m.name AS metric_name,
                m.measurement_type,
                m.start_date AS metric_start_date,
                m.end_date AS metric_end_date,

                mhm.mapping_id,
                mhm.habit_id,
                mhm.weight,
                mhm.start_date AS mapping_start_date,
                mhm.end_date AS mapping_end_date,

                o.activity_date,
                o.numeric_value,
                o.value_status

            FROM metrics m

            INNER JOIN metric_habit_mapping mhm
                ON mhm.metric_id = m.metric_id

            INNER JOIN observations o
                ON o.habit_id = mhm.habit_id

            WHERE UPPER(m.metric_kind) = 'RAW'
        """

        params = []

        if start_date is not None:
            query += """
                AND o.activity_date >= ?
            """
            params.append(start_date)

        if end_date is not None:
            query += """
                AND o.activity_date <= ?
            """
            params.append(end_date)

        query += """
            ORDER BY
                o.activity_date,
                m.metric_id,
                mhm.mapping_id
        """

        rows = con.execute(query, params).fetchall()

        columns = [
            "metric_id",
            "metric_name",
            "measurement_type",
            "metric_start_date",
            "metric_end_date",
            "mapping_id",
            "habit_id",
            "weight",
            "mapping_start_date",
            "mapping_end_date",
            "activity_date",
            "numeric_value",
            "value_status",
        ]

        observations = pd.DataFrame(rows, columns=columns)

        if observations.empty:
            return pd.DataFrame(columns=METRIC_COLUMNS)

        contributions = []

        for row in observations.itertuples(index=False):

            if not _is_active(
                row.activity_date,
                row.metric_start_date,
                row.metric_end_date,
            ):
                continue

            if not _is_active(
                row.activity_date,
                row.mapping_start_date,
                row.mapping_end_date,
            ):
                continue

            value = _observation_value(
                row.numeric_value,
                row.value_status,
                row.measurement_type,
            )

            if value is None:
                continue

            weight = (
                1.0
                if row.weight is None
                else float(row.weight)
            )

            contributions.append(
                {
                    "activity_date": row.activity_date,
                    "metric_id": int(row.metric_id),
                    "metric_name": row.metric_name,
                    "contribution": value * weight,
                }
            )

        if not contributions:
            return pd.DataFrame(columns=METRIC_COLUMNS)

        contribution_df = pd.DataFrame(contributions)

        result = (
            contribution_df
            .groupby(
                [
                    "activity_date",
                    "metric_id",
                    "metric_name",
                ],
                as_index=False,
            )["contribution"]
            .sum()
            .rename(
                columns={
                    "contribution": "value",
                }
            )
        )

        result["status"] = "CALCULATED"

        return (
            result[METRIC_COLUMNS]
            .sort_values(
                [
                    "activity_date",
                    "metric_id",
                ]
            )
            .reset_index(drop=True)
        )

    finally:
        if owns_connection:
            con.close()


# ============================================================
# STEP 2
# Derived Metric configuration
# ============================================================

def _load_derived_metric_configuration(con):
    """
    Load all derived metrics and their formula inputs.

    Returns:

        metrics_by_id
        dependencies
        formula_inputs
    """

    metric_rows = con.execute("""
        SELECT
            metric_id,
            name,
            formula,
            start_date,
            end_date
        FROM metrics
        WHERE UPPER(metric_kind) = 'DERIVED'
        ORDER BY metric_id
    """).fetchall()

    metrics_by_id = {}

    for row in metric_rows:
        metric_id = int(row[0])

        metrics_by_id[metric_id] = {
            "metric_id": metric_id,
            "name": row[1],
            "formula": row[2],
            "start_date": row[3],
            "end_date": row[4],
        }

    input_rows = con.execute("""
        SELECT
            formula_input_id,
            derived_metric_id,
            source_metric_id,
            variable_name
        FROM metric_formula_inputs
        ORDER BY
            derived_metric_id,
            formula_input_id
    """).fetchall()

    dependencies = {
        metric_id: []
        for metric_id in metrics_by_id
    }

    formula_inputs = {
        metric_id: {}
        for metric_id in metrics_by_id
    }

    for (
        formula_input_id,
        derived_metric_id,
        source_metric_id,
        variable_name,
    ) in input_rows:

        derived_metric_id = int(derived_metric_id)
        source_metric_id = int(source_metric_id)

        # A formula input referring to a non-existent derived
        # metric is a configuration problem.
        if derived_metric_id not in metrics_by_id:
            raise ValueError(
                "Formula input references unknown derived metric: "
                f"{derived_metric_id}"
            )

        # A derived metric cannot depend on itself.
        if derived_metric_id == source_metric_id:
            raise ValueError(
                "Derived metric cannot depend on itself: "
                f"metric_id={derived_metric_id}"
            )

        variable_name = str(variable_name).strip()

        if not variable_name:
            raise ValueError(
                "Formula input has an empty variable name: "
                f"formula_input_id={formula_input_id}"
            )

        # Every variable must map to exactly one source metric.
        if variable_name in formula_inputs[derived_metric_id]:
            existing_source = formula_inputs[
                derived_metric_id
            ][variable_name]

            raise ValueError(
                "Duplicate formula variable "
                f"'{variable_name}' for derived metric "
                f"{derived_metric_id}. "
                f"Existing source metric: {existing_source}, "
                f"new source metric: {source_metric_id}"
            )

        formula_inputs[derived_metric_id][variable_name] = (
            source_metric_id
        )

        dependencies[derived_metric_id].append(
            source_metric_id
        )

    # Validate that every source metric actually exists.
    source_metric_ids = {
        source_id
        for metric_inputs in formula_inputs.values()
        for source_id in metric_inputs.values()
    }

    if source_metric_ids:
        placeholders = ", ".join(
            ["?"] * len(source_metric_ids)
        )

        existing_source_rows = con.execute(
            f"""
                SELECT metric_id
                FROM metrics
                WHERE metric_id IN ({placeholders})
            """,
            list(source_metric_ids),
        ).fetchall()

        existing_source_ids = {
            int(row[0])
            for row in existing_source_rows
        }

        missing_source_ids = (
            source_metric_ids - existing_source_ids
        )

        if missing_source_ids:
            raise ValueError(
                "Formula inputs reference unknown source "
                f"metric IDs: {sorted(missing_source_ids)}"
            )

    return (
        metrics_by_id,
        dependencies,
        formula_inputs,
    )


# ============================================================
# STEP 2
# Dependency resolution
# ============================================================

def _topological_order(
    derived_metric_ids,
    dependencies,
):
    """
    Return derived metrics in dependency order.

    Example:

        A depends on B
        B depends on C

    result:

        C, B, A

    Raises ValueError for circular dependencies.
    """

    derived_metric_ids = set(
        int(metric_id)
        for metric_id in derived_metric_ids
    )

    graph = {
        metric_id: {
            dependency
            for dependency in dependencies.get(
                metric_id,
                [],
            )
            if dependency in derived_metric_ids
        }
        for metric_id in derived_metric_ids
    }

    order = []

    while graph:

        ready = sorted(
            metric_id
            for metric_id, deps in graph.items()
            if not deps
        )

        if not ready:
            cycle_nodes = sorted(graph.keys())

            raise ValueError(
                "Circular derived metric dependency detected. "
                f"Metric IDs involved: {cycle_nodes}"
            )

        for metric_id in ready:
            order.append(metric_id)
            del graph[metric_id]

        for deps in graph.values():
            deps.difference_update(ready)

    return order


# ============================================================
# STEP 2
# Formula validation
# ============================================================

def _validate_formula_variables(
    metric_id,
    metric_name,
    formula,
    formula_inputs,
):
    """
    Validate that every variable appearing in the formula
    has a Formula Input.

    The actual expression evaluation remains delegated to
    the existing safe evaluate_formula() function.
    """

    # We intentionally use the existing evaluator as the
    # source of truth for formula syntax.

    # For variable validation, evaluate with placeholder
    # values. This allows evaluate_formula() to tell us
    # about unknown variables without executing arbitrary code.
    variables = {
        variable_name: 1.0
        for variable_name in formula_inputs
    }

    try:
        evaluate_formula(
            str(formula),
            variables,
        )
    except ValueError as exc:
        raise ValueError(
            f"Invalid formula configuration for derived "
            f"metric '{metric_name}' "
            f"(metric_id={metric_id}): {exc}"
        ) from exc
    except Exception:
        # Runtime calculation errors such as division by zero
        # are not configuration errors here.
        pass


# ============================================================
# STEP 2
# Derived Metric calculation
# ============================================================

def calculate_derived_metric_values(
    metric_values: pd.DataFrame,
    con=None,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> pd.DataFrame:
    """
    Calculate DERIVED metrics from already-calculated metrics.

    A derived metric:

        1. Has metric_kind = DERIVED
        2. Has a formula
        3. Has formula inputs mapping variables to source metrics

    Derived metrics are calculated in dependency order.

    Missing source metrics cause the derived metric to be
    unavailable for that date.

    No database writes are performed.
    """

    if metric_values is None:
        metric_values = pd.DataFrame(
            columns=METRIC_COLUMNS
        )

    owns_connection = con is None

    if owns_connection:
        con = get_connection()

    try:
        (
            metrics_by_id,
            dependencies,
            formula_inputs,
        ) = _load_derived_metric_configuration(con)

        if not metrics_by_id:
            return pd.DataFrame(
                columns=METRIC_COLUMNS
            )

        calculation_order = _topological_order(
            metrics_by_id.keys(),
            dependencies,
        )

        # Working set of all values available so far.
        #
        # Initially this contains RAW metrics.
        # Each calculated DERIVED metric is then appended
        # so downstream derived metrics can use it.
        values = metric_values.copy()

        if not values.empty:
            values["metric_id"] = (
                values["metric_id"].astype(int)
            )

        all_derived_results = []

        for metric_id in calculation_order:

            metric = metrics_by_id[metric_id]

            metric_name = metric["name"]
            formula = metric["formula"]

            if formula is None or not str(formula).strip():
                raise ValueError(
                    f"Derived metric '{metric_name}' "
                    f"(metric_id={metric_id}) has no formula."
                )

            variable_map = formula_inputs.get(
                metric_id,
                {},
            )

            if not variable_map:
                raise ValueError(
                    f"Derived metric '{metric_name}' "
                    f"(metric_id={metric_id}) has no "
                    "formula inputs."
                )

            # Validate the configured formula before processing
            # daily data.
            _validate_formula_variables(
                metric_id,
                metric_name,
                formula,
                variable_map,
            )

            source_metric_ids = list(
                variable_map.values()
            )

            # Find dates where at least one source metric exists.
            if values.empty:
                continue

            available_source_values = values[
                values["metric_id"].isin(
                    source_metric_ids
                )
            ]

            if available_source_values.empty:
                continue

            dates = sorted(
                available_source_values[
                    "activity_date"
                ].dropna().unique()
            )

            new_results = []

            for activity_date in dates:

                # Global requested range.
                if (
                    start_date is not None
                    and activity_date < start_date
                ):
                    continue

                if (
                    end_date is not None
                    and activity_date > end_date
                ):
                    continue

                # Metric's own effective dates.
                if not _is_active(
                    activity_date,
                    metric["start_date"],
                    metric["end_date"],
                ):
                    continue

                variables = {}
                missing_source = False

                for (
                    variable_name,
                    source_metric_id,
                ) in variable_map.items():

                    source_rows = values[
                        (
                            values["metric_id"]
                            == source_metric_id
                        )
                        & (
                            values["activity_date"]
                            == activity_date
                        )
                    ]

                    if source_rows.empty:
                        missing_source = True
                        break

                    source_value = (
                        source_rows.iloc[-1]["value"]
                    )

                    if pd.isna(source_value):
                        missing_source = True
                        break

                    variables[variable_name] = float(
                        source_value
                    )

                # Missing source data means this derived metric
                # cannot be calculated for this date.
                if missing_source:
                    continue

                try:
                    derived_value = evaluate_formula(
                        str(formula),
                        variables,
                    )
                except Exception as exc:
                    raise ValueError(
                        f"Could not calculate derived metric "
                        f"'{metric_name}' "
                        f"(metric_id={metric_id}) "
                        f"on {activity_date}: {exc}"
                    ) from exc

                if derived_value is None:
                    continue

                result_row = {
                    "activity_date": activity_date,
                    "metric_id": metric_id,
                    "metric_name": metric_name,
                    "value": float(derived_value),
                    "status": "CALCULATED",
                }

                new_results.append(result_row)
                all_derived_results.append(result_row)

            # Make this derived metric available immediately
            # to later metrics in the dependency chain.
            if new_results:
                values = pd.concat(
                    [
                        values,
                        pd.DataFrame(new_results),
                    ],
                    ignore_index=True,
                )

        if not all_derived_results:
            return pd.DataFrame(
                columns=METRIC_COLUMNS
            )

        return (
            pd.DataFrame(all_derived_results)
            .drop_duplicates(
                subset=[
                    "activity_date",
                    "metric_id",
                ],
                keep="last",
            )
            .sort_values(
                [
                    "activity_date",
                    "metric_id",
                ]
            )
            .reset_index(drop=True)
        )

    finally:
        if owns_connection:
            con.close()


# ============================================================
# COMPLETE METRIC PIPELINE
# ============================================================

def calculate_metric_values(
    con=None,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> pd.DataFrame:
    """
    Run Steps 1 + 2:

        Observations
            ↓
        RAW Metrics
            ↓
        Derived Metrics

    Returns all calculated Metric values.
    """

    owns_connection = con is None

    if owns_connection:
        con = get_connection()

    try:
        # ------------------------------------------------------------
        # STEP 1
        # ------------------------------------------------------------

        raw_values = calculate_raw_metric_values(
            con,
            start_date=start_date,
            end_date=end_date,
        )

        # ------------------------------------------------------------
        # STEP 2
        # ------------------------------------------------------------

        derived_values = calculate_derived_metric_values(
            raw_values,
            con,
            start_date=start_date,
            end_date=end_date,
        )

        # Nothing calculated.
        if (
            raw_values.empty
            and derived_values.empty
        ):
            return pd.DataFrame(
                columns=METRIC_COLUMNS
            )

        # Combine RAW + DERIVED metrics.
        return (
            pd.concat(
                [
                    raw_values,
                    derived_values,
                ],
                ignore_index=True,
            )
            .sort_values(
                [
                    "activity_date",
                    "metric_id",
                ]
            )
            .reset_index(drop=True)
        )

    finally:
        if owns_connection:
            con.close()

def calculate_scored_metrics(
    con=None,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> pd.DataFrame:
    """
    Run Steps 1-3:

        Observations
            ↓
        RAW Metrics
            ↓
        Derived Metrics
            ↓
        Scoring

    Returns the complete scored Metric dataset.
    """

    owns_connection = con is None

    if owns_connection:
        con = get_connection()

    try:
        # ------------------------------------------------------------
        # STEP 1 + STEP 2
        # ------------------------------------------------------------

        metric_values = calculate_metric_values(
            con,
            start_date=start_date,
            end_date=end_date,
        )

        # ------------------------------------------------------------
        # STEP 3
        # ------------------------------------------------------------

        return calculate_metric_scores(
            metric_values,
            con,
            start_date=start_date,
            end_date=end_date,
        )

    finally:
        if owns_connection:
            con.close()

def calculate_metric_progression(
    con=None,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> pd.DataFrame:
    """
    Run Steps 1-4:

        Observations
            ↓
        RAW Metrics
            ↓
        Derived Metrics
            ↓
        Scoring
            ↓
        Metric XP
    """

    owns_connection = con is None

    if owns_connection:
        con = get_connection()

    try:

        # ------------------------------------------------------------
        # STEP 1 + STEP 2 + STEP 3
        # ------------------------------------------------------------

        scored_metrics = calculate_scored_metrics(
            con,
            start_date=start_date,
            end_date=end_date,
        )

        # ------------------------------------------------------------
        # STEP 4
        # ------------------------------------------------------------

        return calculate_metric_xp(
            scored_metrics,
            con,
        )

    finally:
        if owns_connection:
            con.close()

# ============================================================
# STEP 3
# Scoring
# ============================================================

def _load_scoring_rules(con):
    """
    Load scoring rules.

    A metric may have multiple historical scoring rules,
    but only the rule active on the activity date should
    be applied.
    """

    rows = con.execute("""
        SELECT
            rule_id,
            metric_id,
            scoring_method,
            target_value,
            min_value,
            max_value,
            rating_max,
            max_points,
            start_date,
            end_date
        FROM scoring_rules
        ORDER BY
            metric_id,
            start_date,
            rule_id
    """).fetchall()

    rules_by_metric = {}

    for row in rows:
        (
            rule_id,
            metric_id,
            scoring_method,
            target_value,
            min_value,
            max_value,
            rating_max,
            max_points,
            rule_start_date,
            rule_end_date,
        ) = row

        metric_id = int(metric_id)

        rule = {
            "rule_id": int(rule_id),
            "metric_id": metric_id,
            "scoring_method": str(
                scoring_method
            ).strip().upper(),
            "target_value": (
                float(target_value)
                if target_value is not None
                else None
            ),
            "min_value": (
                float(min_value)
                if min_value is not None
                else None
            ),
            "max_value": (
                float(max_value)
                if max_value is not None
                else None
            ),
            "rating_max": (
                float(rating_max)
                if rating_max is not None
                else None
            ),
            "max_points": (
                float(max_points)
                if max_points is not None
                else None
            ),
            "start_date": rule_start_date,
            "end_date": rule_end_date,
        }

        rules_by_metric.setdefault(
            metric_id,
            [],
        ).append(rule)

    return rules_by_metric


def _get_active_scoring_rule(
    metric_id,
    activity_date,
    rules,
):
    """
    Return the single scoring rule active for a metric
    on a particular date.

    Raises ValueError if more than one rule is active.
    """

    active_rules = [
        rule
        for rule in rules
        if _is_active(
            activity_date,
            rule["start_date"],
            rule["end_date"],
        )
    ]

    if len(active_rules) > 1:
        rule_ids = [
            rule["rule_id"]
            for rule in active_rules
        ]

        raise ValueError(
            "Multiple scoring rules are active for "
            f"metric_id={metric_id} on "
            f"{activity_date}. "
            f"Active rule IDs: {rule_ids}"
        )

    if not active_rules:
        return None

    return active_rules[0]


def calculate_metric_scores(
    metric_values: pd.DataFrame,
    con=None,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> pd.DataFrame:
    """
    Step 3:
    Convert calculated Metric values into performance scores.

    Input:
        metric_values produced by Steps 1 + 2.

    Output columns:

        activity_date
        metric_id
        metric_name
        value
        scoring_method
        performance_ratio
        performance_points
        max_points
        rule_id
        status

    The actual scoring mathematics are delegated to
    pipeline.scoring.calculate_score().
    """

    score_columns = [
        "activity_date",
        "metric_id",
        "metric_name",
        "value",
        "scoring_method",
        "performance_ratio",
        "performance_points",
        "max_points",
        "rule_id",
        "status",
    ]

    if metric_values is None or metric_values.empty:
        return pd.DataFrame(
            columns=score_columns
        )

    owns_connection = con is None

    if owns_connection:
        con = get_connection()

    try:
        rules_by_metric = _load_scoring_rules(con)

        results = []

        for row in metric_values.itertuples(index=False):

            activity_date = row.activity_date
            metric_id = int(row.metric_id)

            # --------------------------------------------------------
            # Respect requested calculation window.
            # --------------------------------------------------------

            if (
                start_date is not None
                and activity_date < start_date
            ):
                continue

            if (
                end_date is not None
                and activity_date > end_date
            ):
                continue

            # --------------------------------------------------------
            # Find scoring rule active on this date.
            # --------------------------------------------------------

            metric_rules = rules_by_metric.get(
                metric_id,
                [],
            )

            rule = _get_active_scoring_rule(
                metric_id,
                activity_date,
                metric_rules,
            )

            # --------------------------------------------------------
            # No active rule:
            #
            # The Metric itself is valid, but it cannot be scored.
            # --------------------------------------------------------

            if rule is None:

                results.append(
                    {
                        "activity_date": activity_date,
                        "metric_id": metric_id,
                        "metric_name": row.metric_name,
                        "value": float(row.value),
                        "scoring_method": None,
                        "performance_ratio": None,
                        "performance_points": None,
                        "max_points": None,
                        "rule_id": None,
                        "status": "UNSCORED",
                    }
                )

                continue

            scoring_method = rule[
                "scoring_method"
            ]

            max_points = rule[
                "max_points"
            ]

            # --------------------------------------------------------
            # Validate max_points.
            # --------------------------------------------------------

            if max_points is None:
                raise ValueError(
                    f"Scoring rule {rule['rule_id']} "
                    f"for metric '{row.metric_name}' "
                    "does not define max_points."
                )

            max_points = float(max_points)

            if max_points < 0:
                raise ValueError(
                    f"Scoring rule {rule['rule_id']} "
                    "has negative max_points."
                )

            # --------------------------------------------------------
            # Call the existing scoring engine.
            #
            # IMPORTANT:
            #
            # calculate_score() returns:
            #
            #     achievement_pct
            #     points
            #     status
            #
            # It does NOT return performance_ratio directly.
            # --------------------------------------------------------

            try:

                score_result = calculate_score(
                    method=scoring_method,
                    actual=float(row.value),
                    target=rule["target_value"],
                    min_value=rule["min_value"],
                    max_value=rule["max_value"],
                    rating_max=rule["rating_max"],
                    max_points=max_points,
                )

            except Exception as exc:

                raise ValueError(
                    f"Could not score metric "
                    f"'{row.metric_name}' "
                    f"(metric_id={metric_id}) "
                    f"on {activity_date} "
                    f"using scoring rule "
                    f"{rule['rule_id']}: {exc}"
                ) from exc

            # --------------------------------------------------------
            # Validate result from scoring engine.
            # --------------------------------------------------------

            if not isinstance(score_result, dict):
                raise ValueError(
                    f"calculate_score() returned an "
                    f"unexpected result for metric "
                    f"'{row.metric_name}' "
                    f"(metric_id={metric_id}): "
                    f"{score_result!r}"
                )

            achievement_pct = score_result.get(
                "achievement_pct"
            )

            performance_points = score_result.get(
                "points"
            )

            score_status = score_result.get(
                "status"
            )

            # --------------------------------------------------------
            # No achievement percentage means no score.
            # --------------------------------------------------------

            if achievement_pct is None:

                results.append(
                    {
                        "activity_date": activity_date,
                        "metric_id": metric_id,
                        "metric_name": row.metric_name,
                        "value": float(row.value),
                        "scoring_method": scoring_method,
                        "performance_ratio": None,
                        "performance_points": (
                            performance_points
                        ),
                        "max_points": max_points,
                        "rule_id": rule["rule_id"],
                        "status": (
                            "UNSCORED"
                            if not score_status
                            else str(score_status).upper()
                        ),
                    }
                )

                continue

            # --------------------------------------------------------
            # Convert percentage into normalized ratio.
            #
            # Example:
            #
            # achievement_pct = 75
            #
            # performance_ratio = 0.75
            # --------------------------------------------------------

            performance_ratio = (
                float(achievement_pct) / 100.0
            )

            # --------------------------------------------------------
            # Protect downstream XP from negative ratios.
            #
            # Do NOT silently cap achievement at 100% here.
            #
            # calculate_score() is responsible for scoring behavior.
            # The ratio is only normalized here.
            # --------------------------------------------------------

            performance_ratio = max(
                0.0,
                performance_ratio,
            )

            # --------------------------------------------------------
            # Normalize points.
            # --------------------------------------------------------

            if performance_points is not None:
                performance_points = float(
                    performance_points
                )

            results.append(
                {
                    "activity_date": activity_date,
                    "metric_id": metric_id,
                    "metric_name": row.metric_name,
                    "value": float(row.value),
                    "scoring_method": scoring_method,
                    "performance_ratio": performance_ratio,
                    "performance_points": (
                        performance_points
                    ),
                    "max_points": max_points,
                    "rule_id": rule["rule_id"],
                    "status": "SCORED",
                }
            )

        if not results:
            return pd.DataFrame(
                columns=score_columns
            )

        return (
            pd.DataFrame(results)
            .sort_values(
                [
                    "activity_date",
                    "metric_id",
                ]
            )
            .reset_index(drop=True)
        )

    finally:

        if owns_connection:
            con.close()
# ============================================================
# STEP 4
# Metric XP
# ============================================================

def _load_metric_xp_config(con):
    """
    Load Metric-level XP configuration.

    Expected configuration:

        metric_id
        base_xp
        xp_method
        multiplier
        daily_cap
    """

    rows = con.execute("""
        SELECT
            metric_id,
            base_xp,
            xp_method,
            multiplier,
            daily_cap
        FROM metric_xp_config
        ORDER BY metric_id
    """).fetchall()

    config_by_metric = {}

    for row in rows:
        (
            metric_id,
            base_xp,
            xp_method,
            multiplier,
            daily_cap,
        ) = row

        metric_id = int(metric_id)

        if metric_id in config_by_metric:
            raise ValueError(
                "Multiple Metric XP configurations found "
                f"for metric_id={metric_id}"
            )

        if base_xp is None:
            raise ValueError(
                f"Metric XP configuration for metric_id="
                f"{metric_id} has no base_xp."
            )

        if multiplier is None:
            multiplier = 1.0

        if float(base_xp) < 0:
            raise ValueError(
                f"Metric XP base_xp cannot be negative "
                f"for metric_id={metric_id}."
            )

        if float(multiplier) < 0:
            raise ValueError(
                f"Metric XP multiplier cannot be negative "
                f"for metric_id={metric_id}."
            )

        if (
            daily_cap is not None
            and float(daily_cap) < 0
        ):
            raise ValueError(
                f"Metric XP daily_cap cannot be negative "
                f"for metric_id={metric_id}."
            )

        config_by_metric[metric_id] = {
            "metric_id": metric_id,
            "base_xp": float(base_xp),
            "xp_method": str(
                xp_method
            ).strip().upper(),
            "multiplier": float(multiplier),
            "daily_cap": (
                float(daily_cap)
                if daily_cap is not None
                else None
            ),
        }

    return config_by_metric


def calculate_metric_xp(
    scored_metrics: pd.DataFrame,
    con=None,
) -> pd.DataFrame:
    """
    Step 4:
    Convert Metric performance into Metric XP.

    Input:

        performance_ratio
        performance_points
        Metric XP configuration

    Output:

        activity_date
        metric_id
        metric_name
        performance_ratio
        performance_points
        base_xp
        xp_method
        multiplier
        raw_xp
        daily_cap
        xp_amount
        status

    XP methods:

        PROPORTIONAL
            base_xp
            × performance_ratio
            × multiplier

        FIXED
            base_xp
            × multiplier

    Daily cap is applied after calculating raw XP.
    """

    xp_columns = [
        "activity_date",
        "metric_id",
        "metric_name",
        "performance_ratio",
        "performance_points",
        "base_xp",
        "xp_method",
        "multiplier",
        "raw_xp",
        "daily_cap",
        "xp_amount",
        "status",
    ]

    if scored_metrics is None:
        return pd.DataFrame(
            columns=xp_columns
        )

    if scored_metrics.empty:
        return pd.DataFrame(
            columns=xp_columns
        )

    owns_connection = con is None

    if owns_connection:
        con = get_connection()

    try:
        config_by_metric = _load_metric_xp_config(
            con
        )

        results = []

        for row in scored_metrics.itertuples(
            index=False
        ):

            metric_id = int(row.metric_id)

            # --------------------------------------------------------
            # Metric must have an XP configuration.
            # --------------------------------------------------------

            config = config_by_metric.get(
                metric_id
            )

            if config is None:
                results.append(
                    {
                        "activity_date": row.activity_date,
                        "metric_id": metric_id,
                        "metric_name": row.metric_name,
                        "performance_ratio": (
                            row.performance_ratio
                        ),
                        "performance_points": (
                            row.performance_points
                        ),
                        "base_xp": None,
                        "xp_method": None,
                        "multiplier": None,
                        "raw_xp": None,
                        "daily_cap": None,
                        "xp_amount": None,
                        "status": "NO_XP_CONFIG",
                    }
                )

                continue

            # --------------------------------------------------------
            # Only successfully scored Metrics generate XP.
            # --------------------------------------------------------

            if row.status != "SCORED":

                results.append(
                    {
                        "activity_date": row.activity_date,
                        "metric_id": metric_id,
                        "metric_name": row.metric_name,
                        "performance_ratio": (
                            row.performance_ratio
                        ),
                        "performance_points": (
                            row.performance_points
                        ),
                        "base_xp": config["base_xp"],
                        "xp_method": config["xp_method"],
                        "multiplier": config["multiplier"],
                        "raw_xp": None,
                        "daily_cap": config["daily_cap"],
                        "xp_amount": None,
                        "status": "NO_XP",
                    }
                )

                continue

            if row.performance_ratio is None:
                results.append(
                    {
                        "activity_date": row.activity_date,
                        "metric_id": metric_id,
                        "metric_name": row.metric_name,
                        "performance_ratio": None,
                        "performance_points": (
                            row.performance_points
                        ),
                        "base_xp": config["base_xp"],
                        "xp_method": config["xp_method"],
                        "multiplier": config["multiplier"],
                        "raw_xp": None,
                        "daily_cap": config["daily_cap"],
                        "xp_amount": None,
                        "status": "NO_XP",
                    }
                )

                continue

            performance_ratio = float(
                row.performance_ratio
            )

            base_xp = config["base_xp"]
            multiplier = config["multiplier"]
            xp_method = config["xp_method"]
            daily_cap = config["daily_cap"]

            # --------------------------------------------------------
            # Validate XP method.
            # --------------------------------------------------------

            if xp_method not in {
                "PROPORTIONAL",
                "FIXED",
            }:
                raise ValueError(
                    f"Unsupported XP method "
                    f"'{xp_method}' for metric "
                    f"'{row.metric_name}' "
                    f"(metric_id={metric_id}). "
                    "Expected PROPORTIONAL or FIXED."
                )

            # --------------------------------------------------------
            # Performance ratio is already normalized by Step 3.
            #
            # We still protect this layer against invalid values
            # because XP must never become negative or exceed the
            # configured reward unintentionally.
            # --------------------------------------------------------

            performance_ratio = max(
                0.0,
                min(
                    1.0,
                    performance_ratio,
                ),
            )

            # --------------------------------------------------------
            # Calculate raw XP.
            # --------------------------------------------------------

            if xp_method == "PROPORTIONAL":

                raw_xp = (
                    base_xp
                    * performance_ratio
                    * multiplier
                )

            else:  # FIXED

                # FIXED XP is awarded when the Metric has a
                # successful scoring result.
                raw_xp = (
                    base_xp
                    * multiplier
                )

            # --------------------------------------------------------
            # XP must never be negative.
            # --------------------------------------------------------

            raw_xp = max(
                0.0,
                float(raw_xp),
            )

            # --------------------------------------------------------
            # Apply daily cap after calculating raw XP.
            # --------------------------------------------------------

            if daily_cap is not None:

                xp_amount = min(
                    raw_xp,
                    daily_cap,
                )

            else:

                xp_amount = raw_xp

            results.append(
                {
                    "activity_date": row.activity_date,
                    "metric_id": metric_id,
                    "metric_name": row.metric_name,
                    "performance_ratio": performance_ratio,
                    "performance_points": (
                        row.performance_points
                    ),
                    "base_xp": base_xp,
                    "xp_method": xp_method,
                    "multiplier": multiplier,
                    "raw_xp": raw_xp,
                    "daily_cap": daily_cap,
                    "xp_amount": xp_amount,
                    "status": "XP_CALCULATED",
                }
            )

        if not results:
            return pd.DataFrame(
                columns=xp_columns
            )

        return (
            pd.DataFrame(results)
            .sort_values(
                [
                    "activity_date",
                    "metric_id",
                ]
            )
            .reset_index(drop=True)
        )

    finally:
        if owns_connection:
            con.close()

# ============================================================
# STEP 5
# XP Ledger Persistence
# ============================================================

def persist_metric_xp(
    metric_xp: pd.DataFrame,
    con=None,
) -> int:
    """
    Step 5:
    Persist calculated Metric XP into xp_ledger.

    Logical identity of a Metric XP event:

        entry_date
        + source_type
        + metric_id

    Re-running the same calculation therefore replaces
    the previous calculated entry instead of creating a
    duplicate.

    Returns:
        Number of XP entries persisted.
    """

    if metric_xp is None or metric_xp.empty:
        return 0

    owns_connection = con is None

    if owns_connection:
        con = get_connection()

    try:

        persisted = 0

        for row in metric_xp.itertuples(
            index=False
        ):

            # --------------------------------------------------------
            # Only successfully calculated XP is persisted.
            # --------------------------------------------------------

            if row.status != "XP_CALCULATED":
                continue

            if row.xp_amount is None:
                continue

            xp_amount = float(
                row.xp_amount
            )

            # Zero XP is not an XP award, so don't create
            # a ledger event for it.
            if xp_amount <= 0:
                continue

            metric_id = int(
                row.metric_id
            )

            activity_date = (
                row.activity_date
            )

            source_type = (
                "METRIC_PERFORMANCE"
            )

            # --------------------------------------------------------
            # Find Category for this Metric.
            # --------------------------------------------------------

            category_row = con.execute(
                """
                SELECT
                    category_id
                FROM metrics
                WHERE metric_id = ?
                """,
                [metric_id],
            ).fetchone()

            if category_row is None:
                raise ValueError(
                    "Cannot persist Metric XP: "
                    f"metric_id={metric_id} "
                    "does not exist."
                )

            category_id = category_row[0]

            if category_id is None:
                raise ValueError(
                    "Cannot persist Metric XP: "
                    f"metric_id={metric_id} "
                    "does not have a category_id."
                )

            category_id = int(
                category_id
            )

            # --------------------------------------------------------
            # Determine the logical event.
            #
            # We first delete any previous calculation for this
            # Metric/date/source combination.
            #
            # This makes the operation idempotent.
            # --------------------------------------------------------

            con.execute(
                """
                DELETE FROM xp_ledger
                WHERE entry_date = ?
                  AND source_type = ?
                  AND metric_id = ?
                """,
                [
                    activity_date,
                    source_type,
                    metric_id,
                ],
            )

            # --------------------------------------------------------
            # Generate the required XP ledger primary key.
            #
            # xp_entry_id is NOT nullable in the schema.
            # --------------------------------------------------------

            xp_entry_id = next_id(
                con,
                "xp_ledger",
                "xp_entry_id",
            )

            # --------------------------------------------------------
            # Prepare ledger values.
            # --------------------------------------------------------

            performance_points = (
                row.performance_points
            )

            if performance_points is None:
                performance_points = 0.0
            else:
                performance_points = float(
                    performance_points
                )

            description = (
                f"{row.metric_name} "
                f"performance XP"
            )

            # --------------------------------------------------------
            # Insert current calculation.
            # --------------------------------------------------------

            con.execute(
                """
                INSERT INTO xp_ledger (
                    xp_entry_id,
                    entry_date,
                    source_type,
                    source_id,
                    metric_id,
                    category_id,
                    performance_points,
                    xp_amount,
                    description
                )
                VALUES (
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?
                )
                """,
                [
                    xp_entry_id,
                    activity_date,
                    source_type,
                    metric_id,
                    metric_id,
                    category_id,
                    performance_points,
                    xp_amount,
                    description,
                ],
            )

            persisted += 1

        return persisted

    finally:

        if owns_connection:
            con.close()

def calculate_and_persist_metric_xp(
    con=None,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> pd.DataFrame:
    """
    Run the complete Metric progression pipeline:

        Observations
            ↓
        RAW Metrics
            ↓
        Derived Metrics
            ↓
        Scoring
            ↓
        Metric XP
            ↓
        XP Ledger

    Returns the calculated Metric XP DataFrame.
    """

    owns_connection = con is None

    if owns_connection:
        con = get_connection()

    try:

        metric_xp = calculate_metric_progression(
            con,
            start_date=start_date,
            end_date=end_date,
        )

        persist_metric_xp(
            metric_xp,
            con,
        )

        return metric_xp

    finally:
        if owns_connection:
            con.close()

# ============================================================
# STEP 6A
# Category XP
# ============================================================

def calculate_category_xp(
    con=None,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> pd.DataFrame:
    """
    Aggregate Metric XP into Category XP.

    Source of truth:
        xp_ledger

    Only positive Metric XP awards are aggregated.

    Returns:

        activity_date
        category_id
        category_name
        xp_earned
    """

    category_columns = [
        "activity_date",
        "category_id",
        "category_name",
        "xp_earned",
    ]

    owns_connection = con is None

    if owns_connection:
        con = get_connection()

    try:

        query = """
            SELECT
                x.entry_date AS activity_date,
                x.category_id,
                c.name AS category_name,
                SUM(x.xp_amount) AS xp_earned

            FROM xp_ledger x

            INNER JOIN categories c
                ON c.category_id = x.category_id

            WHERE x.source_type = 'METRIC_PERFORMANCE'
              AND x.xp_amount > 0
        """

        params = []

        if start_date is not None:
            query += """
                AND x.entry_date >= ?
            """
            params.append(start_date)

        if end_date is not None:
            query += """
                AND x.entry_date <= ?
            """
            params.append(end_date)

        query += """
            GROUP BY
                x.entry_date,
                x.category_id,
                c.name

            ORDER BY
                x.entry_date,
                x.category_id
        """

        rows = con.execute(
            query,
            params,
        ).fetchall()

        if not rows:
            return pd.DataFrame(
                columns=category_columns
            )

        return pd.DataFrame(
            rows,
            columns=category_columns,
        )

    finally:
        if owns_connection:
            con.close()

def calculate_category_total_xp(
    con=None,
) -> pd.DataFrame:
    """
    Calculate accumulated Metric XP for every Category.

    Categories with no XP are returned with total_xp = 0.
    """

    columns = [
        "category_id",
        "category_name",
        "total_xp",
    ]

    owns_connection = con is None

    if owns_connection:
        con = get_connection()

    try:

        rows = con.execute("""
            SELECT
                c.category_id,
                c.name AS category_name,

                COALESCE(
                    SUM(
                        CASE
                            WHEN x.source_type =
                                'METRIC_PERFORMANCE'
                            THEN x.xp_amount
                            ELSE 0
                        END
                    ),
                    0
                ) AS total_xp

            FROM categories c

            LEFT JOIN xp_ledger x
                ON x.category_id = c.category_id

            GROUP BY
                c.category_id,
                c.name

            ORDER BY
                c.category_id
        """).fetchall()

        if not rows:
            return pd.DataFrame(
                columns=columns
            )

        return pd.DataFrame(
            rows,
            columns=columns,
        )

    finally:
        if owns_connection:
            con.close()

# ============================================================
# STEP 6B
# Category Level
# ============================================================

def _level_progression_requirement(
    level_transition: int,
    progression_method: str,
    base_xp: float,
    growth_rate: float | None,
) -> float:
    """
    Return the XP required for one level transition.

    level_transition:
        1 = Level 1 -> Level 2
        2 = Level 2 -> Level 3
        ...

    Progression:

        LINEAR:
            base_xp

        INCREASING:
            base_xp * level_transition

        EXPONENTIAL:
            base_xp * growth_rate^(level_transition - 1)
    """

    if level_transition < 1:
        raise ValueError(
            "level_transition must be >= 1."
        )

    method = str(
        progression_method
    ).strip().upper()

    if base_xp < 0:
        raise ValueError(
            "base_xp cannot be negative."
        )

    if method == "LINEAR":

        return float(base_xp)

    if method == "INCREASING":

        return float(
            base_xp * level_transition
        )

    if method == "EXPONENTIAL":

        if growth_rate is None:
            raise ValueError(
                "growth_rate is required for "
                "EXPONENTIAL progression."
            )

        if growth_rate <= 0:
            raise ValueError(
                "growth_rate must be greater than "
                "0 for EXPONENTIAL progression."
            )

        return float(
            base_xp
            * (
                growth_rate
                ** (level_transition - 1)
            )
        )

    raise ValueError(
        f"Unsupported progression method: "
        f"{progression_method}"
    )


def _calculate_category_level(
    total_xp: float,
    progression_method: str,
    base_xp: float,
    growth_rate: float | None,
):
    """
    Convert total Category XP into:

        current_level
        current_level_xp
        next_level_xp
        xp_to_next_level
        progress_ratio
    """

    total_xp = max(
        0.0,
        float(total_xp),
    )

    # ------------------------------------------------------------
    # Level 1 starts at 0 XP.
    # ------------------------------------------------------------

    current_level = 1

    xp_remaining = total_xp

    while True:

        transition = current_level

        required = _level_progression_requirement(
            transition,
            progression_method,
            base_xp,
            growth_rate,
        )

        if xp_remaining < required:
            break

        xp_remaining -= required
        current_level += 1

    # XP required to move from current level to next.
    next_level_xp = (
        _level_progression_requirement(
            current_level,
            progression_method,
            base_xp,
            growth_rate,
        )
    )

    current_level_xp = xp_remaining

    xp_to_next_level = max(
        0.0,
        next_level_xp - current_level_xp,
    )

    if next_level_xp > 0:
        progress_ratio = (
            current_level_xp
            / next_level_xp
        )
    else:
        progress_ratio = 0.0

    return {
        "current_level": current_level,
        "current_level_xp": current_level_xp,
        "next_level_xp": next_level_xp,
        "xp_to_next_level": xp_to_next_level,
        "progress_ratio": progress_ratio,
    }

def _load_category_xp_config(con):
    """
    Load Category progression configuration.
    """

    rows = con.execute("""
        SELECT
            category_id,
            progression_method,
            base_xp,
            growth_rate
        FROM category_xp_config
        ORDER BY category_id
    """).fetchall()

    config = {}

    for row in rows:

        (
            category_id,
            progression_method,
            base_xp,
            growth_rate,
        ) = row

        category_id = int(category_id)

        if category_id in config:
            raise ValueError(
                "Multiple Category XP configurations "
                f"found for category_id={category_id}"
            )

        if base_xp is None:
            raise ValueError(
                f"Category {category_id} has no base_xp."
            )

        config[category_id] = {
            "category_id": category_id,
            "progression_method": str(
                progression_method
            ).strip().upper(),
            "base_xp": float(base_xp),
            "growth_rate": (
                float(growth_rate)
                if growth_rate is not None
                else None
            ),
        }

    return config

def calculate_category_levels(
    con=None,
) -> pd.DataFrame:
    """
    Calculate current Category progression.

    Source of truth:

        xp_ledger
            ↓
        accumulated Category XP
            ↓
        Category progression configuration
            ↓
        Category Level

    Returns:

        category_id
        category_name
        total_xp
        progression_method
        base_xp
        growth_rate
        current_level
        current_level_xp
        next_level_xp
        xp_to_next_level
        progress_ratio
    """

    columns = [
        "category_id",
        "category_name",
        "total_xp",
        "progression_method",
        "base_xp",
        "growth_rate",
        "current_level",
        "current_level_xp",
        "next_level_xp",
        "xp_to_next_level",
        "progress_ratio",
    ]

    owns_connection = con is None

    if owns_connection:
        con = get_connection()

    try:

        category_xp = (
            calculate_category_total_xp(
                con
            )
        )

        if category_xp.empty:
            return pd.DataFrame(
                columns=columns
            )

        configs = _load_category_xp_config(
            con
        )

        results = []

        for row in category_xp.itertuples(
            index=False
        ):

            category_id = int(
                row.category_id
            )

            config = configs.get(
                category_id
            )

            # --------------------------------------------------------
            # A Category without progression configuration cannot
            # have a calculated level.
            # --------------------------------------------------------

            if config is None:
                results.append(
                    {
                        "category_id": category_id,
                        "category_name": row.category_name,
                        "total_xp": float(
                            row.total_xp
                        ),
                        "progression_method": None,
                        "base_xp": None,
                        "growth_rate": None,
                        "current_level": None,
                        "current_level_xp": None,
                        "next_level_xp": None,
                        "xp_to_next_level": None,
                        "progress_ratio": None,
                    }
                )

                continue

            level = _calculate_category_level(
                total_xp=float(
                    row.total_xp
                ),
                progression_method=(
                    config["progression_method"]
                ),
                base_xp=config["base_xp"],
                growth_rate=config["growth_rate"],
            )

            results.append(
                {
                    "category_id": category_id,
                    "category_name": row.category_name,
                    "total_xp": float(
                        row.total_xp
                    ),
                    "progression_method": (
                        config["progression_method"]
                    ),
                    "base_xp": config["base_xp"],
                    "growth_rate": config[
                        "growth_rate"
                    ],
                    "current_level": (
                        level["current_level"]
                    ),
                    "current_level_xp": (
                        level["current_level_xp"]
                    ),
                    "next_level_xp": (
                        level["next_level_xp"]
                    ),
                    "xp_to_next_level": (
                        level["xp_to_next_level"]
                    ),
                    "progress_ratio": (
                        level["progress_ratio"]
                    ),
                }
            )

        return (
            pd.DataFrame(results)
            .sort_values(
                "category_id"
            )
            .reset_index(drop=True)
        )

    finally:
        if owns_connection:
            con.close()


def calculate_category_progression(
    con=None,
) -> pd.DataFrame:
    """
    Complete Step 6:

        XP Ledger
            ↓
        Category XP
            ↓
        Category Level
    """

    owns_connection = con is None

    if owns_connection:
        con = get_connection()

    try:
        return calculate_category_levels(
            con
        )

    finally:
        if owns_connection:
            con.close()