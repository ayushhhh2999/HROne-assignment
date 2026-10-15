import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from os import getpid

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import PyMongoError

TEST_DB = f"hrone_api_test_{getpid()}"
os.environ["MONGO_DB"] = TEST_DB

from app import main as api


@pytest.fixture(autouse=True)
def clean_test_collections():
    api.employees.delete_many({})
    api.attendance.delete_many({})
    yield


@pytest.fixture
def client(clean_test_collections):
    with TestClient(api.app) as test_client:
        yield test_client


@pytest.fixture(scope="session", autouse=True)
def drop_test_database():
    yield
    api.client.drop_database(TEST_DB)
    api.client.close()


def employee(emp_code="EMP9001", department="Engineering", joined_on="2026-01-01"):
    return {
        "emp_code": emp_code,
        "name": f"Employee {emp_code}",
        "email": f"{emp_code.lower()}@example.com",
        "department": department,
        "joined_on": joined_on,
    }


def epoch_ms(day, hour, minute):
    return int(datetime(day[0], day[1], day[2], hour, minute, tzinfo=timezone.utc).timestamp() * 1000)


def test_employee_create_duplicate_filter_and_pagination(client):
    invalid_employee = employee("wrong-code")
    assert client.post("/employees", json=invalid_employee).status_code == 422
    assert client.post("/employees", json=employee()).status_code == 201
    assert client.post("/employees", json=employee()).status_code == 409
    client.post("/employees", json=employee("EMP9002", "Sales"))
    client.post("/employees", json=employee("EMP9003", "Engineering"))

    page = client.get("/employees?department=Engineering&page=2&page_size=1").json()
    assert page["total"] == 2
    assert page["page"] == 2
    assert [item["emp_code"] for item in page["items"]] == ["EMP9003"]
    assert all("_id" not in item for item in page["items"])
    assert client.get("/employees?department=").json()["total"] == 0


def test_concurrent_duplicate_punch_in_is_unique(client):
    client.post("/employees", json=employee())
    payload = {"emp_code": "EMP9001", "punched_at": epoch_ms((2026, 10, 15), 4, 0)}

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: client.post("/attendance/punch-in", json=payload), range(8)))

    assert [response.status_code for response in responses].count(201) == 1
    assert [response.status_code for response in responses].count(409) == 7


def test_punch_out_sequence_and_derived_values(client):
    assert client.post("/attendance/punch-out", json={"emp_code": "UNKNOWN"}).status_code == 404
    assert client.post("/attendance/punch-in", json={"emp_code": "UNKNOWN"}).status_code == 404
    client.post("/employees", json=employee())
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP9001"}).status_code == 404
    punch_in = epoch_ms((2026, 10, 15), 4, 35)
    punch_out = epoch_ms((2026, 10, 15), 13, 0)
    created = client.post("/attendance/punch-in", json={"emp_code": "EMP9001", "punched_at": punch_in})
    assert created.status_code == 201
    assert created.json()["late_minutes"] == 35
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP9001", "punched_at": punch_in}).status_code == 409

    closed = client.post("/attendance/punch-out", json={"emp_code": "EMP9001", "punched_at": punch_out})
    assert closed.status_code == 200
    assert closed.json()["work_hours"] == 8.42
    assert closed.json()["history"] == []
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP9001", "punched_at": punch_out + 60_000}).status_code == 409


def test_attendance_filters_and_bad_dates(client):
    client.post("/employees", json=employee())
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP9001", "punched_at": epoch_ms((2026, 10, 15), 4, 0), "status": "WFH",
    })

    found = client.get("/attendance?status=WFH&date_from=2026-10-15&date_to=2026-10-15").json()
    assert found["total"] == 1
    assert found["items"][0]["emp_code"] == "EMP9001"
    assert client.get("/attendance?date_from=not-a-date").status_code == 422
    assert client.get("/attendance?date_from=").status_code == 422
    assert client.get("/attendance?date_from=2026-10-16&date_to=2026-10-15").status_code == 422
    assert client.patch("/attendance/EMP9001/not-a-date", json={"reason": "valid reason", "regularized_by": "hr"}).status_code == 422


def test_regularization_appends_auditable_changes(client):
    client.post("/employees", json=employee())
    original_in = epoch_ms((2026, 10, 15), 4, 35)
    original_out = epoch_ms((2026, 10, 15), 13, 0)
    client.post("/attendance/punch-in", json={"emp_code": "EMP9001", "punched_at": original_in})
    client.post("/attendance/punch-out", json={"emp_code": "EMP9001", "punched_at": original_out})

    corrected_in = epoch_ms((2026, 10, 15), 3, 58)
    response = client.patch("/attendance/EMP9001/2026-10-15", json={
        "punch_in": corrected_in, "reason": "biometric correction", "regularized_by": "hr.admin",
        "late_minutes": 0,
    })
    assert response.status_code == 200
    record = response.json()
    assert record["punch_in"] == corrected_in
    assert record["late_minutes"] == 0
    assert record["history"][0]["by"] == "hr.admin"
    assert record["history"][0]["changes"]["punch_in"] == {"from": original_in, "to": corrected_in}
    assert "late_minutes" in record["history"][0]["changes"]
    second = client.patch("/attendance/EMP9001/2026-10-15", json={
        "status": "WFH", "reason": "work location update", "regularized_by": "hr.admin",
    })
    assert second.status_code == 200
    assert len(second.json()["history"]) == 2
    assert second.json()["history"][0]["changes"]["punch_in"]["from"] == original_in
    assert client.patch("/attendance/EMP9001/2026-10-15", json={
        "reason": "no changes", "regularized_by": "hr.admin",
    }).status_code == 422


def test_monthly_summary_and_department_zero_log_headcount(client):
    client.post("/employees", json=employee("EMP9001", "Engineering", "2026-10-15"))
    client.post("/employees", json=employee("EMP9002", "Engineering", "2026-10-15"))
    api.attendance.insert_one({
        "emp_code": "EMP9001", "date": "2026-10-15", "status": "PRESENT",
        "punch_in": datetime(2026, 10, 15, 4, 0, tzinfo=timezone.utc), "punch_out": None,
        "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": True,
    })

    monthly = client.get("/analytics/employees/EMP9001/monthly?month=2026-10")
    assert monthly.status_code == 200
    assert monthly.json()["working_days"] == 12
    assert monthly.json()["present_days"] == 0.5
    summary = client.get("/analytics/departments/summary?month=2026-10").json()["items"]
    assert summary == [{
        "department": "Engineering", "headcount": 2, "present_days": 0.5,
        "avg_work_hours": None, "late_count": 0, "total_late_minutes": 0,
        "leave_count": 0, "on_duty_count": 0,
    }]
    zero_logs = client.get("/analytics/employees/EMP9002/monthly?month=2026-10").json()
    assert zero_logs["present_days"] == 0
    assert zero_logs["attendance_pct"] == 0.0
    assert client.get("/analytics/employees/EMP9002/monthly?month=2026-13").status_code == 422


def test_leaderboard_ties_and_empty_trend(client):
    for code in ("EMP9001", "EMP9002"):
        client.post("/employees", json=employee(code, "Engineering", "2026-01-01"))
        api.attendance.insert_one({
            "emp_code": code, "date": "2026-10-15", "status": "PRESENT",
            "punch_in": None, "punch_out": None, "work_hours": None,
            "late_minutes": 35, "overtime_minutes": 0, "half_day": False,
        })
    client.post("/employees", json=employee("EMP9003", "Quiet", "2026-01-01"))

    tied = client.get("/analytics/leaderboard/late?month=2026-10&limit=1").json()["items"]
    assert [(item["rank"], item["emp_code"]) for item in tied] == [(1, "EMP9001"), (1, "EMP9002")]
    trend = client.get("/analytics/departments/Quiet/trend?from=2026-10-15&to=2026-10-17").json()["items"]
    assert len(trend) == 3
    assert [row["date"] for row in trend] == ["2026-10-15", "2026-10-16", "2026-10-17"]
    assert trend[0]["attendance_rate"] == 0.0
    assert trend[2]["attendance_rate"] is None


def test_monthly_explain_uses_employee_collection(client, monkeypatch):
    seen = {}

    def capture(collection, pipeline):
        seen["collection"] = collection.name
        seen["pipeline"] = pipeline
        return {"ok": 1}

    monkeypatch.setattr(api, "explain_collection_pipeline", capture)
    response = client.get("/admin/explain/employee_monthly?emp_code=EMP9001&month=2026-10")
    assert response.status_code == 200
    assert response.json()["collection"] == "employees"
    assert seen["collection"] == "employees"
    assert client.get("/admin/explain/employee_monthly?emp_code=EMP9001&month=bad").status_code == 422


def test_database_errors_are_sanitized(client, monkeypatch):
    class FailingEmployees:
        def count_documents(self, query):
            raise PyMongoError("private connection details")

    monkeypatch.setattr(api, "employees", FailingEmployees())
    response = client.get("/employees")
    assert response.status_code == 503
    assert response.json() == {"detail": "MongoDB operation failed"}