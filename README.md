# HROne Employee Attendance & Analytics API

A FastAPI application for employee management, attendance tracking, HR regularization, and MongoDB-backed analytics. The implementation follows [`openapi.yaml`](openapi.yaml) and the storage design in [`DATA_MODEL.md`](DATA_MODEL.md).

## Features

- Create employees and list them with department filtering, pagination, and stable sorting.
- Record employee punch-in and punch-out times using IST-aware calculations.
- Support overnight shifts, work hours, overtime, late arrivals, and half-day calculations.
- Regularize attendance records with recomputed fields and an append-only audit history.
- Generate monthly employee summaries, department summaries, late-arrival leaderboards, and daily attendance trends.
- Inspect MongoDB query execution statistics through explain endpoints.
- Enforce database uniqueness using MongoDB indexes and return appropriate validation and error responses.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer

## Setup on Windows

Run all commands from the repository root.

Create and activate a virtual environment:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Create your local environment file:

```powershell
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
}
```

Configure `MONGO_URI` and `MONGO_DB` in `.env`. For a local MongoDB instance, the example configuration uses `mongodb://localhost:27017` and `attendance_db`. Environment variables already set in the process take precedence over values loaded from `.env`.

Ensure MongoDB is running, then start the API:

```powershell
python -m uvicorn app.main:app --reload --port 8000
```

The health endpoint is available at `http://127.0.0.1:8000/health`. Interactive API documentation is available at `http://127.0.0.1:8000/docs`.

## Running Tests

Run the automated test suite from the repository root:

```powershell
python -m pytest -q
```

The tests use a PID-specific MongoDB database and remove that test database during teardown. Use a disposable MongoDB instance or test deployment, not a production database.

**Warning:** `sample_seed.py` deletes and reloads the application's employee and attendance collections. Run it only against a disposable database.

## API Endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | Check API and database readiness |
| POST | `/employees` | Create an employee |
| GET | `/employees` | List and filter employees |
| POST | `/attendance/punch-in` | Record punch-in |
| POST | `/attendance/punch-out` | Record punch-out |
| GET | `/attendance` | List and filter attendance |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize attendance |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly employee analytics |
| GET | `/analytics/departments/summary` | Department-level summary |
| GET | `/analytics/leaderboard/late` | Late-arrival leaderboard |
| GET | `/analytics/departments/{department}/trend` | Daily department trend |
| GET | `/admin/explain/{endpoint}` | Inspect query execution statistics |

## Limitations

The assignment implementation does not include authentication, authorization, or a holiday calendar. Department trend requests are limited to 92 days. Explain endpoints expose database execution statistics and should be restricted to trusted operators in production. At larger scales, department trends may benefit from precomputed daily rollups.