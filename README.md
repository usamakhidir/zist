# Zist — Local Loop Importer

A local-first Streamlit application for importing Loop Habits CSV exports into DuckDB.

## Current scope

Version 0.1 intentionally does **not** implement Zist scoring.

It currently:

- accepts a Loop Habits ZIP export
- reads `Habits.csv`
- reads each habit's `Checkmarks.csv`
- normalizes observations into one DuckDB table
- preserves raw values such as `YES_MANUAL`, `NO`, and numeric values
- distinguishes missing/unknown/not-completed/measured states
- safely supports repeated uploads
- inserts new observations
- updates changed observations
- ignores unchanged observations
- stores import history
- displays habits and observations in Streamlit

## Project structure

```text
zist_local/
├── app.py
├── requirements.txt
├── README.md
├── pipeline/
│   ├── __init__.py
│   ├── db.py
│   └── ingest.py
├── database/
│   └── zist.duckdb       # generated locally
├── data/
│   ├── raw/
│   └── imports/
└── config/                    # reserved for the future rules engine
```

## Run locally on Windows

Open PowerShell in this folder.

### 1. Create a virtual environment

```powershell
py -m venv .venv
```

### 2. Activate it

```powershell
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, use:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
```

### 3. Install packages

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 4. Start Streamlit

```powershell
python -m streamlit run app.py
```

The browser should open the local Streamlit page.

## Importing Loop data

Use:

**Import Loop ZIP → Choose file → Import / Update**

You can upload the same Loop ZIP repeatedly.

The observation key is:

```text
activity_date + habit_id
```

Therefore:

- same date + same habit + same value = unchanged
- same date + same habit + changed value = updated
- new date + habit = inserted

The raw Loop export remains the external source; DuckDB contains the normalized local copy.

## Important design decision

No scoring logic is hard-coded yet.

The future configuration layer will define:

- categories
- metrics
- habit-to-metric mappings
- targets
- scoring rules
- XP rules
- effective start/end dates

This keeps the raw Loop data independent from the eventual Zist rules.


## Import transaction handling

Imports use an explicit DuckDB transaction. If an import fails, partial changes are rolled back and the underlying error is shown instead of a secondary rollback error.

