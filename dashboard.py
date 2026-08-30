
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from pipeline.calculation import (
    calculate_and_persist_metric_xp,
    calculate_category_levels,
)


# ============================================================
# Dashboard data
# ============================================================

def get_dashboard_data(
    start_date: date,
    end_date: date,
):
    """
    Calculate the production dashboard data for a selected
    date range.

    All calculation logic remains inside pipeline.calculation.py.
    This module only prepares data for presentation.
    """

    metric_xp = calculate_and_persist_metric_xp(
        start_date=start_date,
        end_date=end_date,
    )

    category_levels = calculate_category_levels()

    return {
        "metric_xp": metric_xp,
        "category_levels": category_levels,
    }


# ============================================================
# Dashboard summary
# ============================================================

def build_dashboard_summary(
    metric_xp: pd.DataFrame,
    category_levels: pd.DataFrame,
):
    """
    Build the high-level dashboard summary.

    Returns:

        metrics_scored
        xp_awards
        period_xp
        categories
    """

    if metric_xp is None or metric_xp.empty:

        metrics_scored = 0
        xp_awards = 0
        period_xp = 0.0

    else:

        if "status" in metric_xp.columns:

            metrics_scored = int(
                (
                    metric_xp["status"]
                    == "SCORED"
                ).sum()
            )

            xp_awards = int(
                (
                    metric_xp["status"]
                    == "XP_CALCULATED"
                ).sum()
            )

        else:

            metrics_scored = 0
            xp_awards = 0

        if "xp_amount" in metric_xp.columns:

            period_xp = float(
                metric_xp["xp_amount"]
                .fillna(0)
                .sum()
            )

        else:

            period_xp = 0.0

    if (
        category_levels is None
        or category_levels.empty
    ):

        categories = 0

    else:

        categories = len(
            category_levels
        )

    return {
        "metrics_scored": metrics_scored,
        "xp_awards": xp_awards,
        "period_xp": period_xp,
        "categories": categories,
    }

def build_xp_period_summary(
    metric_xp: pd.DataFrame,
    start_date: date,
    end_date: date,
):
    """
    Calculate summary statistics for the selected period.

    Returns:

        total_xp
        average_daily_xp
        best_day_xp
        best_day
        active_days
        period_days
    """

    period_days = (
        end_date - start_date
    ).days + 1

    if (
        metric_xp is None
        or metric_xp.empty
    ):
        return {
            "total_xp": 0.0,
            "average_daily_xp": 0.0,
            "best_day_xp": 0.0,
            "best_day": None,
            "active_days": 0,
            "period_days": period_days,
        }

    trend = build_xp_trend(
        metric_xp
    )

    if trend.empty:
        return {
            "total_xp": 0.0,
            "average_daily_xp": 0.0,
            "best_day_xp": 0.0,
            "best_day": None,
            "active_days": 0,
            "period_days": period_days,
        }

    total_xp = float(
        trend["xp_earned"].sum()
    )

    active_days = int(
        len(trend)
    )

    average_daily_xp = (
        total_xp / period_days
        if period_days > 0
        else 0.0
    )

    best_index = trend[
        "xp_earned"
    ].idxmax()

    best_day = trend.loc[
        best_index,
        "activity_date",
    ]

    best_day_xp = float(
        trend.loc[
            best_index,
            "xp_earned",
        ]
    )

    return {
        "total_xp": total_xp,
        "average_daily_xp": average_daily_xp,
        "best_day_xp": best_day_xp,
        "best_day": best_day,
        "active_days": active_days,
        "period_days": period_days,
    }

# ============================================================
# Category dashboard model
# ============================================================

def build_category_cards(
    category_levels: pd.DataFrame,
    start_date: date,
    end_date: date,
):
    """
    Build the production data model used by Category cards.

    Each Category receives:

        category_id
        category_name
        total_xp
        current_level
        current_level_xp
        next_level_xp
        xp_to_next_level
        progress_ratio
        period_xp

    Category total XP and level come from the calculation engine.

    Period XP is read from xp_ledger for the selected date range.
    """

    columns = [
        "category_id",
        "category_name",
        "total_xp",
        "current_level",
        "current_level_xp",
        "next_level_xp",
        "xp_to_next_level",
        "progress_ratio",
        "period_xp",
    ]

    if (
        category_levels is None
        or category_levels.empty
    ):

        return pd.DataFrame(
            columns=columns
        )

    cards = category_levels.copy()

    # ------------------------------------------------------------
    # Read Category XP earned during the selected period.
    #
    # We deliberately read the ledger rather than reconstructing
    # Metric -> Category assignment in the dashboard.
    # ------------------------------------------------------------

    from pipeline.db import get_connection

    con = get_connection()

    try:

        period_category_xp = con.execute(
            """
            SELECT
                category_id,
                COALESCE(
                    SUM(xp_amount),
                    0
                ) AS period_xp

            FROM xp_ledger

            WHERE source_type = 'METRIC_PERFORMANCE'
              AND entry_date >= ?
              AND entry_date <= ?

            GROUP BY category_id
            """,
            [
                start_date,
                end_date,
            ],
        ).df()

    finally:

        con.close()

    # ------------------------------------------------------------
    # Attach period XP to Category state.
    # ------------------------------------------------------------

    if not period_category_xp.empty:

        cards = cards.merge(
            period_category_xp,
            on="category_id",
            how="left",
        )

    else:

        cards["period_xp"] = 0.0

    cards["period_xp"] = (
        cards["period_xp"]
        .fillna(0.0)
    )

    # ------------------------------------------------------------
    # Return only the fields required by the UI.
    # ------------------------------------------------------------

    return (
        cards[
            columns
        ]
        .sort_values(
            [
                "current_level",
                "total_xp",
            ],
            ascending=[
                False,
                False,
            ],
        )
        .reset_index(
            drop=True
        )
    )


# ============================================================
# Metric contribution model
# ============================================================

def build_category_metric_breakdown(
    metric_xp: pd.DataFrame,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """
    Build the Category → Metric contribution model
    for the selected dashboard period.

    Each row represents one Metric within one Category.

    Returns:

        category_id
        category_name
        metric_id
        metric_name
        xp_earned
        performance_ratio
        performance_points
        contribution_pct
    """

    columns = [
        "category_id",
        "category_name",
        "metric_id",
        "metric_name",
        "xp_earned",
        "performance_ratio",
        "performance_points",
        "contribution_pct",
    ]

    if (
        metric_xp is None
        or metric_xp.empty
    ):
        return pd.DataFrame(
            columns=columns
        )

    scored = metric_xp[
        metric_xp["status"]
        == "XP_CALCULATED"
    ].copy()

    if scored.empty:
        return pd.DataFrame(
            columns=columns
        )

    # --------------------------------------------------------
    # Restrict explicitly to the selected dashboard period.
    # --------------------------------------------------------

    scored = scored[
        (
            scored["activity_date"]
            >= start_date
        )
        &
        (
            scored["activity_date"]
            <= end_date
        )
    ]

    if scored.empty:
        return pd.DataFrame(
            columns=columns
        )

    # --------------------------------------------------------
    # Category information comes from the persisted ledger.
    # This avoids recreating Metric → Category logic here.
    # --------------------------------------------------------

    from pipeline.db import get_connection

    con = get_connection()

    try:

        metric_categories = con.execute(
            """
            SELECT DISTINCT
                metric_id,
                category_id
            FROM xp_ledger
            WHERE source_type =
                'METRIC_PERFORMANCE'
              AND entry_date >= ?
              AND entry_date <= ?
            """,
            [
                start_date,
                end_date,
            ],
        ).df()

        category_names = con.execute(
            """
            SELECT
                category_id,
                name AS category_name
            FROM categories
            """
        ).df()

    finally:

        con.close()

    if metric_categories.empty:
        return pd.DataFrame(
            columns=columns
        )

    scored = scored.merge(
        metric_categories,
        on="metric_id",
        how="left",
    )

    scored = scored.merge(
        category_names,
        on="category_id",
        how="left",
    )

    scored = scored.dropna(
        subset=[
            "category_id"
        ]
    )

    if scored.empty:
        return pd.DataFrame(
            columns=columns
        )

    # --------------------------------------------------------
    # Aggregate Metric performance for the period.
    # --------------------------------------------------------

    result = (
        scored
        .groupby(
            [
                "category_id",
                "category_name",
                "metric_id",
                "metric_name",
            ],
            as_index=False,
        )
        .agg(
            xp_earned=(
                "xp_amount",
                "sum",
            ),
            performance_ratio=(
                "performance_ratio",
                "mean",
            ),
            performance_points=(
                "performance_points",
                "sum",
            ),
        )
    )

    # --------------------------------------------------------
    # Contribution within Category.
    # --------------------------------------------------------

    category_totals = (
        result
        .groupby(
            "category_id"
        )["xp_earned"]
        .sum()
        .rename(
            "category_total"
        )
        .reset_index()
    )

    result = result.merge(
        category_totals,
        on="category_id",
        how="left",
    )

    result["contribution_pct"] = 0.0

    has_xp = (
        result["category_total"] > 0
    )

    result.loc[
        has_xp,
        "contribution_pct",
    ] = (
        result.loc[
            has_xp,
            "xp_earned",
        ]
        / result.loc[
            has_xp,
            "category_total",
        ]
        * 100
    )

    result["contribution_pct"] = (
        result["contribution_pct"]
        .round(1)
    )

    result = result.drop(
        columns=[
            "category_total"
        ]
    )

    return (
        result
        .sort_values(
            [
                "category_id",
                "xp_earned",
            ],
            ascending=[
                True,
                False,
            ],
        )
        .reset_index(
            drop=True
        )
    )

# ============================================================
# XP Trend
# ============================================================

def build_xp_trend(
    metric_xp: pd.DataFrame,
) -> pd.DataFrame:
    """
    Build daily XP totals for the selected dashboard period.

    Returns:

        activity_date
        xp_earned
    """

    columns = [
        "activity_date",
        "xp_earned",
    ]

    if (
        metric_xp is None
        or metric_xp.empty
    ):
        return pd.DataFrame(
            columns=columns
        )

    earned = metric_xp[
        metric_xp["status"]
        == "XP_CALCULATED"
    ].copy()

    if earned.empty:
        return pd.DataFrame(
            columns=columns
        )

    result = (
        earned
        .groupby(
            "activity_date",
            as_index=False,
        )["xp_amount"]
        .sum()
        .rename(
            columns={
                "xp_amount": "xp_earned"
            }
        )
        .sort_values(
            "activity_date"
        )
        .reset_index(drop=True)
    )

    return result

def build_cumulative_xp(
    metric_xp: pd.DataFrame,
) -> pd.DataFrame:
    """
    Build cumulative XP over the selected period.

    Returns:

        activity_date
        xp_earned
        cumulative_xp
    """

    columns = [
        "activity_date",
        "xp_earned",
        "cumulative_xp",
    ]

    if (
        metric_xp is None
        or metric_xp.empty
    ):
        return pd.DataFrame(
            columns=columns
        )

    trend = build_xp_trend(
        metric_xp
    )

    if trend.empty:
        return pd.DataFrame(
            columns=columns
        )

    result = trend.copy()

    result["cumulative_xp"] = (
        result["xp_earned"]
        .cumsum()
    )

    return result

def build_category_xp_history(
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """
    Build daily Category XP history for the selected period.

    Returns one row per:

        activity_date
        category_id
        category_name
        xp_earned
    """

    columns = [
        "activity_date",
        "category_id",
        "category_name",
        "xp_earned",
    ]

    from pipeline.db import get_connection

    con = get_connection()

    try:

        result = con.execute(
            """
            SELECT
                x.entry_date AS activity_date,
                x.category_id,
                c.name AS category_name,
                SUM(x.xp_amount) AS xp_earned

            FROM xp_ledger x

            INNER JOIN categories c
                ON c.category_id = x.category_id

            WHERE x.source_type =
                'METRIC_PERFORMANCE'

              AND x.entry_date >= ?
              AND x.entry_date <= ?

            GROUP BY
                x.entry_date,
                x.category_id,
                c.name

            ORDER BY
                x.entry_date,
                x.category_id
            """,
            [
                start_date,
                end_date,
            ],
        ).df()

    finally:

        con.close()

    if result.empty:
        return pd.DataFrame(
            columns=columns
        )

    return result

def build_category_cumulative_history(
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """
    Build cumulative Category XP within the selected period.
    """

    history = build_category_xp_history(
        start_date=start_date,
        end_date=end_date,
    )

    if history.empty:
        return history

    history = history.sort_values(
        [
            "category_id",
            "activity_date",
        ]
    ).copy()

    history["cumulative_xp"] = (
        history
        .groupby(
            "category_id"
        )["xp_earned"]
        .cumsum()
    )

    return history

# ============================================================
# Production Dashboard
# ============================================================

def render_dashboard():
    """
    Render the production Zist Dashboard.

    This function is the only Streamlit-facing entry point
    in dashboard.py.
    """

    st.header("📈 Zist Progress")

    st.caption(
        "Your performance, XP, and Category progression."
    )

    # ========================================================
    # Date range
    # ========================================================

    col1, col2 = st.columns(2)

    with col1:

        start_date = st.date_input(
            "Start date",
            value=(
                date.today()
                - timedelta(days=7)
            ),
            key="dashboard_start_date",
        )

    with col2:

        end_date = st.date_input(
            "End date",
            value=date.today(),
            key="dashboard_end_date",
        )

    if start_date > end_date:

        st.error(
            "Start date must be before or equal to End date."
        )

        return

    # ========================================================
    # Calculate
    # ========================================================

    if st.button(
        "Calculate progress",
        type="primary",
        key="dashboard_calculate",
    ):

        try:

            with st.spinner(
                "Calculating your progress..."
            ):

                data = get_dashboard_data(
                    start_date=start_date,
                    end_date=end_date,
                )

            st.session_state[
                "dashboard_data"
            ] = data

            # Keep the selected period with the calculated
            # dashboard state.
            st.session_state[
                "dashboard_period"
            ] = {
                "start_date": start_date,
                "end_date": end_date,
            }

            st.success(
                "Progress calculated successfully."
            )

        except Exception as exc:

            st.error(
                f"Could not calculate progress: {exc}"
            )

            return

    # ========================================================
    # Retrieve most recent result
    # ========================================================

    data = st.session_state.get(
        "dashboard_data"
    )

    if data is None:

        st.info(
            "Select a date range and calculate your progress."
        )

        return

    metric_xp = data[
        "metric_xp"
    ]

    category_levels = data[
        "category_levels"
    ]

    # ========================================================
    # Retrieve the period used for the current result
    # ========================================================

    dashboard_period = st.session_state.get(
        "dashboard_period"
    )

    if dashboard_period is not None:

        display_start_date = (
            dashboard_period["start_date"]
        )

        display_end_date = (
            dashboard_period["end_date"]
        )

    else:

        # Fallback for an existing session created before
        # dashboard_period was introduced.
        display_start_date = start_date
        display_end_date = end_date

    # ========================================================
    # Summary
    # ========================================================

    summary = build_dashboard_summary(
        metric_xp,
        category_levels,
    )

    c1, c2, c3, c4 = st.columns(4)

    c1.metric(
        "Metrics scored",
        summary["metrics_scored"],
    )

    c2.metric(
        "XP awards",
        summary["xp_awards"],
    )

    c3.metric(
        "Period XP",
        f"{summary['period_xp']:.1f}",
    )

    c4.metric(
        "Categories",
        summary["categories"],
    )

    st.divider()

    # ========================================================
    # Category cards
    # ========================================================

    st.subheader("Categories")

    cards = build_category_cards(
        category_levels=category_levels,
        start_date=display_start_date,
        end_date=display_end_date,
    )

    if cards.empty:

        st.info(
            "No Category progression available yet."
        )

    else:

        # ----------------------------------------------------
        # Two cards per row.
        # ----------------------------------------------------

        for start in range(
            0,
            len(cards),
            2,
        ):

            row_cards = cards.iloc[
                start:start + 2
            ]

            columns = st.columns(
                len(row_cards)
            )

            for column, row in zip(
                columns,
                row_cards.itertuples(
                    index=False
                ),
            ):

                with column:

                    with st.container(
                        border=True
                    ):

                        # ------------------------------------
                        # Category name
                        # ------------------------------------

                        st.markdown(
                            f"### {row.category_name}"
                        )

                        # ------------------------------------
                        # Current level
                        # ------------------------------------

                        if pd.isna(
                            row.current_level
                        ):

                            st.markdown(
                                "**Level —**"
                            )

                        else:

                            st.markdown(
                                f"**Level "
                                f"{int(row.current_level)}**"
                            )

                        # ------------------------------------
                        # Progress
                        # ------------------------------------

                        if pd.isna(
                            row.progress_ratio
                        ):

                            progress = 0.0

                        else:

                            progress = max(
                                0.0,
                                min(
                                    1.0,
                                    float(
                                        row.progress_ratio
                                    ),
                                ),
                            )

                        st.progress(
                            progress
                        )

                        # ------------------------------------
                        # Current-level XP
                        # ------------------------------------

                        if (
                            pd.isna(
                                row.current_level_xp
                            )
                            or pd.isna(
                                row.next_level_xp
                            )
                        ):

                            st.caption(
                                "Level progression unavailable"
                            )

                        else:

                            st.caption(
                                f"{row.current_level_xp:.1f} "
                                f"/ {row.next_level_xp:.1f} XP"
                            )

                        # ------------------------------------
                        # Card statistics
                        # ------------------------------------

                        stat1, stat2 = st.columns(2)

                        with stat1:

                            st.metric(
                                "Total XP",
                                f"{row.total_xp:.1f}",
                            )

                        with stat2:

                            st.metric(
                                "Period XP",
                                f"+{row.period_xp:.1f}",
                            )

                        # ------------------------------------
                        # XP remaining
                        # ------------------------------------

                        if pd.isna(
                            row.xp_to_next_level
                        ):

                            st.caption(
                                "XP to next level unavailable"
                            )

                        else:

                            st.caption(
                                f"{row.xp_to_next_level:.1f} XP "
                                "to next level"
                            )
    # ------------------------------------------------------------
    # XP Overview
    # ------------------------------------------------------------

    st.divider()

    st.subheader("XP Overview")

    xp_summary = build_xp_period_summary(
        metric_xp,
        start_date,
        end_date,
    )

    c1, c2, c3, c4 = st.columns(4)

    with c1:
        st.metric(
            "Period XP",
            f"{xp_summary['total_xp']:.1f}",
        )

    with c2:
        st.metric(
            "Daily Average",
            f"{xp_summary['average_daily_xp']:.1f}",
        )

    with c3:
        st.metric(
            "Best Day",
            f"{xp_summary['best_day_xp']:.1f}",
        )

    with c4:
        st.metric(
            "Active Days",
            f"{xp_summary['active_days']}"
            f" / {xp_summary['period_days']}",
        )
    xp_trend = build_xp_trend(
        metric_xp
    )

    if xp_trend.empty:

        st.info(
            "No XP earned during the selected period."
        )

    else:

        chart_data = (
            xp_trend
            .set_index("activity_date")
            [["xp_earned"]]
            .rename(
                columns={
                    "xp_earned": "XP"
                }
            )
        )

        st.line_chart(
            chart_data,
            use_container_width=True,
        )
        # ========================================================
        # Category Breakdown
        # ========================================================

    st.divider()

    st.subheader(
        "Category Breakdown"
    )

    breakdown = build_category_metric_breakdown(
        metric_xp=metric_xp,
        start_date=display_start_date,
        end_date=display_end_date,
    )

    if breakdown.empty:

        st.info(
            "No Metric XP has been earned in this period."
        )

    else:

        # ----------------------------------------------------
        # One expandable section per Category.
        # ----------------------------------------------------

        for category_id in (
            breakdown["category_id"]
            .dropna()
            .unique()
        ):

            category_df = breakdown[
                breakdown["category_id"]
                == category_id
            ].copy()

            if category_df.empty:
                continue

            category_name = (
                category_df[
                    "category_name"
                ].iloc[0]
            )

            category_xp = float(
                category_df[
                    "xp_earned"
                ].sum()
            )

            with st.expander(
                f"{category_name} • "
                f"{category_xp:.1f} XP",
                expanded=True,
            ):

                # --------------------------------------------
                # Metrics within Category
                # --------------------------------------------

                for row in category_df.itertuples(
                    index=False
                ):

                    st.markdown(
                        f"**{row.metric_name}**"
                    )

                    # ----------------------------------------
                    # Performance bar
                    # ----------------------------------------

                    if pd.isna(
                        row.performance_ratio
                    ):

                        performance = 0.0

                    else:

                        performance = max(
                            0.0,
                            min(
                                1.0,
                                float(
                                    row.performance_ratio
                                ),
                            ),
                        )

                    st.progress(
                        performance
                    )

                    # ----------------------------------------
                    # Metric statistics
                    # ----------------------------------------

                    c1, c2, c3 = st.columns(3)

                    with c1:

                        st.metric(
                            "XP",
                            f"{row.xp_earned:.1f}",
                        )

                    with c2:

                        if pd.isna(
                            row.performance_ratio
                        ):

                            st.metric(
                                "Performance",
                                "—",
                            )

                        else:

                            st.metric(
                                "Performance",
                                f"{float(row.performance_ratio) * 100:.1f}%",
                            )

                    with c3:

                        st.metric(
                            "Contribution",
                            f"{row.contribution_pct:.1f}%",
                        )

                    # ----------------------------------------
                    # Performance points
                    # ----------------------------------------

                    if pd.isna(
                        row.performance_points
                    ):

                        st.caption(
                            "Performance points unavailable"
                        )

                    else:

                        st.caption(
                            f"{float(row.performance_points):.1f} "
                            "performance points"
                        )

                    st.divider()

        # ========================================================
    # Progress History
    # ========================================================

    st.divider()

    st.subheader(
        "Progress History"
    )

    cumulative_xp = build_cumulative_xp(
        metric_xp
    )

    if cumulative_xp.empty:

        st.info(
            "No XP history is available for this period."
        )

    else:

        history_chart = (
            cumulative_xp
            .set_index(
                "activity_date"
            )[
                [
                    "cumulative_xp"
                ]
            ]
            .rename(
                columns={
                    "cumulative_xp":
                        "Cumulative XP"
                }
            )
        )

        st.caption(
            "Cumulative XP earned during the selected period."
        )

        st.line_chart(
            history_chart,
            use_container_width=True,
        )

    # --------------------------------------------------------
    # Category history
    # --------------------------------------------------------

    category_history = (
        build_category_cumulative_history(
            start_date=display_start_date,
            end_date=display_end_date,
        )
    )

    if not category_history.empty:

        st.markdown(
            "#### Category XP"
        )

        category_chart = (
            category_history
            .pivot(
                index="activity_date",
                columns="category_name",
                values="cumulative_xp",
            )
            .ffill()
            .fillna(0)
        )

        st.line_chart(
            category_chart,
            use_container_width=True,
        )
   