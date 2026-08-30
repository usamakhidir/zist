from pathlib import Path
import duckdb

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "database" / "zist.duckdb"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS imports (
    import_id BIGINT PRIMARY KEY,
    imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    source_filename VARCHAR,
    source_hash VARCHAR,
    inserted_count INTEGER DEFAULT 0,
    updated_count INTEGER DEFAULT 0,
    unchanged_count INTEGER DEFAULT 0,
    min_date DATE,
    max_date DATE
);

CREATE TABLE IF NOT EXISTS habits (
    habit_id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    position VARCHAR,
    habit_type VARCHAR,
    question VARCHAR,
    description VARCHAR,
    frequency_numerator INTEGER,
    frequency_denominator INTEGER,
    color VARCHAR,
    unit VARCHAR,
    target_type VARCHAR,
    target_value DOUBLE,
    archived BOOLEAN,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS observations (
    activity_date DATE NOT NULL,
    habit_id VARCHAR NOT NULL,
    raw_value VARCHAR,
    numeric_value DOUBLE,
    value_status VARCHAR,
    notes VARCHAR,
    source_file VARCHAR,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (activity_date, habit_id)
);


CREATE TABLE IF NOT EXISTS categories (
    category_id BIGINT PRIMARY KEY,
    name VARCHAR NOT NULL UNIQUE,
    description VARCHAR,
    start_date DATE,
    end_date DATE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS metrics (
    metric_id BIGINT PRIMARY KEY,
    name VARCHAR NOT NULL UNIQUE,
    measurement_type VARCHAR NOT NULL,
    unit VARCHAR,
    description VARCHAR,
    metric_kind VARCHAR NOT NULL DEFAULT 'RAW',
    formula VARCHAR,
    category_id BIGINT,
    start_date DATE,
    end_date DATE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);


CREATE TABLE IF NOT EXISTS metric_formula_inputs (
    formula_input_id BIGINT PRIMARY KEY,
    derived_metric_id BIGINT NOT NULL,
    source_metric_id BIGINT NOT NULL,
    variable_name VARCHAR NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS metric_habit_mapping (
    mapping_id BIGINT PRIMARY KEY,
    metric_id BIGINT NOT NULL,
    habit_id VARCHAR NOT NULL,
    weight DOUBLE DEFAULT 1.0,
    start_date DATE,
    end_date DATE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scoring_rules (
    rule_id BIGINT PRIMARY KEY,
    metric_id BIGINT NOT NULL,
    scoring_method VARCHAR NOT NULL,
    target_value DOUBLE,
    min_value DOUBLE,
    max_value DOUBLE,
    rating_max DOUBLE,
    max_points DOUBLE NOT NULL DEFAULT 10,
    start_date DATE,
    end_date DATE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);


CREATE TABLE IF NOT EXISTS metric_xp_config (
    metric_id BIGINT PRIMARY KEY,
    base_xp DOUBLE NOT NULL DEFAULT 10,
    xp_method VARCHAR NOT NULL DEFAULT 'PROPORTIONAL',
    multiplier DOUBLE NOT NULL DEFAULT 1,
    daily_cap DOUBLE,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS category_xp_config (
    category_id BIGINT PRIMARY KEY,
    current_level INTEGER NOT NULL DEFAULT 1,
    current_xp DOUBLE NOT NULL DEFAULT 0,
    progression_method VARCHAR NOT NULL DEFAULT 'INCREASING',
    base_xp DOUBLE NOT NULL DEFAULT 100,
    growth_rate DOUBLE NOT NULL DEFAULT 1.25,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS xp_ledger (
    xp_entry_id BIGINT PRIMARY KEY,
    entry_date DATE NOT NULL,
    source_type VARCHAR NOT NULL,
    source_id BIGINT,
    metric_id BIGINT,
    category_id BIGINT,
    performance_points DOUBLE,
    xp_amount DOUBLE NOT NULL,
    description VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE OR REPLACE VIEW habit_summary AS
SELECT
    h.habit_id,
    h.name,
    h.habit_type,
    h.question,
    h.unit,
    h.target_type,
    h.target_value,
    h.frequency_numerator,
    h.frequency_denominator,
    h.archived,
    MIN(o.activity_date) AS first_observation,
    MAX(o.activity_date) AS last_observation,
    COUNT(o.activity_date) AS observation_count
FROM habits h
LEFT JOIN observations o ON o.habit_id = h.habit_id
GROUP BY ALL;
"""

def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(DB_PATH))

def initialize_database():
    con = get_connection()
    con.execute(SCHEMA_SQL)
    con.close()

def get_import_history():
    con = get_connection()
    df = con.execute("""
        SELECT
            imported_at,
            source_filename,
            source_hash,
            inserted_count,
            updated_count,
            unchanged_count,
            min_date,
            max_date
        FROM imports
        ORDER BY imported_at DESC
    """).df()
    con.close()
    return df


def next_id(con, table_name: str, id_column: str) -> int:
    # Table/column names are internal constants supplied by this application.
    return int(con.execute(
        f"SELECT COALESCE(MAX({id_column}), 0) + 1 FROM {table_name}"
    ).fetchone()[0])
