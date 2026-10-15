# HROne Employee Attendance & Analytics API

A FastAPI application for employee management, attendance tracking, attendance regularization, and MongoDB-backed analytics. The implementation follows [`openapi.yaml`](openapi.yaml) and the storage design in [`DATA_MODEL.md`](DATA_MODEL.md).

## Features

- Create employees and list them with department filtering, pagination, and stable sorting.
- Record employee punch-in and punch-out times using IST-aware calculations.
- Support overnight shifts, work hours, overtime, late arrivals, and half-day calculations.
- Regularize attendance records with recomputed fields and an append-only audit history.
- Generate monthly employee summaries, department summaries, late-arrival leaderboards, and daily department trends.
- Inspect MongoDB query execution statistics through explain endpoints.
- Enforce database uniqueness using MongoDB indexes.
- Validate requests and return appropriate error responses.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer
- Git

## Setup on Windows

The application is inside the `candidate_kit/` directory. Open PowerShell in the repository root and run:

```powershell
cd candidate_kit
```

### 1. Create a virtual environment

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, you can use the environment's Python executable directly instead.

### 2. Install dependencies

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 3. Configure environment variables

Create a local `.env` file from the example:

```powershell
Copy-Item .env.example .env
```

Configure the following variables in `.env`:

- `MONGO_URI` — your MongoDB connection URI.
- `MONGO_DB` — the database name used by the application.

For a local MongoDB instance, the example configuration uses `mongodb://localhost:27017` and `attendance_db`.

Make sure MongoDB is running before you start the API. Do not commit your `.env` file or database credentials.

### 4. Start the API

Run this command from inside `candidate_kit/`:

```powershell
python -m uvicorn app.main:app --reload --port 8000
```

The API will be available at:

- **Health check:** http://127.0.0.1:8000/health
- **Interactive API documentation:** http://127.0.0.1:8000/docs
- **OpenAPI schema:** http://127.0.0.1:8000/openapi.json

## Running Tests

Open a second PowerShell terminal, navigate to the repository's `candidate_kit/` directory, and activate the virtual environment.

Run the automated tests:

```powershell
python -m pytest -v
```

Or run them with concise output:

```powershell
python -m pytest -q
```

The tests use a PID-specific MongoDB database and remove it during teardown. Use a disposable MongoDB instance or test deployment, not a production database.

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

Refer to [`openapi.yaml`](openapi.yaml) or visit `/docs` while the application is running for request parameters, schemas, and response details.

## Sample Data

The `sample_data/` directory contains sample employee and attendance records. The `sample_seed.py` script can be used to load sample data, subject to the database configuration in the application.

**Warning:** The seed script deletes and reloads the application's employee and attendance collections. Run it only against a disposable database.

## Project Structure

```text
candidate_kit/
├── app/
│   ├── __init__.py
│   └── main.py
├── sample_data/
│   ├── attendance_logs.json
│   └── employees.json
├── .env.example
├── .gitignore
├── DATA_MODEL.md
├── DECISIONS.md
├── openapi.yaml
├── requirements.txt
├── REVIEW.md
├── sample_seed.py
└── test_main.py
```

## Design and Review Documentation

- [`DATA_MODEL.md`](DATA_MODEL.md) — data model and storage design.
- [`DECISIONS.md`](DECISIONS.md) — implementation decisions and trade-offs.
- [`REVIEW.md`](REVIEW.md) — code review findings and fixes.
- [`openapi.yaml`](openapi.yaml) — API contract.

## Limitations

- Authentication and authorization are not implemented.
- A holiday calendar is not included.
- Department trend requests are limited to 92 days.
- Explain endpoints expose database execution statistics and should be restricted to trusted operators in production.
- At larger scales, department trends may benefit from precomputed daily rollups.
