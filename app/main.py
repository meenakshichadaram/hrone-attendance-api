"""
Employee Attendance & Analytics API

FastAPI application for managing employee attendance lifecycle, manual regularizations
with full audit trail, and MongoDB-native analytics pipelines.
"""

import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import re
from typing import Any, Dict, List, Optional, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create all required indexes idempotently at application startup."""
    init_indexes()
    yield

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def check_db_ping():
    client.admin.command("ping")


def init_indexes():
    """Create all required indexes idempotently at application startup."""
    try:
        # employees collection
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)])

        # attendance_logs collection
        # Unique constraint on natural key (emp_code, date) prevents concurrent duplicate punch-ins
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        # Fast sorting & pagination for GET /attendance
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        # Fast lookup for closing recent punch-in on punch-out
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
        # Fast query for leaderboard analytics
        db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)])
        # Fast query for status lookups
        db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)])
    except PyMongoError:
        pass


# --------------------------------------------------------------------------- #
# Helpers & Business Rule Functions
# --------------------------------------------------------------------------- #
def truncate_to_seconds(dt: datetime) -> datetime:
    """R1: Before any calculation, instants are truncated to whole seconds."""
    return dt.replace(microsecond=0)


def epoch_ms_to_datetime_utc(ms: int) -> datetime:
    """Converts epoch milliseconds to UTC datetime truncated to whole seconds."""
    if not isinstance(ms, int) or isinstance(ms, bool):
        raise HTTPException(status_code=422, detail="Instant must be integer epoch milliseconds")
    if ms < 100000000000 or ms > 4102444800000:
        raise HTTPException(status_code=422, detail="Epoch milliseconds value out of acceptable range")
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=UTC)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts a BSON / Python datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def round_half_up(value: Union[float, Decimal], decimals: int = 2) -> float:
    """R8: Decimal rounding half-up (not banker's / float rounding)."""
    d = Decimal(str(value))
    quantizer = Decimal("1e-" + str(decimals))
    return float(d.quantize(quantizer, rounding=ROUND_HALF_UP))


def parse_hh_mm(hh_mm_str: str) -> tuple[int, int]:
    parts = hh_mm_str.split(":")
    return int(parts[0]), int(parts[1])


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """Overnight shift: shift_end <= shift_start (e.g. 22:00 - 06:00)."""
    sh, sm = parse_hh_mm(shift_start)
    eh, em = parse_hh_mm(shift_end)
    return (eh, em) <= (sh, sm)


def determine_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start, e.g. 22:00-06:00):
    a punch-in whose IST time is earlier than shift_end belongs to the shift that started
    the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    sh, sm = parse_hh_mm(shift_start)
    eh, em = parse_hh_mm(shift_end)

    if (eh, em) <= (sh, sm):
        # Overnight shift
        if punch_in_ist.time() < time(eh, em):
            att_date = punch_in_ist.date() - timedelta(days=1)
        else:
            att_date = punch_in_ist.date()
    else:
        att_date = punch_in_ist.date()

    return att_date.isoformat()


def compute_late_minutes(punch_in_utc: datetime, att_date_str: str, shift_start: str) -> int:
    """
    R2: Grace period is 10 minutes. A punch-in is late only if it is strictly more than
    10:00 after shift_start. When late, late_minutes = whole minutes (floor) elapsed since
    shift_start - not since the end of the grace period. Otherwise 0.
    Example (shift 09:30): 09:40:00 -> 0; 09:40:01 -> 10; 10:15:59 -> 45.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    d = date.fromisoformat(att_date_str)
    sh, sm = parse_hh_mm(shift_start)
    shift_start_ist = datetime(d.year, d.month, d.day, sh, sm, 0, tzinfo=IST)

    diff_sec = (punch_in_ist - shift_start_ist).total_seconds()
    if diff_sec > 600:  # Strictly more than 10 minutes
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    hours_dec = Decimal(str(diff_sec)) / Decimal("3600")
    return float(hours_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out_utc: datetime, att_date_str: str, shift_start: str, shift_end: str) -> int:
    """
    R3: overtime_minutes = whole minutes (floor) between shift_end and punch_out,
    counted only when that is >= 30; otherwise 0.
    The shift end of an overnight shift falls on the next calendar day.
    """
    punch_out_ist = punch_out_utc.astimezone(IST)
    d = date.fromisoformat(att_date_str)
    sh, sm = parse_hh_mm(shift_start)
    eh, em = parse_hh_mm(shift_end)

    if (eh, em) <= (sh, sm):
        d_end = d + timedelta(days=1)
    else:
        d_end = d

    shift_end_ist = datetime(d_end.year, d_end.month, d_end.day, eh, em, 0, tzinfo=IST)
    diff_sec = (punch_out_ist - shift_end_ist).total_seconds()
    if diff_sec < 0:
        return 0
    minutes = int(diff_sec // 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: After punch-out, half_day = true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return bool(work_hours < 4.50)


def count_working_days_in_month(month_str: str, joined_on_str: str) -> int:
    """
    R7: Mon-Fri, no holiday calendar. For an employee who joined mid-month,
    working days are counted from joined_on.
    """
    year, month = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    m_start = date(year, month, 1)
    m_end = date(year, month, num_days)

    joined_on = date.fromisoformat(joined_on_str)
    if joined_on > m_end:
        return 0

    start_date = max(m_start, joined_on)
    working_days = 0
    cur = start_date
    while cur <= m_end:
        if cur.weekday() < 5:  # Monday to Friday
            working_days += 1
        cur += timedelta(days=1)
    return working_days


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc.get("created_at")),
    }


def serialize_history_entry(entry: dict) -> dict:
    changes = {}
    for k, v in entry.get("changes", {}).items():
        from_val = v.get("from")
        to_val = v.get("to")
        if k in ("punch_in", "punch_out"):
            from_val = datetime_to_epoch_ms(from_val) if isinstance(from_val, datetime) else from_val
            to_val = datetime_to_epoch_ms(to_val) if isinstance(to_val, datetime) else to_val
        changes[k] = {"from": from_val, "to": to_val}

    return {
        "at": datetime_to_epoch_ms(entry["at"]) if isinstance(entry["at"], datetime) else entry["at"],
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": changes,
    }


def serialize_attendance_record(doc: dict) -> dict:
    history = [serialize_history_entry(h) for h in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def sanitize_for_json(obj: Any) -> Any:
    """Recursively converts BSON datetimes, ObjectIds, etc. into JSON-serializable primitives."""
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_for_json(v) for v in obj]
    elif isinstance(obj, datetime):
        return datetime_to_epoch_ms(obj)
    elif hasattr(obj, "__str__") and type(obj).__name__ in ("ObjectId", "Int64"):
        return str(obj)
    return obj


# --------------------------------------------------------------------------- #
# Request Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("Invalid joined_on calendar date")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = Field("PRESENT")

    @field_validator("status")
    def validate_status(cls, v):
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("Only PRESENT, WFH, and ON_DUTY are accepted for punch-in")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)

    @field_validator("status")
    def validate_status(cls, v):
        if v is not None and v not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise ValueError("Invalid status value")
        return v


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        check_db_ping()
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    now_utc = truncate_to_seconds(datetime.now(timezone.utc))
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_utc = epoch_ms_to_datetime_utc(body.punched_at)
    else:
        punch_in_utc = truncate_to_seconds(datetime.now(timezone.utc))

    att_date = determine_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_utc, att_date, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return serialize_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_utc = epoch_ms_to_datetime_utc(body.punched_at)
    else:
        punch_out_utc = truncate_to_seconds(datetime.now(timezone.utc))

    # Closes the employee's most recent record whose punch_in exists
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found")

    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=UTC)

    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    if diff_sec <= 0 or diff_sec > 86400:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record already punched out")

    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    overtime_min = compute_overtime(punch_out_utc, record["date"], emp["shift_start"], emp["shift_end"])
    half_day = compute_half_day(work_hours)

    # Atomic update to prevent race conditions on simultaneous punch-outs
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Record already punched out")

    return serialize_attendance_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        if status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise HTTPException(status_code=422, detail="Invalid status")
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [serialize_attendance_record(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(..., pattern=r"^EMP\d{4,6}$"),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(status_code=422, detail="Supplying punch times with ABSENT or LEAVE status is prohibited")

    target_status = body.status if body.status is not None else record["status"]

    if target_status in ("ABSENT", "LEAVE"):
        new_status = target_status
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_status = target_status
        if body.punch_in is not None:
            new_punch_in = epoch_ms_to_datetime_utc(body.punch_in)
        else:
            new_punch_in = record.get("punch_in")

        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

        if new_punch_in.tzinfo is None:
            new_punch_in = new_punch_in.replace(tzinfo=UTC)

        computed_date = determine_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(status_code=422, detail=f"punch_in belongs to date {computed_date}, not {date}")

        new_late_minutes = compute_late_minutes(new_punch_in, date, emp["shift_start"])

        if body.punch_out is not None:
            new_punch_out = epoch_ms_to_datetime_utc(body.punch_out)
        else:
            new_punch_out = record.get("punch_out")

        if new_punch_out is not None:
            if new_punch_out.tzinfo is None:
                new_punch_out = new_punch_out.replace(tzinfo=UTC)

            diff_sec = (new_punch_out - new_punch_in).total_seconds()
            if diff_sec <= 0 or diff_sec > 86400:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, date, emp["shift_start"], emp["shift_end"])
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Track actual field changes
    changes = {}

    def compare_and_record(field_name: str, old_val: Any, new_val: Any, is_date: bool = False):
        if is_date:
            old_ms = datetime_to_epoch_ms(old_val)
            new_ms = datetime_to_epoch_ms(new_val)
            if old_ms != new_ms:
                changes[field_name] = {"from": old_val, "to": new_val}
        else:
            if old_val != new_val:
                changes[field_name] = {"from": old_val, "to": new_val}

    compare_and_record("status", record.get("status"), new_status)
    compare_and_record("punch_in", record.get("punch_in"), new_punch_in, is_date=True)
    compare_and_record("punch_out", record.get("punch_out"), new_punch_out, is_date=True)
    compare_and_record("work_hours", record.get("work_hours"), new_work_hours)
    compare_and_record("late_minutes", record.get("late_minutes", 0), new_late_minutes)
    compare_and_record("overtime_minutes", record.get("overtime_minutes", 0), new_overtime_minutes)
    compare_and_record("half_day", record.get("half_day", False), new_half_day)

    if not changes:
        raise HTTPException(status_code=422, detail="No changes detected in regularization request")

    now_utc = truncate_to_seconds(datetime.now(timezone.utc))
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic locking using current history length ensures no history entries are lost
    current_history_len = len(record.get("history", []))
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": {"$size": current_history_len}},
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict detected")

    return serialize_attendance_record(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(..., pattern=r"^EMP\d{4,6}$"),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    working_days = count_working_days_in_month(month, emp["joined_on"])
    month_start = f"{month}-01"
    year, m_num = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m_num)[1]
    month_end = f"{month}-{last_day:02d}"

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$project": {
                "is_weekday": {
                    "$in": [
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}},
                        [2, 3, 4, 5, 6],  # Mon=2 to Fri=6
                    ]
                },
                "status": 1,
                "half_day": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$eq": ["$is_weekday", True]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        stats = agg_res[0]
        present_days = float(stats["present_days"])
        leave_days = int(stats["leave_days"])
        late_count = int(stats["late_count"])
        total_late_minutes = int(stats["total_late_minutes"])
        total_overtime_minutes = int(stats["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = round_half_up(Decimal(str(present_days)) / Decimal(str(working_days)) * Decimal(100), 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, m_num = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m_num)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emps": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$emps"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {
                                                "$in": [
                                                    {"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date", "format": "%Y-%m-%d"}}},
                                                    [2, 3, 4, 5, 6],
                                                ]
                                            },
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "work_hours_list": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$l.work_hours", None]},
                            ]
                        },
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
                "total_late_minutes": {"$sum": "$logs.late_minutes"},
                "leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]},
                        }
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$work_hours_list"}, 0]},
                        {"$round": [{"$avg": "$work_hours_list.work_hours"}, 2]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": ASCENDING}},
    ]

    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, m_num = map(int, month.split("-"))
    last_day = calendar.monthrange(year, m_num)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    match_dept: Dict[str, Any] = {}
    if department is not None:
        match_dept["emp.department"] = department

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if match_dept:
        pipeline.append({"$match": match_dept})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {"$rank": {}}  # Standard competition ranking: ties share rank, next rank is skipped
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    date_from: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    from_d = date.fromisoformat(date_from)
    to_d = date.fromisoformat(date_to)

    if to_d < from_d:
        raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")
    if (to_d - from_d).days > 92:
        raise HTTPException(status_code=422, detail="Date range exceeds 92 days")

    # 404 if department has no employees
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    from_dt = datetime(from_d.year, from_d.month, from_d.day, tzinfo=UTC)
    to_dt = datetime(to_d.year, to_d.month, to_d.day, tzinfo=UTC)
    bounds_upper = to_dt + timedelta(days=1)

    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "_id": 0,
                "date_dt": {"$literal": from_dt},
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, bounds_upper],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"date": "$date_dt", "format": "%Y-%m-%d"}},
                "date_dt": 1,
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$date", "$$cur_date"]}
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp",
                        }
                    },
                    {"$unwind": "$emp"},
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp.department", department]}
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "joined_emps",
            }
        },
        {
            "$project": {
                "date": 1,
                "date_dt": 1,
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": "$date_dt"},
                        [2, 3, 4, 5, 6],
                    ]
                },
                "headcount": {"$size": "$joined_emps"},
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": ASCENDING}},
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {"$round": ["$moving_avg_7d", 4]},
                        None,
                    ]
                },
            }
        },
    ]

    items = list(db.employees.aggregate(pipeline))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    if endpoint == "attendance_list":
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        explain_cmd = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        res = db.command(explain_cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_for_json(res)}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
        year, m_num = map(int, month.split("-"))
        last_day = calendar.monthrange(year, m_num)[1]
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": f"{month}-01", "$lte": f"{month}-{last_day:02d}"},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {"$sum": 1},
                }
            },
        ]
        explain_cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(explain_cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_for_json(res)}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        year, m_num = map(int, month.split("-"))
        last_day = calendar.monthrange(year, m_num)[1]
        month_end = f"{month}-{last_day:02d}"
        match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
        if department:
            match_stage["department"] = department
        pipeline = [
            {"$match": match_stage},
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                }
            },
        ]
        explain_cmd = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(explain_cmd)
        return {"endpoint": endpoint, "collection": "employees", "explain": sanitize_for_json(res)}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        year, m_num = map(int, month.split("-"))
        last_day = calendar.monthrange(year, m_num)[1]
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": f"{month}-01", "$lte": f"{month}-{last_day:02d}"},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
        ]
        explain_cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(explain_cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_for_json(res)}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required for department_trend")
        pipeline = [
            {"$match": {"department": department}},
            {"$limit": 1},
        ]
        explain_cmd = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(explain_cmd)
        return {"endpoint": endpoint, "collection": "employees", "explain": sanitize_for_json(res)}

    raise HTTPException(status_code=422, detail=f"Unsupported explain endpoint: {endpoint}")
