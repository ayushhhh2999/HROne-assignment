"""Employee Attendance & Analytics API (HROne candidate assignment).

Run from the repository root: uvicorn app.main:app --port 8000
Configuration: MONGO_URI and MONGO_DB environment variables (or local .env).
All analytics are performed by MongoDB aggregation pipelines.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from fastapi.responses import JSONResponse

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, tz_aware=True)
db = client[MONGO_DB]
employees = db["employees"]
attendance = db["attendance_logs"]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = (*PRESENCE, "ABSENT", "LEAVE")
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.exception_handler(PyMongoError)
async def mongo_error_handler(request: Request, exc: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "MongoDB operation failed"})


# ------------------------------- validation ---------------------------------
def valid_date(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("date must be YYYY-MM-DD")
    return value


def valid_month(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("month must be YYYY-MM")

    year, month = map(int, value.split("-"))
    # The analytics pipelines need a representable start and exclusive end date.
    # Year 0000 is invalid, and 9999-12 cannot form the next-month boundary.
    if year == 0 or (year == 9999 and month == 12):
        raise ValueError("month is outside the supported calendar range")

    return value


def request_date(value: str) -> str:
    try:
        return valid_date(value)
    except ValueError as exc:
        raise HTTPException(422, "date must be YYYY-MM-DD") from exc


def request_month(value: str) -> str:
    try:
        return valid_month(value)
    except ValueError as exc:
        raise HTTPException(422, "month must be YYYY-MM") from exc


def valid_clock(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("time must be HH:MM")
    return value


def epoch_ms_to_dt(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise HTTPException(422, "timestamps must be integer epoch milliseconds in the supported range")
    # Contract truncates to whole seconds before storing/returning.
    return datetime.fromtimestamp(value // 1000, tz=UTC)


def dt_to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)  # PyMongo may return naive UTC datetimes.
    return int(value.timestamp()) * 1000


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def as_ist(value: datetime) -> datetime:
    return as_utc(value).astimezone(IST)


def month_bounds(month: str) -> tuple[str, str, date, date]:
    valid_month(month)
    year, mon = map(int, month.split("-"))
    start = date(year, mon, 1)
    end_exclusive = date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1)
    return start.isoformat(), end_exclusive.isoformat(), start, end_exclusive - timedelta(days=1)


def round_half_up(value: float | Decimal, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def shift_datetimes(attendance_date: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    day = date.fromisoformat(attendance_date)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    start = datetime.combine(day, time(sh, sm), tzinfo=IST)
    end_day = day + timedelta(days=1) if shift_end <= shift_start else day
    end = datetime.combine(end_day, time(eh, em), tzinfo=IST)
    return start, end


def attendance_date_for(punch_in: datetime, emp: dict) -> str:
    local = as_ist(punch_in)
    if emp["shift_end"] <= emp["shift_start"] and local.strftime("%H:%M") < emp["shift_end"]:
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def compute_late_minutes(punch_in: Optional[datetime], shift_start: str, attendance_date: Optional[str] = None) -> int:
    if punch_in is None:
        return 0
    if attendance_date is None:
        attendance_date = as_ist(punch_in).date().isoformat()
    sh, sm = map(int, shift_start.split(":"))
    shift_start_dt = datetime.combine(date.fromisoformat(attendance_date), time(sh, sm), tzinfo=IST)
    seconds = int((as_utc(punch_in) - shift_start_dt.astimezone(UTC)).total_seconds())
    # Strictly more than ten minutes; minutes are measured from shift start.
    if seconds <= 600:
        return 0
    return max(0, seconds // 60)


def compute_work_hours(punch_in: Optional[datetime], punch_out: Optional[datetime]) -> Optional[float]:
    if punch_in is None or punch_out is None:
        return None
    seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_overtime(punch_out: Optional[datetime], shift_end: str, attendance_date: str, shift_start: str = "09:30") -> int:
    if punch_out is None:
        return 0
    _, end = shift_datetimes(attendance_date, shift_start, shift_end)
    seconds = int((as_utc(punch_out) - end.astimezone(UTC)).total_seconds())
    minutes = seconds // 60
    return int(minutes) if minutes >= 30 else 0


def derive_fields(record: dict, emp: dict) -> dict:
    pin = as_utc(record.get("punch_in"))
    pout = as_utc(record.get("punch_out"))
    if record.get("status") in ("ABSENT", "LEAVE"):
        return {"punch_in": None, "punch_out": None, "work_hours": None,
                "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(pin, pout)
    return {
        "punch_in": pin,
        "punch_out": pout,
        "work_hours": hours,
        "late_minutes": compute_late_minutes(pin, emp["shift_start"], record["date"]),
        "overtime_minutes": compute_overtime(pout, emp["shift_end"], record["date"], emp["shift_start"]),
        "half_day": bool(hours is not None and hours < 4.50),
    }


def serialize_attendance(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k not in ("_id", "id")}
    result.setdefault("history", [])
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    result["punch_in"] = dt_to_ms(result.get("punch_in"))
    result["punch_out"] = dt_to_ms(result.get("punch_out"))
    for entry in result.get("history", []):
        entry["at"] = dt_to_ms(entry.get("at"))
        for field, change in entry.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                change["from"] = dt_to_ms(change.get("from"))
                change["to"] = dt_to_ms(change.get("to"))
    return result


def serialize_employee(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    result["created_at"] = dt_to_ms(result.get("created_at"))
    return result


def presence_value(record: dict) -> float:
    if record.get("status") not in PRESENCE:
        return 0.0
    return 0.5 if record.get("half_day", False) else 1.0


def mongo_round_expr(expression, places: int):
    # MongoDB $round is ties-to-even; round half-up by adding a tiny epsilon before rounding.
    epsilon = 0.0000000001
    return {"$round": [{"$add": [expression, epsilon]}, places]}


def build_attendance_filter(emp_code=None, date_from=None, date_to=None, status=None):
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        bounds = {}
        if date_from is not None:
            request_date(date_from)
            bounds["$gte"] = date_from
        if date_to is not None:
            request_date(date_to)
            bounds["$lte"] = date_to
        if date_from is not None and date_to is not None and date_from > date_to:
            raise HTTPException(422, "date_from must be on or before date_to")
        q["date"] = bounds
    if status is not None:
        if status not in STATUSES:
            raise HTTPException(422, "invalid status")
        q["status"] = status
    return q


# ------------------------------- Pydantic -----------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_clock(cls, v):
        return valid_clock(v)

    @field_validator("joined_on")
    @classmethod
    def validate_join_date(cls, v):
        return valid_date(v)

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, v):
        if v is not None:
            epoch_ms_to_dt(v)
        return v


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, v):
        if v is not None:
            epoch_ms_to_dt(v)
        return v


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_timestamp(cls, v):
        if v is not None:
            epoch_ms_to_dt(v)
        return v


# ------------------------------ startup/indexes ----------------------------
def ensure_indexes():
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_employee_sort")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)], name="department_joined")
    employees.create_index([("joined_on", ASCENDING)], name="employee_joined_on")
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    attendance.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="attendance_employee_date_desc")


@app.on_event("startup")
def startup():
    ensure_indexes()


# --------------------------------- health -----------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError as exc:
        raise HTTPException(503, "MongoDB is unavailable") from exc


# ------------------------------ employee CRUD -------------------------------
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = {"department": department} if department is not None else {}
    total = employees.count_documents(query)
    docs = employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in docs], "total": total, "page": page, "page_size": page_size}


# ------------------------------ attendance ----------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = epoch_ms_to_dt(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    record_date = attendance_date_for(ts, emp)
    if record_date < emp["joined_on"]:
        raise HTTPException(422, "punch-in cannot precede the employee's joining date")
    doc = {
        "emp_code": body.emp_code, "date": record_date, "status": body.status,
        "punch_in": ts, "punch_out": None, "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], record_date),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        attendance.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = epoch_ms_to_dt(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    candidate = attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not candidate:
        raise HTTPException(404, "no punch-in found")
    if candidate.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")
    pin = as_utc(candidate["punch_in"])
    seconds = int((ts - pin).total_seconds())
    if seconds <= 0 or seconds > 24 * 3600:
        raise HTTPException(422, "punch-out must be after punch-in and no more than 24 hours later")
    hours = compute_work_hours(pin, ts)
    overtime = compute_overtime(ts, emp["shift_end"], candidate["date"], emp["shift_start"])
    updated = attendance.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": hours, "overtime_minutes": overtime, "half_day": bool(hours is not None and hours < 4.5)}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record is already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
):
    query = build_attendance_filter(emp_code, date_from, date_to, status)
    total = attendance.count_documents(query)
    cursor = attendance.find(query, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_attendance(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    request_date(date)
    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    old = attendance.find_one({"emp_code": emp_code, "date": date})
    if not old:
        raise HTTPException(404, "attendance record not found")
    patch = body.model_dump(exclude_unset=True)
    reason, by = patch.pop("reason"), patch.pop("regularized_by")
    if "status" in patch and patch["status"] is None:
        raise HTTPException(422, "status cannot be null")
    if any(key in patch and patch[key] is None for key in ("punch_in", "punch_out")):
        raise HTTPException(422, "punch timestamps cannot be null when supplied")
    if patch.get("status") in ("ABSENT", "LEAVE") and ("punch_in" in patch or "punch_out" in patch):
        raise HTTPException(422, "ABSENT and LEAVE cannot be supplied with punch times")
    final = dict(old)
    if "status" in patch:
        final["status"] = patch["status"]
    for key in ("punch_in", "punch_out"):
        if key in patch:
            final[key] = epoch_ms_to_dt(patch[key]) if patch[key] is not None else None
    final["date"] = date
    if final.get("status") in ("ABSENT", "LEAVE"):
        if "punch_in" in patch and patch["punch_in"] is not None or "punch_out" in patch and patch["punch_out"] is not None:
            raise HTTPException(422, "ABSENT and LEAVE require null punch times")
        final["punch_in"] = final["punch_out"] = None
    else:
        if final.get("punch_in") is None:
            raise HTTPException(422, "presence statuses require punch_in")
        if attendance_date_for(final["punch_in"], emp) != date:
            raise HTTPException(422, "punch_in must belong to the attendance date")
        if final.get("punch_out") is not None:
            seconds = int((as_utc(final["punch_out"]) - as_utc(final["punch_in"])).total_seconds())
            if seconds <= 0 or seconds > 24 * 3600:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    derived = derive_fields(final, emp)
    final.update(derived)
    changed = {}
    for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        before, after = old.get(field), final.get(field)
        if field in ("punch_in", "punch_out"):
            before, after = as_utc(before), as_utc(after)
        if before != after:
            changed[field] = {"from": before, "to": after}
    if not changed:
        raise HTTPException(422, "regularization does not change the record")
    now = datetime.now(UTC).replace(microsecond=0)
    history_entry = {"at": now, "by": by, "reason": reason, "changes": changed}
    # Optimistic concurrency: only update if the version-like fields still match what we read.
    guard = {"emp_code": emp_code, "date": date, "status": old.get("status"), "punch_in": old.get("punch_in"), "punch_out": old.get("punch_out")}
    guard["history"] = old["history"] if "history" in old else {"$exists": False}
    update_set = {k: final.get(k) for k in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")}
    updated = attendance.find_one_and_update(guard, {"$set": update_set, "$push": {"history": history_entry}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record changed concurrently; retry the correction")
    return serialize_attendance(updated)


# ------------------------------ aggregation helpers -------------------------
# Monthly and department calculations are expressed as MongoDB aggregation pipelines.
def employee_monthly_pipeline(emp_code: str, month: str):
    start_s, end_s, month_start, month_end = month_bounds(month)
    start_dt = datetime.combine(month_start, time.min, tzinfo=UTC)
    end_dt = datetime.combine(month_end + timedelta(days=1), time.min, tzinfo=UTC)
    day_count = {"$max": [0, {"$dateDiff": {"startDate": "$_working_start", "endDate": "$_month_end_exclusive", "unit": "day"}}]}
    day_values = {"$map": {"input": {"$range": [0, day_count]}, "as": "i",
                            "in": {"$dateAdd": {"startDate": "$_working_start", "unit": "day", "amount": "$$i"}}}}
    weekday_values = {"$filter": {"input": day_values, "as": "d", "cond": {"$and": [
        {"$gte": [{"$dayOfWeek": "$$d"}, 2]}, {"$lte": [{"$dayOfWeek": "$$d"}, 6]},
    ]}}}
    logs_lookup = {"$lookup": {
        "from": "attendance_logs", "let": {"code": "$emp_code"},
        "pipeline": [{"$match": {"$expr": {"$and": [
            {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_s]}, {"$lt": ["$date", end_s]},
        ]}}}], "as": "logs",
    }}
    present_day_value = {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$cond": [
        {"$and": [
            {"$in": ["$$l.status", list(PRESENCE)]},
            {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, [2, 3, 4, 5, 6]]},
        ]}, {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1]}, 0,
    ]}}}}
    leave_count = {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$eq": ["$$l.status", "LEAVE"]}}}}
    late_count = {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}}}}
    late_total = {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$ifNull": ["$$l.late_minutes", 0]}}}}
    overtime_total = {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$ifNull": ["$$l.overtime_minutes", 0]}}}}
    return [
        {"$match": {"emp_code": emp_code}},
        {"$set": {
            "_month_end_exclusive": end_dt,
            "_working_start": {"$dateFromString": {"dateString": {"$cond": [{"$gt": ["$joined_on", start_s]}, "$joined_on", start_s]}}},
        }},
        {"$set": {"working_days": {"$size": weekday_values}}},
        logs_lookup,
        {"$project": {
            "_id": 0, "emp_code": 1, "working_days": 1,
            "present_days": present_day_value, "leave_days": leave_count, "late_count": late_count,
            "total_late_minutes": late_total, "total_overtime_minutes": overtime_total,
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    request_month(month)
    if not employees.find_one({"emp_code": emp_code}, {"_id": 1}):
        raise HTTPException(404, "employee not found")
    rows = list(employees.aggregate(employee_monthly_pipeline(emp_code, month)))
    if not rows:
        raise HTTPException(404, "employee not found")
    stats = rows[0]
    working_days = stats["working_days"]
    pct = None if working_days == 0 else round_half_up(Decimal(str(stats["present_days"])) * 100 / Decimal(working_days), 4)
    return {"emp_code": emp_code, "month": month, "working_days": working_days,
            "present_days": round_half_up(stats["present_days"], 2), "leave_days": stats["leave_days"],
            "late_count": stats["late_count"], "total_late_minutes": stats["total_late_minutes"],
            "total_overtime_minutes": stats["total_overtime_minutes"], "attendance_pct": pct}


def department_summary_pipeline(month: str, department: Optional[str] = None):
    start_s, end_s, _, month_end = month_bounds(month)
    employee_match = {"joined_on": {"$lte": month_end.isoformat()}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_s]}, {"$lt": ["$date", end_s]}]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True, "includeArrayIndex": "log_index"}},
        {"$group": {"_id": "$department", "headcount": {"$sum": {"$cond": [{"$or": [{"$eq": ["$log_index", 0]}, {"$eq": ["$log_index", None]}]}, 1, 0]}},
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "1970-01-01"]}}}}, [2,3,4,5,6]]}]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
            "work_hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, "$logs.work_hours", 0]}},
            "work_hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]}, mongo_round_expr({"$divide": ["$work_hours_sum", "$work_hours_count"]}, 2), None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    request_month(month)
    items = list(employees.aggregate(department_summary_pipeline(month, department)))
    return {"month": month, "items": items}


def leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None):
    start_s, end_s, _, _ = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": start_s, "$lt": end_s}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": 1}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    request_month(month)
    return {"month": month, "items": list(attendance.aggregate(leaderboard_pipeline(month, limit, department)))}


def trend_pipeline(department: str, from_date: str, to_date: str):
    start = datetime.combine(date.fromisoformat(from_date), time.min, tzinfo=UTC)
    days = (date.fromisoformat(to_date) - date.fromisoformat(from_date)).days + 1
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "department": {"$first": "$department"},
                    "people": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$set": {"day_list": {"$map": {
            "input": {"$range": [0, days]}, "as": "i",
            "in": {"$dateAdd": {"startDate": start, "unit": "day", "amount": "$$i"}},
        }}}},
        {"$unwind": "$day_list"},
        {"$set": {
            "day": {"$dateToString": {"date": "$day_list", "format": "%Y-%m-%d", "timezone": "UTC"}},
            "headcount": {"$size": {"$filter": {
                "input": "$people", "as": "p",
                "cond": {"$lte": ["$$p.joined_on", {"$dateToString": {"date": "$day_list", "format": "%Y-%m-%d", "timezone": "UTC"}}]},
            }}},
            "is_working_day": {"$and": [
                {"$gte": [{"$dayOfWeek": "$day_list"}, 2]},
                {"$lte": [{"$dayOfWeek": "$day_list"}, 6]},
            ]},
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day": "$day", "codes": {"$map": {
                "input": {"$filter": {"input": "$people", "as": "p", "cond": {"$lte": ["$$p.joined_on", "$day"]}}},
                "as": "p", "in": "$$p.emp_code",
            }}},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$date", "$$day"]}, {"$in": ["$emp_code", "$$codes"]},
            ]}}}],
            "as": "logs",
        }},
        {"$set": {
            "present_count": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {
                "$cond": [{"$in": ["$$l.status", list(PRESENCE)]},
                           {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1]}, 0],
            }}}},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}}}},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None,
        ]}}},
        {"$setWindowFields": {"sortBy": {"day_list": 1}, "output": {
            "moving_avg_raw": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}},
        }}},
        {"$project": {
            "_id": 0, "department": 1, "date": "$day", "is_working_day": 1,
            "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": {"$cond": [{"$eq": ["$attendance_rate", None]}, None, mongo_round_expr("$attendance_rate", 4)]},
            "moving_avg_7d": {"$cond": [{"$eq": [{"$ifNull": ["$moving_avg_raw", None]}, None]}, None, mongo_round_expr("$moving_avg_raw", 4)]},
        }},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    request_date(from_date); request_date(to_date)
    if to_date < from_date:
        raise HTTPException(422, "to must be on or after from")
    if (date.fromisoformat(to_date) - date.fromisoformat(from_date)).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    if not employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "department not found")
    items = list(employees.aggregate(trend_pipeline(department, from_date, to_date)))
    return {"department": department, "items": items}


# ---------------------------------- explain ---------------------------------
def explain_collection_pipeline(collection, pipeline):
    result = db.command({"explain": {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"})
    # JSON-safe representation while retaining the raw MongoDB explain document's structure.
    return json_util.loads(json_util.dumps(result))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    if endpoint == "attendance_list":
        query = build_attendance_filter(emp_code, date_from, date_to, status)
        pipeline = [{"$match": query}, {"$sort": {"date": -1, "emp_code": 1}}, {"$skip": (page-1)*page_size}, {"$limit": page_size}]
        collection = attendance
    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
        request_month(month); pipeline = employee_monthly_pipeline(emp_code, month); collection = employees
    elif endpoint == "department_summary":
        if not month: raise HTTPException(422, "month is required")
        request_month(month); pipeline = department_summary_pipeline(month, department); collection = employees
    elif endpoint == "late_leaderboard":
        if not month: raise HTTPException(422, "month is required")
        request_month(month); pipeline = leaderboard_pipeline(month, limit, department); collection = attendance
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date: raise HTTPException(422, "department, from and to are required")
        request_date(from_date); request_date(to_date)
        if to_date < from_date or (date.fromisoformat(to_date)-date.fromisoformat(from_date)).days+1 > 92:
            raise HTTPException(422, "invalid trend date range")
        pipeline = trend_pipeline(department, from_date, to_date); collection = employees
    else:
        raise HTTPException(422, "unknown endpoint")
    try:
        return {"endpoint": endpoint, "collection": collection.name, "explain": explain_collection_pipeline(collection, pipeline)}
    except PyMongoError as exc:
        raise HTTPException(503, "MongoDB explain failed") from exc
