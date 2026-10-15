# HROne Employee Attendance API

FastAPI service for employee records, attendance punches, HR regularization, and MongoDB-backed attendance analytics. The API follows [`openapi.yaml`](openapi.yaml); employee and attendance storage follows [`DATA_MODEL.md`](DATA_MODEL.md).

## Features

- Employee creation, exact department filtering, stable sorting, and bounded pagination.
- IST-aware punch-in/out for regular and overnight shifts, with unique employee/day records.
- Manual attendance corrections with recomputed fields and append-only audit history.
- Monthly employee summaries, department summaries, a tie-aware late leaderboard, and gap-filled daily trends.
- MongoDB query-plan explanations for the supported list and analytics operations.
- Internal MongoDB identifiers are not returned by the API.

## Prerequisites

- Python 3.11 or newer.
- MongoDB 6.0 or newer. MongoDB 6+ aggregation operators are used by the analytics pipelines.

## Windows PowerShell Setup

From the repository root:

```powershell
Set-Location .\candidate_kit
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Set `MONGO_URI` and `MONGO_DB` in `.env`. For a local MongoDB instance, the example file uses `mongodb://localhost:27017` and `attendance_db`. Process environment variables take precedence over `.env` values.

Start the API from `candidate_kit/`:

```powershell
python -m uvicorn app.main:app --reload --port 8000
```

The readiness endpoint is `http://127.0.0.1:8000/health`. Interactive API documentation is at `http://127.0.0.1:8000/docs`.

## Tests

The pytest suite uses a PID-specific MongoDB database, creates its own records, and drops that test database at the end. It reads the connection URI from the application's configuration, so point it at a disposable/test MongoDB deployment when appropriate.

```powershell
Set-Location .\candidate_kit
python -m pytest -q
```

The original smoke script is at the repository root and leaves its generated records in the configured database:

```powershell
Set-Location ..
python api_testing.py
```

`sample_seed.py` deletes and reloads both collections in the configured database. Run it only against a disposable database.

## Endpoints

`GET /health`, `POST /employees`, `GET /employees`, `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`, `PATCH /attendance/{emp_code}/{date}`, `GET /analytics/employees/{emp_code}/monthly`, `GET /analytics/departments/summary`, `GET /analytics/leaderboard/late`, `GET /analytics/departments/{department}/trend`, and `GET /admin/explain/{endpoint}`.

## Limitations

There is no authentication/authorization layer or holiday calendar in this assignment. Department trend ranges are limited to 92 days. Explain runs MongoDB `executionStats` and should be restricted to trusted operators in a production deployment. Large production scale may require rollups and a trend pipeline that avoids building a department-wide employee array.
