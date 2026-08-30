from dashboard import render_dashboard
from datetime import date, timedelta

import pandas as pd

from pipeline.calculation import (
    calculate_scored_metrics,
    calculate_metric_progression,
    calculate_and_persist_metric_xp,
    calculate_category_levels,
)
import streamlit as st
import pandas as pd
import re
from datetime import date

from pipeline.db import (
    initialize_database,
    get_connection,
    get_import_history,
    next_id,
)
from pipeline.ingest import import_loop_zip
from pipeline.scoring import SCORING_METHODS, calculate_score

st.set_page_config(
    page_title="Zist",
    page_icon="🎮",
    layout="wide",
)

def _safe_date(value):
    """Convert pandas/DB null dates to None for Streamlit date_input."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


initialize_database()

# Configuration edit state (UI only; database history is preserved via effective dates).
for _key in [
    "edit_category_id",
    "edit_metric_id",
    "edit_mapping_id",
    "edit_formula_input_id",
    "edit_rule_id",
]:
    if _key not in st.session_state:
        st.session_state[_key] = None

st.title("🎮 Zist")
st.caption("Local-first habit data ingestion and configurable performance system")

page = st.sidebar.radio(
    "Navigate",
    [
        "Dashboard",
        "Import Loop ZIP",
        "Habits",
        "Observations",
        "Configuration",
        "XP & Categories",
        "Import History",
        "Progress"
    ],
)

if page == "Dashboard":
    render_dashboard()
    
elif page == "Import Loop ZIP":
    st.header("📥 Import Loop ZIP")
    st.write(
        "Upload a Loop Habits CSV ZIP. You can upload the same export repeatedly. "
        "Existing records are updated only when their values change; new records are inserted."
    )

    uploaded = st.file_uploader(
        "Choose Loop ZIP export",
        type=["zip"],
        help="Example: Loop Habits CSV 2026-08-10.zip",
    )

    if uploaded is not None:
        if st.button("Import / Update", type="primary"):
            with st.spinner("Reading and importing Loop data..."):
                result = import_loop_zip(uploaded.getvalue(), uploaded.name)

            if result["errors"]:
                st.error("Import completed with errors.")
                for error in result["errors"]:
                    st.write(f"- {error}")
            else:
                st.success("Import completed successfully.")

            a, b, c, d = st.columns(4)
            a.metric("New", result["inserted"])
            b.metric("Updated", result["updated"])
            c.metric("Unchanged", result["unchanged"])
            d.metric("Habits", result["habits"])

            st.write(
                f"**Activity range:** {result['min_date'] or '—'} → "
                f"{result['max_date'] or '—'}"
            )

            if result["changed_records"]:
                st.subheader("Changed records")
                st.dataframe(
                    pd.DataFrame(result["changed_records"]),
                    use_container_width=True,
                    hide_index=True,
                )

elif page == "Habits":
    st.header("🧩 Imported Habits")
    con = get_connection()
    df = con.execute("""
        SELECT
            habit_id,
            name,
            habit_type,
            question,
            unit,
            target_type,
            target_value,
            frequency_numerator,
            frequency_denominator,
            archived,
            first_observation,
            last_observation,
            observation_count
        FROM habit_summary
        ORDER BY habit_id
    """).df()
    con.close()
    st.dataframe(df, use_container_width=True, hide_index=True)

elif page == "Observations":
    st.header("📊 Normalized Observations")
    con = get_connection()

    c1, c2 = st.columns(2)
    with c1:
        habits = con.execute(
            "SELECT habit_id, name FROM habits ORDER BY habit_id"
        ).df()
        selected_habit = st.selectbox(
            "Habit",
            ["All"] + [
                f"{r.habit_id} — {r.name}" for r in habits.itertuples()
            ],
        )
    with c2:
        min_date, max_date = con.execute("""
            SELECT MIN(activity_date), MAX(activity_date)
            FROM observations
        """).fetchone()
        date_range = None
        if min_date and max_date:
            date_range = st.date_input(
                "Date range",
                value=(min_date, max_date),
            )

    query = """
        SELECT
            o.activity_date,
            o.habit_id,
            h.name AS habit,
            h.habit_type,
            h.unit,
            o.raw_value,
            o.numeric_value,
            o.value_status,
            o.notes,
            o.source_file
        FROM observations o
        JOIN habits h ON h.habit_id = o.habit_id
        WHERE 1=1
    """
    params = []

    if selected_habit != "All":
        habit_id = selected_habit.split(" — ")[0]
        query += " AND o.habit_id = ?"
        params.append(habit_id)

    if date_range:
        if isinstance(date_range, tuple) and len(date_range) == 2:
            query += " AND o.activity_date BETWEEN ? AND ?"
            params.extend([date_range[0], date_range[1]])

    query += " ORDER BY o.activity_date DESC, o.habit_id"

    df = con.execute(query, params).df()
    con.close()
    st.caption(f"{len(df):,} observation(s)")
    st.dataframe(df, use_container_width=True, hide_index=True)

elif page == "Configuration":
    st.header("⚙️ Configuration")

    tab_categories, tab_metrics, tab_mapping, tab_rules = st.tabs(
        ["Categories", "Metrics", "Habit → Metric", "Scoring Rules"]
    )

    # ---------------- Categories ----------------
    with tab_categories:
        st.subheader("Categories")
        st.caption("High-level attributes such as Health, Career or Learning.")

        con = get_connection()
        categories = con.execute("""
            SELECT category_id, name, description, start_date, end_date
            FROM categories
            ORDER BY name
        """).df()
        con.close()

        if not categories.empty:
            category_display = categories.copy()
            category_display.insert(0, "Edit", False)
            edited = st.data_editor(
                category_display,
                use_container_width=True,
                hide_index=True,
                disabled=[c for c in category_display.columns if c != "Edit"],
                column_config={
                    "Edit": st.column_config.CheckboxColumn(
                        "Edit",
                        help="Select a category to edit",
                        default=False,
                        width="small",
                    )
                },
                key="categories_editor",
            )
            selected = edited.loc[edited["Edit"]]
            if len(selected) == 1:
                st.session_state.edit_category_id = int(selected.iloc[0]["category_id"])
            elif len(selected) > 1:
                st.warning("Select only one category to edit.")

        if st.session_state.edit_category_id is not None:
            edit_row = categories[categories["category_id"] == st.session_state.edit_category_id].iloc[0]
            with st.form("edit_category"):
                st.markdown(f"**Edit category: {edit_row['name']}**")
                name = st.text_input("Name", value=edit_row["name"])
                description = st.text_input("Description", value=edit_row["description"] or "")
                c1, c2 = st.columns(2)
                start = c1.date_input("Start date", value=_safe_date(edit_row["start_date"]))
                end = c2.date_input("End date (optional)", value=_safe_date(edit_row["end_date"]))
                save_col, cancel_col = st.columns(2)
                save_clicked = save_col.form_submit_button("Save changes", type="primary")
                cancel_clicked = cancel_col.form_submit_button("Cancel")
                if save_clicked:
                    con = get_connection()
                    con.execute("""
                        UPDATE categories
                        SET name = ?, description = ?, start_date = ?, end_date = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE category_id = ?
                    """, [name.strip(), description.strip() or None, start, end, int(edit_row["category_id"])])
                    con.close()
                    st.session_state.edit_category_id = None
                    st.rerun()
                if cancel_clicked:
                    st.session_state.edit_category_id = None
                    st.rerun()

        with st.form("add_category"):
            st.markdown("**Add category**")
            name = st.text_input("Name", placeholder="Health")
            description = st.text_input("Description", placeholder="Physical wellbeing")
            c1, c2 = st.columns(2)
            start = c1.date_input("Start date", value=date.today())
            end = c2.date_input("End date (optional)", value=None)
            submitted = st.form_submit_button("Save category")

            if submitted:
                if not name.strip():
                    st.error("Category name is required.")
                elif end and end < start:
                    st.error("End date cannot be before start date.")
                else:
                    con = get_connection()
                    try:
                        category_id = next_id(con, "categories", "category_id")
                        con.execute("""
                            INSERT INTO categories (
                                category_id, name, description, start_date, end_date
                            )
                            VALUES (?, ?, ?, ?, ?)
                        """, [
                            category_id,
                            name.strip(),
                            description.strip() or None,
                            start,
                            end,
                        ])
                        st.success("Category saved.")
                    except Exception as exc:
                        st.error(f"Could not save category: {exc}")
                    finally:
                        con.close()

    # ---------------- Metrics ----------------
    with tab_metrics:
        st.subheader("Metrics")
        st.caption(
            "A metric is what you actually measure, independent of the Loop habit."
        )

        con = get_connection()
        metrics = con.execute("""
            SELECT
                m.metric_id,
                m.name,
                m.measurement_type,
                m.unit,
                m.description,
                m.metric_kind,
                m.formula,
                m.start_date,
                m.end_date,
                m.category_id,
                c.name AS category
            FROM metrics m
            LEFT JOIN categories c ON c.category_id = m.category_id
            ORDER BY m.name
        """).df()
        con.close()

        if not metrics.empty:
            metric_display = metrics.copy()
            metric_display.insert(0, "Edit", False)
            edited = st.data_editor(
                metric_display,
                use_container_width=True,
                hide_index=True,
                disabled=[c for c in metric_display.columns if c != "Edit"],
                column_config={
                    "Edit": st.column_config.CheckboxColumn(
                        "Edit",
                        help="Select a metric to edit",
                        default=False,
                        width="small",
                    )
                },
                key="metrics_editor",
            )
            selected = edited.loc[edited["Edit"]]
            if len(selected) == 1:
                st.session_state.edit_metric_id = int(selected.iloc[0]["metric_id"])
            elif len(selected) > 1:
                st.warning("Select only one metric to edit.")

        if st.session_state.edit_metric_id is not None:
            edit_row = metrics[metrics["metric_id"] == st.session_state.edit_metric_id].iloc[0]
            with st.form("edit_metric"):
                st.markdown(f"**Edit metric: {edit_row['name']}**")
                name = st.text_input("Metric name", value=edit_row["name"])
                metric_kind = st.selectbox("Metric type", ["RAW", "DERIVED"], index=0 if edit_row["metric_kind"] == "RAW" else 1)
                measurement_type = st.text_input("Measurement type", value=edit_row["measurement_type"])
                unit = st.text_input("Unit", value=edit_row["unit"] or "")
                description = st.text_input("Description", value=edit_row["description"] or "")

                con = get_connection()
                category_rows = con.execute(
                    "SELECT category_id, name FROM categories ORDER BY name"
                ).df()
                con.close()
                category_options = {"None": None}
                category_options.update({
                    f"{r.category_id} — {r.name}": int(r.category_id)
                    for r in category_rows.itertuples()
                })
                current_category = "None"
                if not pd.isna(edit_row["category_id"]) and edit_row["category_id"] is not None:
                    current_category = next(
                        (label for label, cid in category_options.items()
                         if cid == int(edit_row["category_id"])),
                        "None"
                    )
                category_label = st.selectbox(
                    "Category",
                    list(category_options),
                    index=list(category_options).index(current_category),
                )

                formula = ""
                if metric_kind == "DERIVED":
                    formula = st.text_input(
                        "Formula",
                        value=edit_row["formula"] or "",
                        placeholder="Deep_Work_Hours / Work_Hours",
                        help="Use the variable names configured under Derived Metric Formula Inputs.",
                    )
                    st.caption(
                        "Examples: `Deep_Work_Hours / Work_Hours` · "
                        "`Steps / 10000` · "
                        "`MIN(Sleep_Hours / 8, 1)` · "
                        "`0.6 * Sleep_Score + 0.4 * Exercise_Score`"
                    )
                c1, c2 = st.columns(2)
                start = c1.date_input("Start date", value=_safe_date(edit_row["start_date"]))
                end = c2.date_input("End date (optional)", value=_safe_date(edit_row["end_date"]))
                save_col, cancel_col = st.columns(2)
                save_clicked = save_col.form_submit_button("Save changes", type="primary")
                cancel_clicked = cancel_col.form_submit_button("Cancel")
                if save_clicked:
                    con = get_connection()
                    con.execute("""
                        UPDATE metrics
                        SET name = ?, metric_kind = ?, measurement_type = ?, unit = ?,
                            description = ?, formula = ?, category_id = ?,
                            start_date = ?, end_date = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE metric_id = ?
                    """, [name.strip(), metric_kind, measurement_type, unit.strip() or None,
                          description.strip() or None, formula.strip() or None,
                          category_options[category_label], start, end,
                          int(edit_row["metric_id"])])
                    con.close()
                    st.session_state.edit_metric_id = None
                    st.rerun()
                if cancel_clicked:
                    st.session_state.edit_metric_id = None
                    st.rerun()

        with st.form("add_metric"):
            st.markdown("**Add metric**")
            name = st.text_input("Metric name", placeholder="Exercise")
            metric_kind = st.selectbox(
                "Metric type",
                ["RAW", "DERIVED"],
                format_func=lambda x: "Raw metric" if x == "RAW" else "Derived metric",
                help="Raw metrics come directly from Loop-linked habits. Derived metrics are calculated from other metrics.",
            )
            measurement_type = st.selectbox(
                "Measurement type",
                ["Duration", "Quantity", "Count", "Rating", "Binary", "Percentage", "Other"],
            )
            unit = st.text_input("Unit", placeholder="minutes")
            description = st.text_input("Description")

            con = get_connection()
            category_rows = con.execute(
                "SELECT category_id, name FROM categories ORDER BY name"
            ).df()
            con.close()
            category_options = {"None": None}
            category_options.update({
                f"{r.category_id} — {r.name}": int(r.category_id)
                for r in category_rows.itertuples()
            })
            category_label = st.selectbox("Category", list(category_options))

            formula = ""
            if metric_kind == "DERIVED":
                formula = st.text_input(
                    "Formula",
                    placeholder="Deep_Work_Hours / Work_Hours",
                    help="Use the variable names shown by the formula inputs below. Supported: +, -, *, /, %, parentheses, MIN, MAX, ABS, ROUND.",
                )
                st.caption(
                    "Examples: `Deep_Work_Hours / Work_Hours` · "
                    "`Steps / 10000` · "
                    "`MIN(Sleep_Hours / 8, 1)` · "
                    "`0.6 * Sleep_Score + 0.4 * Exercise_Score`"
                )
            c1, c2 = st.columns(2)
            start = c1.date_input("Start date", value=date.today())
            end = c2.date_input("End date (optional)", value=None)
            submitted = st.form_submit_button("Save metric")

            if submitted:
                if not name.strip():
                    st.error("Metric name is required.")
                elif end and end < start:
                    st.error("End date cannot be before start date.")
                else:
                    con = get_connection()
                    try:
                        metric_id = next_id(con, "metrics", "metric_id")
                        con.execute("""
                        INSERT INTO metrics (
                            metric_id, name, measurement_type, unit,
                            description, metric_kind, formula, category_id,
                            start_date, end_date
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, [
                        metric_id,
                        name.strip(),
                        measurement_type,
                        unit.strip() or None,
                        description.strip() or None,
                        metric_kind,
                        formula.strip() or None,
                        category_options[category_label],
                        start,
                        end,
                    ])
                        st.success("Metric saved.")
                    except Exception as exc:
                        st.error(f"Could not save metric: {exc}")
                    finally:
                        con.close()

    # ---------------- Habit mapping ----------------
    with tab_mapping:
        st.subheader("Habit → Metric Mapping")
        st.caption(
            "Connect a Loop habit to one of your Zist metrics. Weights are stored "
            "now and will be used later when category scoring is implemented."
        )

        con = get_connection()
        habits = con.execute(
            "SELECT habit_id, name FROM habits ORDER BY name"
        ).df()
        metrics = con.execute(
            "SELECT metric_id, name FROM metrics ORDER BY name"
        ).df()
        mappings = con.execute("""
            SELECT
                m.mapping_id,
                m.habit_id,
                h.name AS habit,
                m.metric_id,
                mt.name AS metric,
                m.weight,
                m.start_date,
                m.end_date
            FROM metric_habit_mapping m
            JOIN habits h ON h.habit_id = m.habit_id
            JOIN metrics mt ON mt.metric_id = m.metric_id
            ORDER BY h.name, mt.name
        """).df()
        con.close()

        if not mappings.empty:
            mapping_display = mappings.copy()
            mapping_display.insert(0, "Edit", False)
            edited = st.data_editor(
                mapping_display,
                use_container_width=True,
                hide_index=True,
                disabled=[c for c in mapping_display.columns if c != "Edit"],
                column_config={
                    "Edit": st.column_config.CheckboxColumn(
                        "Edit",
                        help="Select a mapping to edit",
                        default=False,
                        width="small",
                    )
                },
                key="mappings_editor",
            )
            selected = edited.loc[edited["Edit"]]
            if len(selected) == 1:
                st.session_state.edit_mapping_id = int(selected.iloc[0]["mapping_id"])
            elif len(selected) > 1:
                st.warning("Select only one mapping to edit.")

        if st.session_state.edit_mapping_id is not None:
            edit_row = mappings[mappings["mapping_id"] == st.session_state.edit_mapping_id].iloc[0]
            with st.form("edit_mapping"):
                st.markdown(f"**Edit mapping: {edit_row['habit']} → {edit_row['metric']}**")
                weight = st.number_input("Weight", value=float(edit_row["weight"]))
                c1, c2 = st.columns(2)
                start = c1.date_input("Start date", value=_safe_date(edit_row["start_date"]))
                end = c2.date_input("End date (optional)", value=_safe_date(edit_row["end_date"]))
                save_col, cancel_col = st.columns(2)
                save_clicked = save_col.form_submit_button("Save changes", type="primary")
                cancel_clicked = cancel_col.form_submit_button("Cancel")
                if save_clicked:
                    con = get_connection()
                    con.execute("""
                        UPDATE metric_habit_mapping
                        SET weight = ?, start_date = ?, end_date = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE mapping_id = ?
                    """, [weight, start, end, int(edit_row["mapping_id"])])
                    con.close()
                    st.session_state.edit_mapping_id = None
                    st.rerun()
                if cancel_clicked:
                    st.session_state.edit_mapping_id = None
                    st.rerun()

        if habits.empty:
            st.info("Import your Loop ZIP first so Zist has habits to map.")
        elif metrics.empty:
            st.info("Create at least one metric first.")
        else:
            habit_options = {
                f"{r.habit_id} — {r.name}": r.habit_id
                for r in habits.itertuples()
            }
            metric_options = {
                f"{r.metric_id} — {r.name}": int(r.metric_id)
                for r in metrics.itertuples()
            }

            with st.form("add_mapping"):
                habit_label = st.selectbox("Loop habit", list(habit_options))
                metric_label = st.selectbox("Zist metric", list(metric_options))
                weight = st.number_input("Weight", min_value=0.0, value=1.0, step=0.1)
                c1, c2 = st.columns(2)
                start = c1.date_input("Start date", value=date.today())
                end = c2.date_input("End date (optional)", value=None)
                submitted = st.form_submit_button("Save mapping")

                if submitted:
                    if end and end < start:
                        st.error("End date cannot be before start date.")
                    else:
                        con = get_connection()
                        try:
                            mapping_id = next_id(con, "metric_habit_mapping", "mapping_id")
                            con.execute("""
                                INSERT INTO metric_habit_mapping (
                                    mapping_id, metric_id, habit_id, weight,
                                    start_date, end_date
                                )
                                VALUES (?, ?, ?, ?, ?, ?)
                            """, [
                                mapping_id,
                                metric_options[metric_label],
                                habit_options[habit_label],
                                weight,
                                start,
                                end,
                            ])
                            st.success("Mapping saved.")
                        except Exception as exc:
                            st.error(f"Could not save mapping: {exc}")
                        finally:
                            con.close()


    # ---------------- Derived metric formulas ----------------
    with tab_metrics:
        st.divider()
        st.subheader("Derived Metric Formula Inputs")
        st.caption(
            "For a derived metric, map source metrics to simple variable names. "
            "Those names are what you use in the formula."
        )

        con = get_connection()
        derived_metrics = con.execute("""
            SELECT metric_id, name, formula
            FROM metrics
            WHERE metric_kind = 'DERIVED'
            ORDER BY name
        """).df()
        source_metrics = con.execute("""
            SELECT metric_id, name
            FROM metrics
            ORDER BY name
        """).df()
        formula_inputs = con.execute("""
            SELECT
                fi.formula_input_id,
                dm.name AS derived_metric,
                sm.name AS source_metric,
                fi.variable_name
            FROM metric_formula_inputs fi
            JOIN metrics dm ON dm.metric_id = fi.derived_metric_id
            JOIN metrics sm ON sm.metric_id = fi.source_metric_id
            ORDER BY dm.name, fi.variable_name
        """).df()
        con.close()

        if not formula_inputs.empty:
            formula_display = formula_inputs.copy()
            formula_display.insert(0, "Edit", False)
            edited = st.data_editor(
                formula_display,
                use_container_width=True,
                hide_index=True,
                disabled=[c for c in formula_display.columns if c != "Edit"],
                column_config={
                    "Edit": st.column_config.CheckboxColumn(
                        "Edit",
                        help="Select a formula input to edit",
                        default=False,
                        width="small",
                    )
                },
                key="formula_inputs_editor",
            )
            selected = edited.loc[edited["Edit"]]
            if len(selected) == 1:
                st.session_state.edit_formula_input_id = int(selected.iloc[0]["formula_input_id"])
            elif len(selected) > 1:
                st.warning("Select only one formula input to edit.")

        if st.session_state.edit_formula_input_id is not None:
            edit_row = formula_inputs[
                formula_inputs["formula_input_id"] == st.session_state.edit_formula_input_id
            ].iloc[0]
            with st.form("edit_formula_input"):
                st.markdown(
                    f"**Edit formula input: "
                    f"{edit_row['derived_metric']} → {edit_row['source_metric']}**"
                )

                variable_name = st.text_input(
                    "Variable name",
                    value=edit_row["variable_name"],
                )

                save_col, cancel_col = st.columns(2)

                save_clicked = save_col.form_submit_button(
                    "Save changes",
                    type="primary",
                )

                cancel_clicked = cancel_col.form_submit_button(
                    "Cancel",
                )

                if save_clicked:
                    if not re.match(
                        r"^[A-Za-z][A-Za-z0-9_]*$",
                        variable_name.strip(),
                    ):
                        st.error(
                            "Variable name must start with a letter "
                            "and contain only letters, numbers and underscores."
                        )
                    else:
                        con = get_connection()

                        try:
                            con.execute("""
                                UPDATE metric_formula_inputs
                                SET
                                    variable_name = ?,
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE formula_input_id = ?
                            """, [
                                variable_name.strip(),
                                int(edit_row["formula_input_id"]),
                            ])

                            st.success("Formula input updated.")

                        except Exception as exc:
                            st.error(
                                f"Could not update formula input: {exc}"
                            )

                        finally:
                            con.close()

                        st.session_state.edit_formula_input_id = None
                        st.rerun()

                if cancel_clicked:
                    st.session_state.edit_formula_input_id = None
                    st.rerun()
        if derived_metrics.empty:
            st.info("Create a Derived metric above first.")
        else:
            derived_options = {
                f"{r.metric_id} — {r.name}": int(r.metric_id)
                for r in derived_metrics.itertuples()
            }
            source_options = {
                f"{r.metric_id} — {r.name}": int(r.metric_id)
                for r in source_metrics.itertuples()
            }

            with st.form("add_formula_input"):
                derived_label = st.selectbox("Derived metric", list(derived_options))
                source_label = st.selectbox("Source metric", list(source_options))
                variable_name = st.text_input(
                    "Variable name",
                    placeholder="Deep_Work_Hours",
                    help="Use letters, numbers and underscores only; it must start with a letter.",
                )
                submitted = st.form_submit_button("Save formula input")

                if submitted:
                    valid_name = re.match(r"^[A-Za-z][A-Za-z0-9_]*$", variable_name.strip())
                    if not valid_name:
                        st.error("Variable name must start with a letter and contain only letters, numbers and underscores.")
                    elif derived_options[derived_label] == source_options[source_label]:
                        st.error("A derived metric cannot use itself as a source metric.")
                    else:
                        con = get_connection()
                        try:
                            input_id = next_id(con, "metric_formula_inputs", "formula_input_id")
                            con.execute("""
                                INSERT INTO metric_formula_inputs (
                                    formula_input_id, derived_metric_id, source_metric_id,
                                    variable_name
                                )
                                VALUES (?, ?, ?, ?)
                            """, [
                                input_id,
                                derived_options[derived_label],
                                source_options[source_label],
                                variable_name.strip(),
                            ])
                            st.success("Formula input saved.")
                        except Exception as exc:
                            st.error(f"Could not save formula input: {exc}")
                        finally:
                            con.close()

        st.markdown("**Formula preview**")
        if not derived_metrics.empty:
            preview_metric = st.selectbox(
                "Preview derived metric",
                [f"{r.metric_id} — {r.name}" for r in derived_metrics.itertuples()],
                key="formula_preview_metric",
            )
            selected_id = int(preview_metric.split(" — ")[0])

            con = get_connection()
            metric_row = con.execute(
                "SELECT formula FROM metrics WHERE metric_id = ?",
                [selected_id],
            ).fetchone()
            inputs = con.execute("""
                SELECT variable_name
                FROM metric_formula_inputs
                WHERE derived_metric_id = ?
                ORDER BY variable_name
            """, [selected_id]).df()
            con.close()

            formula = metric_row[0] if metric_row else None
            if formula:
                st.code(formula)
                variables = {}
                cols = st.columns(max(1, min(3, len(inputs))))
                for i, row in inputs.iterrows():
                    variable = row["variable_name"]
                    with cols[i % len(cols)]:
                        variables[variable] = st.number_input(
                            variable,
                            value=1.0,
                            step=0.1,
                            key=f"formula_var_{selected_id}_{variable}",
                        )
                try:
                    from pipeline.scoring import evaluate_formula
                    value = evaluate_formula(formula, variables)
                    st.metric("Formula result", f"{value:.4f}")
                except Exception as exc:
                    st.warning(str(exc))
            else:
                st.info("This derived metric does not have a formula yet.")

    # ---------------- Scoring rules ----------------
    with tab_rules:
        st.subheader("Scoring Rules")
        st.caption(
            "Choose a built-in scoring method for each metric. Rules are effective-dated, "
            "so you can change your standards without changing the historical definition."
        )

        con = get_connection()
        metrics = con.execute(
            "SELECT metric_id, name, unit FROM metrics ORDER BY name"
        ).df()
        categories = con.execute(
            "SELECT category_id, name FROM categories ORDER BY name"
        ).df()
        rules = con.execute("""
            SELECT
                r.rule_id,
                m.name AS metric,
                r.scoring_method,
                r.target_value,
                r.min_value,
                r.max_value,
                r.rating_max,
                r.max_points,
                r.start_date,
                r.end_date
            FROM scoring_rules r
            JOIN metrics m ON m.metric_id = r.metric_id
            ORDER BY m.name, r.start_date
        """).df()
        con.close()

        if not rules.empty:
            display_rules = rules.copy()
            display_rules["scoring_method"] = display_rules["scoring_method"].map(
                lambda x: SCORING_METHODS.get(x, x)
            )
            display_rules.insert(0, "Edit", False)
            edited = st.data_editor(
                display_rules,
                use_container_width=True,
                hide_index=True,
                disabled=[c for c in display_rules.columns if c != "Edit"],
                column_config={
                    "Edit": st.column_config.CheckboxColumn(
                        "Edit",
                        help="Select a scoring rule to edit",
                        default=False,
                        width="small",
                    )
                },
                key="scoring_rules_editor",
            )
            selected = edited.loc[edited["Edit"]]
            if len(selected) == 1:
                st.session_state.edit_rule_id = int(selected.iloc[0]["rule_id"])
            elif len(selected) > 1:
                st.warning("Select only one scoring rule to edit.")

        if st.session_state.edit_rule_id is not None:
            edit_row = rules[rules["rule_id"] == st.session_state.edit_rule_id].iloc[0]
            with st.form("edit_scoring_rule"):
                st.markdown(f"**Edit scoring rule: {edit_row['metric']}**")
                method = st.selectbox(
                    "Scoring method",
                    list(SCORING_METHODS.keys()),
                    index=list(SCORING_METHODS.keys()).index(edit_row["scoring_method"]),
                    format_func=lambda x: SCORING_METHODS[x],
                )
                target = st.number_input("Target", value=float(edit_row["target_value"] or 0))
                min_value = st.number_input("Range minimum", value=float(edit_row["min_value"] or 0))
                max_value = st.number_input("Range maximum", value=float(edit_row["max_value"] or 0))
                rating_max = st.number_input("Rating maximum", value=float(edit_row["rating_max"] or 0))
                max_points = st.number_input("Maximum points", value=float(edit_row["max_points"]))
                c1, c2 = st.columns(2)
                start = c1.date_input("Start date", value=_safe_date(edit_row["start_date"]))
                end = c2.date_input("End date (optional)", value=_safe_date(edit_row["end_date"]))
                save_col, cancel_col = st.columns(2)
                save_clicked = save_col.form_submit_button("Save changes", type="primary")
                cancel_clicked = cancel_col.form_submit_button("Cancel")
                if save_clicked:
                    con = get_connection()
                    con.execute("""
                        UPDATE scoring_rules
                        SET scoring_method = ?, target_value = ?, min_value = ?,
                            max_value = ?, rating_max = ?, max_points = ?,
                            start_date = ?, end_date = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE rule_id = ?
                    """, [method, target, min_value, max_value, rating_max,
                          max_points, start, end, int(edit_row["rule_id"])])
                    con.close()
                    st.session_state.edit_rule_id = None
                    st.rerun()
                if cancel_clicked:
                    st.session_state.edit_rule_id = None
                    st.rerun()

        if metrics.empty:
            st.info("Create at least one metric before creating a scoring rule.")
        else:
            metric_options = {
                f"{r.metric_id} — {r.name}": (int(r.metric_id), r.unit)
                for r in metrics.itertuples()
            }
            category_options = {"None": None}
            category_options.update({
                f"{r.category_id} — {r.name}": int(r.category_id)
                for r in categories.itertuples()
            })

            with st.form("add_scoring_rule"):
                metric_label = st.selectbox("Metric", list(metric_options))
                method = st.selectbox(
                    "Scoring method",
                    list(SCORING_METHODS.keys()),
                    format_func=lambda x: SCORING_METHODS[x],
                )
                target = st.number_input(
                    "Target",
                    min_value=0.0,
                    value=1.0,
                    step=0.1,
                    help="Used by Target Attainment, At-Most Target and Frequency.",
                )
                max_points = st.number_input(
                    "Maximum points",
                    min_value=0.0,
                    value=10.0,
                    step=1.0,
                )

                c3, c4 = st.columns(2)
                min_value = c3.number_input(
                    "Range minimum",
                    min_value=0.0,
                    value=0.0,
                    step=0.1,
                    help="Used by Optimal Range.",
                )
                max_value = c4.number_input(
                    "Range maximum",
                    min_value=0.0,
                    value=1.0,
                    step=0.1,
                    help="Used by Optimal Range.",
                )

                rating_max = st.number_input(
                    "Rating maximum",
                    min_value=0.0,
                    value=10.0,
                    step=1.0,
                    help="Used by Rating.",
                )

                c5, c6 = st.columns(2)
                start = c5.date_input("Start date", value=date.today())
                end = c6.date_input("End date (optional)", value=None)

                st.markdown("**Preview**")
                preview_actual = st.number_input(
                    "Example actual value",
                    min_value=0.0,
                    value=0.0,
                    step=0.1,
                )

                preview = None
                try:
                    preview = calculate_score(
                        method,
                        preview_actual,
                        target=target,
                        min_value=min_value,
                        max_value=max_value,
                        rating_max=rating_max,
                        max_points=max_points,
                    )
                except ValueError as exc:
                    st.warning(str(exc))

                if preview:
                    p1, p2, p3 = st.columns(3)
                    p1.metric("Achievement", f"{preview['achievement_pct']:.1f}%")
                    p2.metric("Points", f"{preview['points']:.1f} / {max_points:.1f}")
                    p3.metric("Status", preview["status"])

                submitted = st.form_submit_button("Save scoring rule")

                if submitted:
                    if end and end < start:
                        st.error("End date cannot be before start date.")
                    elif method == "RANGE" and min_value > max_value:
                        st.error("Range minimum cannot exceed range maximum.")
                    elif method in {"TARGET", "AT_MOST", "FREQUENCY"} and target <= 0:
                        st.error(f"{SCORING_METHODS[method]} requires a positive target.")
                    elif method == "RATING" and rating_max <= 0:
                        st.error("Rating requires a positive rating maximum.")
                    elif max_points < 0:
                        st.error("Maximum points cannot be negative.")
                    else:
                        con = get_connection()
                        try:
                            rule_id = next_id(con, "scoring_rules", "rule_id")
                            metric_id = metric_options[metric_label][0]

                            con.execute("""
                                INSERT INTO scoring_rules (
                                    rule_id, metric_id, scoring_method,
                                    target_value, min_value, max_value, rating_max,
                                    max_points, start_date, end_date
                                )
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, [
                                rule_id,
                                metric_id,
                                method,
                                target if method in {"TARGET", "AT_MOST", "FREQUENCY"} else None,
                                min_value if method == "RANGE" else None,
                                max_value if method == "RANGE" else None,
                                rating_max if method == "RATING" else None,
                                max_points,
                                start,
                                end,
                            ])
                            st.success("Scoring rule saved.")
                        except Exception as exc:
                            st.error(f"Could not save scoring rule: {exc}")
                        finally:
                            con.close()


elif page == "XP & Categories":
    st.header("⭐ XP & Categories")
    st.caption("Configure XP generation for each metric and progression for each life category.")

    tab_metric, tab_category = st.tabs(["Metric XP", "Category Progression"])

    # -------------------------
    # Metric-level XP
    # -------------------------
    with tab_metric:
        st.subheader("Metric XP")
        st.caption("Define how much XP each metric can generate from its performance score.")

        con = get_connection()
        metrics_xp = con.execute("""
            SELECT
                m.metric_id,
                m.name,
                m.metric_kind,
                c.name AS category,
                COALESCE(x.base_xp, 10.0) AS base_xp,
                COALESCE(x.xp_method, 'PROPORTIONAL') AS xp_method,
                COALESCE(x.multiplier, 1.0) AS multiplier,
                x.daily_cap
            FROM metrics m
            LEFT JOIN categories c ON c.category_id = m.category_id
            LEFT JOIN metric_xp_config x ON x.metric_id = m.metric_id
            ORDER BY m.name
        """).df()
        con.close()

        if metrics_xp.empty:
            st.info("Create metrics first, then configure their XP.")
        else:
            st.dataframe(metrics_xp, use_container_width=True, hide_index=True)

            st.markdown("**Edit metric XP**")
            metric_labels = {
                f"{r.metric_id} — {r.name}": int(r.metric_id)
                for r in metrics_xp.itertuples()
            }
            selected_metric_label = st.selectbox(
                "Metric",
                list(metric_labels),
                key="xp_metric_selector",
            )
            selected_metric_id = metric_labels[selected_metric_label]
            row = metrics_xp[metrics_xp["metric_id"] == selected_metric_id].iloc[0]

            with st.form("metric_xp_form"):
                base_xp = st.number_input(
                    "Base XP",
                    min_value=0.0,
                    value=float(row["base_xp"]),
                    step=1.0,
                    help="Maximum/base XP awarded when the metric is fully achieved.",
                )
                xp_methods = {
                    "PROPORTIONAL": "Proportional to performance",
                    "FIXED": "Fixed XP on successful completion",
                }
                xp_method = st.selectbox(
                    "XP method",
                    list(xp_methods),
                    index=list(xp_methods).index(row["xp_method"])
                    if row["xp_method"] in xp_methods else 0,
                    format_func=lambda x: xp_methods[x],
                )
                multiplier = st.number_input(
                    "XP multiplier",
                    min_value=0.0,
                    value=float(row["multiplier"]),
                    step=0.1,
                )
                cap_enabled = row["daily_cap"] is not None
                daily_cap = st.number_input(
                    "Daily XP cap",
                    min_value=0.0,
                    value=float(row["daily_cap"] or base_xp),
                    step=1.0,
                    disabled=not cap_enabled,
                )
                enable_cap = st.checkbox("Enable daily XP cap", value=cap_enabled)

                save_col, cancel_col = st.columns(2)
                save_clicked = save_col.form_submit_button("Save changes", type="primary")
                cancel_clicked = cancel_col.form_submit_button("Cancel")

                if save_clicked:
                    con = get_connection()
                    con.execute("""
                        INSERT INTO metric_xp_config (
                            metric_id, base_xp, xp_method, multiplier,
                            daily_cap, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT (metric_id) DO UPDATE SET
                            base_xp = excluded.base_xp,
                            xp_method = excluded.xp_method,
                            multiplier = excluded.multiplier,
                            daily_cap = excluded.daily_cap,
                            updated_at = excluded.updated_at
                    """, [
                        selected_metric_id,
                        float(base_xp),
                        xp_method,
                        float(multiplier),
                        float(daily_cap) if enable_cap else None,
                        pd.Timestamp.now(),
                    ])
                    con.close()
                    st.success("Metric XP configuration saved.")
                    st.rerun()

                if cancel_clicked:
                    st.rerun()

    # -------------------------
    # Category-level progression
    # -------------------------
    with tab_category:
        st.subheader("Category Progression")
        st.caption("Each category has its own XP total, current level and level progression.")

        con = get_connection()
        categories_xp = con.execute("""
            SELECT
                c.category_id,
                c.name,
                COALESCE(x.current_level, 1) AS current_level,
                COALESCE(x.current_xp, 0.0) AS current_xp,
                COALESCE(x.progression_method, 'INCREASING') AS progression_method,
                COALESCE(x.base_xp, 100.0) AS base_xp,
                COALESCE(x.growth_rate, 1.25) AS growth_rate
            FROM categories c
            LEFT JOIN category_xp_config x ON x.category_id = c.category_id
            ORDER BY c.name
        """).df()
        con.close()

        if categories_xp.empty:
            st.info("Create categories first, then configure their progression.")
        else:
            st.dataframe(categories_xp, use_container_width=True, hide_index=True)

            category_labels = {
                f"{r.category_id} — {r.name}": int(r.category_id)
                for r in categories_xp.itertuples()
            }
            selected_category_label = st.selectbox(
                "Category",
                list(category_labels),
                key="xp_category_selector",
            )
            selected_category_id = category_labels[selected_category_label]
            row = categories_xp[
                categories_xp["category_id"] == selected_category_id
            ].iloc[0]

            with st.form("category_xp_form"):
                c1, c2 = st.columns(2)
                current_level = c1.number_input(
                    "Current level",
                    min_value=1,
                    value=int(row["current_level"]),
                    step=1,
                )
                current_xp = c2.number_input(
                    "Current XP",
                    min_value=0.0,
                    value=float(row["current_xp"]),
                    step=10.0,
                )

                progression_methods = {
                    "LINEAR": "Linear",
                    "INCREASING": "Increasing",
                    "EXPONENTIAL": "Exponential",
                }
                progression_method = st.selectbox(
                    "Progression method",
                    list(progression_methods),
                    index=list(progression_methods).index(row["progression_method"])
                    if row["progression_method"] in progression_methods else 1,
                    format_func=lambda x: progression_methods[x],
                )

                c1, c2 = st.columns(2)
                base_xp = c1.number_input(
                    "Base XP required for next level",
                    min_value=1.0,
                    value=float(row["base_xp"]),
                    step=10.0,
                )
                growth_rate = c2.number_input(
                    "Growth rate",
                    min_value=1.0,
                    value=float(row["growth_rate"]),
                    step=0.05,
                )

                save_col, cancel_col = st.columns(2)
                save_clicked = save_col.form_submit_button("Save changes", type="primary")
                cancel_clicked = cancel_col.form_submit_button("Cancel")

                if save_clicked:
                    if progression_method == "EXPONENTIAL" and growth_rate <= 1:
                        st.error("Exponential growth rate must be greater than 1.")
                    else:
                        con = get_connection()
                        con.execute("""
                            INSERT INTO category_xp_config (
                                category_id, current_level, current_xp,
                                progression_method, base_xp, growth_rate, updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT (category_id) DO UPDATE SET
                                current_level = excluded.current_level,
                                current_xp = excluded.current_xp,
                                progression_method = excluded.progression_method,
                                base_xp = excluded.base_xp,
                                growth_rate = excluded.growth_rate,
                                updated_at = excluded.updated_at
                        """, [
                            selected_category_id,
                            int(current_level),
                            float(current_xp),
                            progression_method,
                            float(base_xp),
                            float(growth_rate),
                            pd.Timestamp.now(),
                        ])
                        con.close()
                        st.success("Category progression saved.")
                        st.rerun()

                if cancel_clicked:
                    st.rerun()

elif page == "Import History":
    st.header("🕘 Import History")
    df = get_import_history()
    if df.empty:
        st.info("No imports yet.")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)

elif page == "Progress":
    st.header("📈 Progress")

    st.write(
        "Calculate and inspect Metric performance, "
        "XP, and Category progression."
    )

    col1, col2 = st.columns(2)

    with col1:
        calculation_start = st.date_input(
            "Start date",
            value=date.today() - timedelta(days=7),
            key="calculation_start_date",
        )

    with col2:
        calculation_end = st.date_input(
            "End date",
            value=date.today(),
            key="calculation_end_date",
        )

    if calculation_start > calculation_end:
        st.error("Start date must be before or equal to End date.")
    else:

        calculate_button = st.button(
            "Calculate progress",
            type="primary",
            key="calculate_progress_button",
        )

        if calculate_button:

            try:
                with st.spinner("Calculating progress..."):

                    result = calculate_and_persist_metric_xp(
                        start_date=calculation_start,
                        end_date=calculation_end,
                    )

                    category_result = (
                        calculate_category_levels()
                    )

                st.session_state[
                    "calculation_result"
                ] = result

                st.session_state[
                    "category_result"
                ] = category_result

                st.success(
                    "Progress calculated successfully."
                )

            except Exception as exc:

                st.error(
                    f"Could not calculate progress: {exc}"
                )

        # ------------------------------------------------------------
        # Display last calculated results.
        # ------------------------------------------------------------

        metric_result = st.session_state.get(
            "calculation_result"
        )

        category_result = st.session_state.get(
            "category_result"
        )

        if metric_result is not None:

            st.subheader("Metric Performance")

            display_columns = [
                "activity_date",
                "metric_name",
                "value",
                "performance_ratio",
                "performance_points",
                "xp_amount",
                "status",
            ]

            available_columns = [
                column
                for column in display_columns
                if column in metric_result.columns
            ]

            st.dataframe(
                metric_result[
                    available_columns
                ],
                use_container_width=True,
                hide_index=True,
            )

            scored_count = (
            metric_result["status"]
            == "SCORED"
        ).sum()

        xp_count = (
            metric_result["status"]
            == "XP_CALCULATED"
        ).sum()

        total_xp = (
            metric_result["xp_amount"]
            .fillna(0)
            .sum()
        )

        c1, c2, c3 = st.columns(3)

        c1.metric(
            "Metrics scored",
            int(scored_count),
        )

        c2.metric(
            "XP awards",
            int(xp_count),
        )

        c3.metric(
            "XP earned",
            f"{total_xp:.1f}",
        )

        if category_result is not None:

            st.subheader("Category Progression")

            category_columns = [
                "category_name",
                "total_xp",
                "current_level",
                "current_level_xp",
                "next_level_xp",
                "xp_to_next_level",
                "progress_ratio",
            ]

            available_category_columns = [
                column
                for column in category_columns
                if column in category_result.columns
            ]

            st.dataframe(
                category_result[
                    available_category_columns
                ],
                use_container_width=True,
                hide_index=True,
            )
    
