"""Employee Attendance & Analytics API for the HROne engineering assignment."""
import os
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.responses import Response
from bson.timestamp import Timestamp
from bson import json_util
import json
import re
import math
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import ObjectId

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=15000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
STATUSES = PRESENCE + ["ABSENT", "LEAVE"]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def ensure_indexes():
    """Create the indexes used for uniqueness, filters, sort order and analytics."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="employee_department_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="employee_department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="employee_joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="attendance_employee_date_status")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)], name="attendance_status_date")


@app.on_event("startup")
def startup():
    ensure_indexes()


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def trunc_seconds(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0)


def from_ms(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or value < 100_000_000_000 or value > 4_102_444_800_000:
        raise HTTPException(422, "timestamp must be epoch milliseconds in the supported range")
    try:
        return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(422, "invalid timestamp")


def to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.replace(microsecond=0).timestamp() * 1000)


def db_dt(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.astimezone(UTC).replace(tzinfo=None, microsecond=0)


def parse_date(value: str) -> date:
    try:
        d = date.fromisoformat(value)
        if d.isoformat() != value:
            raise ValueError
        return d
    except (ValueError, TypeError):
        raise HTTPException(422, "date must be YYYY-MM-DD")


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    start = date.fromisoformat(value + "-01")
    end = date(start.year + (start.month == 12), (start.month % 12) + 1, 1) - timedelta(days=1)
    return start, end


def round_half_up(value: float, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def working_days(start: date, end: date) -> int:
    if end < start:
        return 0
    count = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


def shift_datetimes(attendance_date: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    d = parse_date(attendance_date)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    start = datetime.combine(d, time(sh, sm), IST)
    end_date = d + timedelta(days=1) if (eh, em) <= (sh, sm) else d
    end = datetime.combine(end_date, time(eh, em), IST)
    return start.astimezone(UTC).replace(microsecond=0), end.astimezone(UTC).replace(microsecond=0)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = punch_in.astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    if (end_h, end_m) <= (start_h, start_m) and (local.hour, local.minute) < (end_h, end_m):
        local = local - timedelta(days=1)
    return local.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: Optional[str] = None) -> int:
    local = punch_in.astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    start = local.replace(hour=h, minute=m, second=0, microsecond=0)
    if shift_end is not None:
        eh, em = map(int, shift_end.split(":"))
        if (eh, em) <= (h, m) and (local.hour, local.minute) < (eh, em):
            start -= timedelta(days=1)
    elapsed = (local - start).total_seconds()
    return int(elapsed // 60) if elapsed > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (punch_out - punch_in).total_seconds()
    return round_half_up(seconds / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str) -> int:
    _, end = shift_datetimes(date_str, shift_start, shift_end)
    minutes = int((punch_out - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def clean_employee(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("_id", None)
    if "created_at" in doc:
        doc["created_at"] = to_ms(doc["created_at"])
    return doc


def clean_attendance(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("_id", None)
    for key in ("punch_in", "punch_out"):
        doc[key] = to_ms(doc.get(key))
    history = []
    for entry in doc.get("history", []) or []:
        e = dict(entry)
        e["at"] = to_ms(e.get("at"))
        changes = {}
        for field, pair in (e.get("changes") or {}).items():
            p = dict(pair)
            if field in ("punch_in", "punch_out"):
                p["from"] = to_ms(p.get("from"))
                p["to"] = to_ms(p.get("to"))
            changes[field] = p
        e["changes"] = changes
        history.append(e)
    doc["history"] = history
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("half_day", False)
    doc.setdefault("work_hours", None)
    return doc


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_join_date(cls, v):
        parse_date(v)
        return v

    @model_validator(mode="after")
    def different_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_timestamp(cls, v):
        if v is not None:
            from_ms(v)
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v not in PRESENCE:
            raise ValueError("status must be PRESENT, WFH or ON_DUTY")
        return v


class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_timestamp(cls, v):
        if v is not None:
            from_ms(v)
        return v


class RegularizeIn(StrictModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_timestamps(cls, v):
        if v is not None:
            from_ms(v)
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v is not None and v not in STATUSES:
            raise ValueError("invalid status")
        return v


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = db_dt(now_utc())
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return clean_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    for item in items:
        item["created_at"] = to_ms(item.get("created_at"))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    ts = trunc_seconds(ts)
    d = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])
    doc = {"emp_code": body.emp_code, "date": d, "status": body.status, "punch_in": db_dt(ts), "punch_out": None,
           "work_hours": None, "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]), "overtime_minutes": 0,
           "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return clean_attendance(doc)


def attendance_query(emp_code=None, date_from=None, date_to=None, status=None):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            parse_date(date_from); q["date"]["$gte"] = date_from
        if date_to:
            parse_date(date_to); q["date"]["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be <= date_to")
    if status:
        if status not in STATUSES:
            raise HTTPException(422, "invalid status")
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    docs = list(db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size))
    return {"items": [clean_attendance(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = trunc_seconds(from_ms(body.punched_at) if body.punched_at is not None else now_utc())
    candidates = list(db.attendance_logs.find({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": db_dt(ts)}}).sort("punch_in", DESCENDING).limit(1))
    if not candidates:
        raise HTTPException(404, "no punch-in found")
    record = candidates[0]
    pin = record["punch_in"].replace(tzinfo=UTC)
    delta = (ts - pin).total_seconds()
    if delta <= 0 or delta > 24 * 3600:
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    hours = compute_work_hours(pin, ts)
    overtime = compute_overtime(ts, emp["shift_end"], record["date"], emp["shift_start"])
    updated = db.attendance_logs.find_one_and_update({"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": db_dt(ts), "work_hours": hours, "overtime_minutes": overtime, "half_day": hours < 4.5}},
        return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record already punched out")
    return clean_attendance(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "attendance record not found")
    old = dict(record)
    new_status = body.status if body.status is not None else record["status"]
    if new_status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")
    pin = from_ms(body.punch_in) if body.punch_in is not None else (record.get("punch_in").replace(tzinfo=UTC) if record.get("punch_in") else None)
    pout = from_ms(body.punch_out) if body.punch_out is not None else (record.get("punch_out").replace(tzinfo=UTC) if record.get("punch_out") else None)
    if new_status in ("ABSENT", "LEAVE"):
        pin = pout = None
    elif pin is None:
        raise HTTPException(422, "presence status requires punch_in")
    if pin and attendance_date_for(pin, emp["shift_start"], emp["shift_end"]) != date:
        raise HTTPException(422, "punch_in must belong to the record attendance date")
    if pout and (pin is None or pout <= pin or (pout - pin).total_seconds() > 86400):
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    late = compute_late_minutes(pin, emp["shift_start"], emp["shift_end"]) if pin and new_status in PRESENCE else 0
    hours = compute_work_hours(pin, pout) if pin and pout and new_status in PRESENCE else None
    overtime = compute_overtime(pout, emp["shift_end"], date, emp["shift_start"]) if pout and new_status in PRESENCE else 0
    half = bool(hours is not None and hours < 4.5)
    proposed = {"status": new_status, "punch_in": db_dt(pin), "punch_out": db_dt(pout), "work_hours": hours,
                "late_minutes": late, "overtime_minutes": overtime, "half_day": half}
    changes = {}
    for field, value in proposed.items():
        previous = old.get(field)
        if field in ("punch_in", "punch_out") and previous is not None:
            previous = previous.replace(tzinfo=UTC)
        if previous != value:
            changes[field] = {"from": to_ms(previous) if field in ("punch_in", "punch_out") else previous,
                              "to": to_ms(value) if field in ("punch_in", "punch_out") else value}
    if not changes:
        raise HTTPException(422, "request does not change the record")
    entry = {"at": db_dt(now_utc()), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic concurrency: match the observed history length and key fields so concurrent edits cannot overwrite history.
    filter_doc = {"_id": record["_id"], "status": old["status"], "punch_in": old.get("punch_in"), "punch_out": old.get("punch_out"),
                  "history": old.get("history", [])}
    updated = db.attendance_logs.find_one_and_update(filter_doc, {"$set": proposed, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record changed concurrently; retry")
    return clean_attendance(updated)


def month_match(start: date, end: date) -> dict:
    return {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}


def employee_month_pipeline(emp_code: str, start: date, end: date):
    return [
        {"$match": {"emp_code": emp_code, **month_match(start, end)}},
        {"$set": {"_weekday": {"$and": [{"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]},
                   "_present": {"$in": ["$status", PRESENCE]}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": ["$_present", {"$and": [{"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]}]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}
    ]

def department_summary_pipeline(start: date, end: date, department: Optional[str] = None):
    match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        match["department"] = department

    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start.isoformat()]},
                                    {"$lte": ["$date", end.isoformat()]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {
                    "$sum": {"$cond": [{"$eq": ["$logs", None]}, 1, 0]}
                },
                "_employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", PRESENCE]},
                                    {
                                        "$gte": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": {
                                                            "$ifNull": [
                                                                "$logs.date",
                                                                "1970-01-01",
                                                            ]
                                                        }
                                                    }
                                                }
                                            },
                                            2,
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": {
                                                            "$ifNull": [
                                                                "$logs.date",
                                                                "1970-01-01",
                                                            ]
                                                        }
                                                    }
                                                }
                                            },
                                            6,
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$ifNull": ["$logs.half_day", False]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "_avg_work_hours": {
                    "$avg": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", PRESENCE]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]
                    }
                },
            }
        },
        {
            "$set": {
                "headcount": {"$size": "$_employees"},
                "avg_work_hours": {"$ifNull": ["$_avg_work_hours", None]},
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = parse_month(month)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if not emp:
        raise HTTPException(404, "employee not found")
    wd_start = max(start, parse_date(emp["joined_on"]))
    wd = working_days(wd_start, end)
    agg = list(db.attendance_logs.aggregate(employee_month_pipeline(emp_code, start, end)))
    vals = agg[0] if agg else {}
    present = round_half_up(vals.get("present_days", 0), 2)
    pct = round_half_up(present / wd * 100, 4) if wd else None
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": present,
            "leave_days": vals.get("leave_days", 0), "late_count": vals.get("late_count", 0),
            "total_late_minutes": vals.get("total_late_minutes", 0), "total_overtime_minutes": vals.get("total_overtime_minutes", 0),
            "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end = parse_month(month)
    pipeline = department_summary_pipeline(start, end, department)
    items = list(db.employees.aggregate(pipeline))

    for item in items:
        item["present_days"] = round_half_up(item.get("present_days", 0), 2)
        if item.get("avg_work_hours") is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)

    return {"month": month, "items": items}
@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start, end = parse_month(month)
    pipeline = [
        {"$match": {**month_match(start, end), "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}
    ]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(alias="from"), to_date: str = Query(alias="to")):
    start, end = parse_date(from_date), parse_date(to_date)
    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(422, "invalid date range; maximum is 92 days")
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "department not found")
    # Aggregate logs to daily metrics, then use $densify/$fill so missing days are generated in MongoDB.
    pipeline = [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"}, {"$match": {"employee.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", PRESENCE]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}
        }},

        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": 0,
                            "dates": {
                                "$map": {
                                    "input": {"$range": [0, (end - start).days + 1]},
                                    "as": "i",
                                    "in": {
                                        "$dateToString": {
                                            "date": {
                                                "$dateAdd": {
                                                    "startDate": datetime.combine(start, time(), UTC),
                                                    "unit": "day",
                                                    "amount": "$$i",
                                                }
                                            },
                                            "format": "%Y-%m-%d",
                                            "timezone": "UTC",
                                        }
                                    },
                                }
                            },
                        }
                    },
                    {"$unwind": "$dates"},
                    {
                        "$project": {
                            "_id": "$dates",
                            "present_count": {"$literal": 0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        {
            "$group": {
                "_id": "$_id",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },

        {"$project": {"_id": 0, "date": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [datetime.combine(start, time(), UTC), datetime.combine(end + timedelta(days=1), time(), UTC)]}}},
        {"$set": {"date_str": {"$dateToString": {"date": "$date", "format": "%Y-%m-%d", "timezone": "UTC"}},
                   "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date_str"}, "pipeline": [
            {"$match": {"department": department}}, {"$match": {"$expr": {"$lte": ["$joined_on", "$$day"]}}}, {"$count": "n"}
        ], "as": "hc"}},
        {"$set": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
            "is_working_day": {"$and": [
                {"$gte": [{"$dayOfWeek": "$date"}, 2]},
                {"$lte": [{"$dayOfWeek": "$date"}, 6]},
            ]},
        }},        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None
        ]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"_window_rates": {"$push": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$set": {"moving_avg_7d": {"$let": {
            "vars": {"valid": {"$filter": {"input": "$_window_rates", "as": "r", "cond": {"$ne": ["$$r", None]}}}},
            "in": {"$cond": [{"$gt": [{"$size": "$$valid"}, 0]}, {"$avg": "$$valid"}, None]}
        }}}},
        {"$sort": {"date": 1}}, {"$project": {"_id": 0, "date": "$date_str", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}}
    ]
    items = list(db.attendance_logs.aggregate(pipeline))
    for item in items:
        if item["attendance_rate"] is not None: item["attendance_rate"] = round_half_up(item["attendance_rate"], 4)
        if item["moving_avg_7d"] is not None: item["moving_avg_7d"] = round_half_up(item["moving_avg_7d"], 4)
    # If no log falls anywhere in the range, densify has no seed row. Build
   #the empty day series in a tiny aggregation.
    if not items:
        days = (end - start).days + 1
        items = [{"date": (start + timedelta(days=i)).isoformat(), "is_working_day": (start + timedelta(days=i)).weekday() < 5,
                  "headcount": db.employees.count_documents({"department": department, "joined_on": {"$lte": (start + timedelta(days=i)).isoformat()}}),
                  "present_count": 0, "late_count": 0, "attendance_rate": None, "moving_avg_7d": None} for i in range(days)]
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
                    limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, from_date: Optional[str] = Query(None, alias="from"), to_date: Optional[str] = Query(None, alias="to"),
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed: raise HTTPException(422, "unsupported endpoint")
    if endpoint == "attendance_list":
        q = attendance_query(emp_code, date_from, date_to, status)
        cmd = {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*page_size, "limit": page_size}
        explain = db.command("explain", cmd, verbosity="executionStats")
        return Response(
    content=json_util.dumps({
        "endpoint": endpoint,
        "collection": "attendance_logs",
        "explain": explain,
    }, json_options=json_util.RELAXED_JSON_OPTIONS),
    media_type="application/json",
)
    if not month and endpoint != "department_trend": raise HTTPException(422, "month is required")
    start = end = None
    if month: start, end = parse_month(month)
    if endpoint == "employee_monthly":
        if not emp_code: raise HTTPException(422, "emp_code is required")
        pipeline = employee_month_pipeline(emp_code, start, end)
        coll = "attendance_logs"
    elif endpoint == "department_summary":
       pipeline = department_summary_pipeline(start, end, department)
       coll = "employees"

    elif endpoint == "late_leaderboard":
        pipeline = [
            {
                "$match": {
                    **month_match(start, end),
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
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
        ]

        if department is not None:
            pipeline.append(
                {"$match": {"employee.department": department}}
            )

        pipeline += [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "_id": 1,
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
        coll = "attendance_logs"


    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")

        a, b = parse_date(from_date), parse_date(to_date)
        if b < a or (b - a).days + 1 > 92:
            raise HTTPException(422, "invalid date range")

        if not db.employees.find_one({"department": department}, {"_id": 1}):
            raise HTTPException(404, "department not found")

        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }},
            {"$unwind": "$employee"},
            {"$match": {"employee.department": department}},
            {"$group": {
                "_id": "$date",
                "present_count": {"$sum": {"$cond": [
                    {"$in": ["$status", PRESENCE]},
                    {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                    0,
                ]}},
                "late_count": {"$sum": {"$cond": [
                    {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                    1,
                    0,
                ]}},
            }},
            {"$project": {
                "_id": 0,
                "date": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }},
            {"$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.combine(a, time(), UTC),
                        datetime.combine(b + timedelta(days=1), time(), UTC),
                    ],
                },
            }},
            {"$set": {
                "date_str": {"$dateToString": {
                    "date": "$date",
                    "format": "%Y-%m-%d",
                    "timezone": "UTC",
                }},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }},
            {"$lookup": {
                "from": "employees",
                "let": {"day": "$date_str"},
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$match": {"$expr": {"$lte": ["$joined_on", "$$day"]}}},
                    {"$count": "n"},
                ],
                "as": "hc",
            }},
            {"$set": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
                "is_working_day": {"$and": [
                    {"$gte": [{"$dayOfWeek": "$date"}, 2]},
                    {"$lte": [{"$dayOfWeek": "$date"}, 6]},
                ]},
            }},
            {"$set": {"attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": ["$present_count", "$headcount"]},
                None,
            ]}}},
            {"$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "_window_rates": {
                        "$push": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    },
                },
            }},
            {"$set": {"moving_avg_7d": {"$let": {
                "vars": {"valid": {"$filter": {
                    "input": "$_window_rates",
                    "as": "r",
                    "cond": {"$ne": ["$$r", None]},
                }}},
                "in": {"$cond": [
                    {"$gt": [{"$size": "$$valid"}, 0]},
                    {"$avg": "$$valid"},
                    None,
                ]},
            }}}},
            {"$sort": {"date": 1}},
            {"$project": {
                "_id": 0,
                "date": "$date_str",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }},
        ]
        coll = "attendance_logs"

    explain = db.command("explain", {"aggregate": coll, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")

    return Response(
        content=json_util.dumps(
            {
                "endpoint": endpoint,
                "collection": coll,
                "explain": explain,
            },
            json_options=json_util.RELAXED_JSON_OPTIONS,
        ),
        media_type="application/json",
    )
