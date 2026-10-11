"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from pymongo import MongoClient

from decimal import Decimal, ROUND_HALF_UP
from pymongo import ReturnDocument

from fastapi.responses import JSONResponse
from bson.json_util import dumps
import json

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employee_code",
    )

    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_employee_date",
    )


    # Supports date-range filters used by attendance analytics.
    db.attendance_logs.create_index(
        [("date", 1)],
        name="ix_attendance_date",
    )

    # Supports status-filtered attendance queries.
    db.attendance_logs.create_index(
        [("status", 1), ("date", -1), ("emp_code", 1)],
        name="ix_attendance_status_date_emp",
    )

    # Supports department-based analytics and trend queries.
    db.employees.create_index(
        [("department", 1), ("joined_on", 1)],
        name="ix_employee_department_joined",
    )


    # Supports attendance list sorting and pagination.
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="ix_attendance_date_emp_sort",
    )

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def compute_late_minutes(punch_in_ist: datetime, shift_start_str: str) -> int:
    """Calculate late minutes with a 10-minute grace period."""
    shift_start = datetime.strptime(
        shift_start_str, "%H:%M"
    ).time()

    scheduled_start = datetime.combine(
        punch_in_ist.date(),
        shift_start,
        tzinfo=IST,
    )

    # For a punch after midnight, associate it with yesterday's
    # shift when the shift starts late in the evening.
    if (
        punch_in_ist.time() < shift_start
        and shift_start.hour >= 18
    ):
        scheduled_start -= timedelta(days=1)

    minutes_late = int(
        (punch_in_ist - scheduled_start).total_seconds() // 60
    )

    return minutes_late if minutes_late > 10 else 0

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    elapsed_seconds = Decimal(
        str((punch_out - punch_in).total_seconds())
    )

    return float(
        (elapsed_seconds / Decimal("3600")).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )

def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str, ) -> int:
    """Return overtime only when at least 30 minutes have elapsed."""
    start_time = datetime.strptime(shift_start, "%H:%M").time()
    end_time = datetime.strptime(shift_end, "%H:%M").time()

    shift_date = datetime.fromisoformat(date_str).date()
    shift_end_date = shift_date

    if end_time <= start_time:
        shift_end_date += timedelta(days=1)

    scheduled_end_ist = datetime.combine(
        shift_end_date,
        end_time,
        tzinfo=IST,
    )

    # Convert the punch-out instant to IST for a consistent comparison.
    if punch_out.tzinfo is None:
        punch_out_ist = punch_out.replace(tzinfo=timezone.utc).astimezone(IST)
    else:
        punch_out_ist = punch_out.astimezone(IST)

    minutes_after_shift = int(
        (punch_out_ist - scheduled_end_ist).total_seconds() // 60
    )

    return minutes_after_shift if minutes_after_shift >= 30 else 0

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now()
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc

@app.get("/employees")
def list_employees(
        department: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
):
    if page < 1:
        raise HTTPException(422, "page must be at least 1")

    if page_size < 1 or page_size > 100:
        raise HTTPException(422, "page_size must be between 1 and 100")

    q = {}
    if department is not None:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)

    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    # 1. Confirm employee exists.
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="unknown employee",
        )

    # 2. Validate attendance status.
    allowed_statuses = {"PRESENT", "WFH", "ON_DUTY"}

    if body.status not in allowed_statuses:
        raise HTTPException(
            status_code=422,
            detail="status must be PRESENT, WFH, or ON_DUTY",
        )

    # 3. Convert epoch milliseconds to UTC.
    if body.punched_at is None:
        ts_utc = datetime.now(timezone.utc)
    else:
        if (
            isinstance(body.punched_at, bool)
            or not isinstance(body.punched_at, int)
            or not 100_000_000_000
            <= body.punched_at
            <= 4_102_444_800_000
        ):
            raise HTTPException(
                status_code=422,
                detail="punched_at must be valid epoch milliseconds",
            )

        try:
            ts_utc = datetime.fromtimestamp(
                body.punched_at / 1000,
                tz=timezone.utc,
            )
        except (OverflowError, OSError, ValueError):
            raise HTTPException(
                status_code=422,
                detail="invalid punched_at timestamp",
            )

    # R1: truncate to whole seconds before calculations.
    ts_utc = ts_utc.replace(microsecond=0)
    ts_db = ts_utc.replace(tzinfo=None)
    ts_ist = ts_utc.astimezone(IST)

    # 4. Parse shift times.
    shift_start = datetime.strptime(
        emp["shift_start"], "%H:%M"
    ).time()
    shift_end = datetime.strptime(
        emp["shift_end"], "%H:%M"
    ).time()

    is_overnight = shift_end <= shift_start

    # 5. Determine the shift's attendance date.
    attendance_date = ts_ist.date()

    # For an overnight shift, punches before shift_end belong
    # to the shift that started on the previous calendar day.
    if is_overnight and ts_ist.time() < shift_end:
        attendance_date -= timedelta(days=1)

    date_str = attendance_date.isoformat()

    # 6. Determine the scheduled start for this shift date.
    scheduled_start_ist = datetime.combine(
        attendance_date,
        shift_start,
        tzinfo=IST,
    )

    # 7. Calculate late minutes from the scheduled shift start.
    minutes_late = int(
        (ts_ist - scheduled_start_ist).total_seconds() // 60
    )

    late_minutes = minutes_late if minutes_late > 10 else 0

    # 8. Build the attendance document.
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts_db,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    # 9. Insert the record.
    try:
        res = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date",
        )

    # 10. Return API-safe values.
    result = dict(doc)
    result["id"] = str(res.inserted_id)
    result.pop("_id", None)
    result["punch_in"] = int(ts_utc.timestamp() * 1000)
    result["punch_out"] = None

    return result

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    # 1. Confirm employee exists.
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="unknown employee",
        )

    # 2. Convert epoch milliseconds to UTC.
    if body.punched_at is None:
        ts_utc = datetime.now(timezone.utc)
    else:
        if (
            isinstance(body.punched_at, bool)
            or not isinstance(body.punched_at, int)
            or not 100_000_000_000
            <= body.punched_at
            <= 4_102_444_800_000
        ):
            raise HTTPException(
                status_code=422,
                detail="punched_at must be valid epoch milliseconds",
            )

        try:
            ts_utc = datetime.fromtimestamp(
                body.punched_at / 1000,
                tz=timezone.utc,
            )
        except (OverflowError, OSError, ValueError):
            raise HTTPException(
                status_code=422,
                detail="invalid punched_at timestamp",
            )

    # R1: truncate instants to whole seconds.
    ts_utc = ts_utc.replace(microsecond=0)
    ts_db = ts_utc.replace(tzinfo=None)

    # 3. Find the most recent matching punch-in.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts_db},
        },
        sort=[("punch_in", -1)],
    )

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="no punch-in found",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="already punched out",
        )

    punch_in_db = record["punch_in"]

    if punch_in_db.tzinfo is not None:
        punch_in_db = punch_in_db.astimezone(
            timezone.utc
        ).replace(tzinfo=None)

    # 4. Validate elapsed time.
    elapsed_seconds = (ts_db - punch_in_db).total_seconds()

    if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be after punch_in and within 24 hours",
        )

    # 5. Calculate work hours using half-up rounding.
    work_hours = float(
        (
            Decimal(str(elapsed_seconds)) / Decimal("3600")
        ).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )

    # 6. Calculate scheduled shift end using the attendance date.
    shift_start = datetime.strptime(
        emp["shift_start"], "%H:%M"
    ).time()
    shift_end = datetime.strptime(
        emp["shift_end"], "%H:%M"
    ).time()

    shift_start_date = datetime.fromisoformat(
        record["date"]
    ).date()

    shift_end_date = shift_start_date

    # Overnight shift: end occurs on the following calendar day.
    if shift_end <= shift_start:
        shift_end_date += timedelta(days=1)

    shift_end_ist = datetime.combine(
        shift_end_date,
        shift_end,
        tzinfo=IST,
    )

    shift_end_utc = shift_end_ist.astimezone(
        timezone.utc
    ).replace(tzinfo=None)

    # 7. Overtime counts only complete minutes after shift end.
    minutes_after_shift = int(
        (ts_db - shift_end_utc).total_seconds() // 60
    )

    # R3: overtime is recorded only if it reaches 30 minutes.
    overtime_minutes = (
        minutes_after_shift
        if minutes_after_shift >= 30
        else 0
    )

    # 8. Half-day is based on rounded work hours.
    half_day = work_hours < 4.50

    # 9. Atomic update prevents concurrent punch-outs.
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts_db,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="already punched out",
        )

    # 10. Return instants as epoch milliseconds.
    updated.pop("_id", None)

    for field in ("punch_in", "punch_out"):
        value = updated[field]

        if value is not None:
            if value.tzinfo is not None:
                value = value.astimezone(
                    timezone.utc
                ).replace(tzinfo=None)

            updated[field] = int(
                value.replace(
                    tzinfo=timezone.utc
                ).timestamp() * 1000
            )

    return updated

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    from datetime import date as date_type, datetime, timezone

    # 1. Validate pagination
    if page < 1:
        raise HTTPException(
            status_code=422,
            detail="page must be at least 1",
        )

    if page_size < 1 or page_size > 100:
        raise HTTPException(
            status_code=422,
            detail="page_size must be between 1 and 100",
        )

    # 2. Validate date filters
    for field_name, value in [
        ("date_from", date_from),
        ("date_to", date_to),
    ]:
        if value is not None:
            try:
                parsed_date = date_type.fromisoformat(value)
                if parsed_date.isoformat() != value:
                    raise ValueError
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=f"{field_name} must use YYYY-MM-DD format",
                )

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must not be after date_to",
        )

    # 3. Validate status
    allowed_statuses = {
        "PRESENT",
        "WFH",
        "ON_DUTY",
        "ABSENT",
        "LEAVE",
    }

    if status is not None and status not in allowed_statuses:
        raise HTTPException(
            status_code=422,
            detail="Invalid status",
        )

    # 4. Build MongoDB filters
    query = {}

    if emp_code:
        query["emp_code"] = emp_code

    if date_from or date_to:
        date_filter = {}

        if date_from:
            date_filter["$gte"] = date_from

        if date_to:
            date_filter["$lte"] = date_to

        query["date"] = date_filter

    if status:
        query["status"] = status

    # 5. Count matching records before pagination
    total = db.attendance_logs.count_documents(query)

    # 6. Apply sorting and pagination inside MongoDB
    skip = (page - 1) * page_size

    cursor = (
        db.attendance_logs.find(query, {"_id": 0, "id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    page_docs = list(cursor)

    # 7. Convert stored UTC datetimes to epoch milliseconds.
    #    Handle nested history entries and change values too.
    def convert_timestamps(value):
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)

            return int(value.timestamp() * 1000)

        if isinstance(value, dict):
            return {
                key: convert_timestamps(item)
                for key, item in value.items()
                if key not in {"_id", "id"}
            }

        if isinstance(value, list):
            return [convert_timestamps(item) for item in value]

        return value

    items = [convert_timestamps(doc) for doc in page_docs]

    # 8. Return the contract response
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn,
):
    from datetime import date as date_type
    from pymongo.errors import DuplicateKeyError

    # 1. Validate the path date.
    try:
        parsed_date = date_type.fromisoformat(date)
        if parsed_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")

    # 2. Validate the employee and attendance record.
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date}
    )
    if record is None:
        raise HTTPException(404, "attendance record not found")

    # 3. Validate the status, if supplied.
    allowed_statuses = {
        "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
    }
    if body.status is not None and body.status not in allowed_statuses:
        raise HTTPException(422, "invalid status")

    supplied = body.model_fields_set

    # ABSENT/LEAVE cannot be combined with supplied punch times.
    if body.status in {"ABSENT", "LEAVE"} and (
        "punch_in" in supplied or "punch_out" in supplied
    ):
        raise HTTPException(
            422, "ABSENT or LEAVE cannot include punch times"
        )

    # 4. Convert existing stored instants to naive UTC.
    def as_utc_naive(value):
        if value is None:
            return None
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    old_in = as_utc_naive(record.get("punch_in"))
    old_out = as_utc_naive(record.get("punch_out"))

    # Request timestamps must be integer epoch milliseconds.
    def parse_epoch(value, field):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 100_000_000_000 <= value <= 4_102_444_800_000
        ):
            raise HTTPException(
                422, f"{field} must be valid epoch milliseconds"
            )

        try:
            parsed = datetime.fromtimestamp(
                value / 1000, tz=timezone.utc
            )
        except (OverflowError, OSError, ValueError):
            raise HTTPException(422, f"invalid {field}")

        return parsed.replace(microsecond=0).replace(tzinfo=None)

    new_status = body.status or record["status"]

    if new_status in {"ABSENT", "LEAVE"}:
        new_in = None
        new_out = None
    else:
        new_in = (
            parse_epoch(body.punch_in, "punch_in")
            if "punch_in" in supplied
            else old_in
        )
        new_out = (
            parse_epoch(body.punch_out, "punch_out")
            if "punch_out" in supplied
            else old_out
        )

        if new_in is None:
            raise HTTPException(
                422, "presence status requires punch_in"
            )

        # The corrected punch-in must belong to this attendance date.
        punch_in_ist = new_in.replace(tzinfo=timezone.utc).astimezone(IST)
        shift_start = datetime.strptime(
            emp["shift_start"], "%H:%M"
        ).time()
        shift_end = datetime.strptime(
            emp["shift_end"], "%H:%M"
        ).time()

        expected_date = punch_in_ist.date()
        if (
            shift_end <= shift_start
            and punch_in_ist.time() < shift_end
        ):
            expected_date -= timedelta(days=1)

        if expected_date.isoformat() != date:
            raise HTTPException(
                422, "punch_in must belong to the record attendance date"
            )

        if new_out is not None:
            elapsed = (new_out - new_in).total_seconds()
            if elapsed <= 0 or elapsed > 24 * 60 * 60:
                raise HTTPException(
                    422, "punch_out must be after punch_in and within 24 hours"
                )

    # 5. Recalculate derived values from the final timestamps.
    if new_in is None:
        late_minutes = 0
    else:
        punch_in_ist = new_in.replace(
            tzinfo=timezone.utc
        ).astimezone(IST)
        late_minutes = compute_late_minutes(
            punch_in_ist, emp["shift_start"]
        )

    if new_in is None or new_out is None:
        work_hours = None
        overtime_minutes = 0
        half_day = False
    else:
        elapsed_seconds = (new_out - new_in).total_seconds()
        work_hours = float(
            (
                Decimal(str(elapsed_seconds)) / Decimal("3600")
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )

        shift_start = datetime.strptime(
            emp["shift_start"], "%H:%M"
        ).time()
        shift_end = datetime.strptime(
            emp["shift_end"], "%H:%M"
        ).time()

        shift_end_date = parsed_date
        if shift_end <= shift_start:
            shift_end_date += timedelta(days=1)

        end_ist = datetime.combine(
            shift_end_date, shift_end, tzinfo=IST
        )
        end_utc = end_ist.astimezone(timezone.utc).replace(
            tzinfo=None
        )

        overtime_minutes = max(
            0, int((new_out - end_utc).total_seconds() // 60)
        )
        if overtime_minutes < 30:
            overtime_minutes = 0

        half_day = work_hours < 4.50

    # 6. Capture only values that actually changed.
    def epoch_ms(value):
        if value is None:
            return None
        return int(value.replace(tzinfo=timezone.utc).timestamp() * 1000)

    old_values = {
        "status": record["status"],
        "punch_in": epoch_ms(old_in),
        "punch_out": epoch_ms(old_out),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }

    new_values = {
        "status": new_status,
        "punch_in": epoch_ms(new_in),
        "punch_out": epoch_ms(new_out),
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }

    changes = {}
    for field, new_value in new_values.items():
        if old_values[field] != new_value:
            changes[field] = {
                "from": old_values[field],
                "to": new_value,
            }

    if not changes:
        raise HTTPException(422, "request makes no changes")

    # 7. Atomically update the record and append audit history.
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    history_entry = {
        "at": datetime.fromtimestamp(
            now_ms / 1000, tz=timezone.utc
        ).replace(tzinfo=None),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Match the old values as well as _id, so concurrent corrections
    # cannot overwrite one another's history.
    filter_query = {
        "_id": record["_id"],
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "history": record.get("history", []),
    }

    updated = db.attendance_logs.find_one_and_update(
        filter_query,
        {
            "$set": {
                "status": new_status,
                "punch_in": new_in,
                "punch_out": new_out,
                "work_hours": work_hours,
                "late_minutes": late_minutes,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(
            409, "record changed concurrently; retry the correction"
        )

    # 8. Serialize timestamps as epoch milliseconds; never expose _id.
    updated.pop("_id", None)
    for field in ("punch_in", "punch_out"):
        updated[field] = epoch_ms(as_utc_naive(updated[field]))

    for entry in updated.get("history", []):
        entry["at"] = epoch_ms(as_utc_naive(entry["at"]))
        for field_change in entry["changes"].values():
            # Existing history entries may store datetime values.
            for key in ("from", "to"):
                value = field_change.get(key)
                if isinstance(value, datetime):
                    field_change[key] = epoch_ms(as_utc_naive(value))

    return updated

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    from datetime import date as date_type, timedelta
    from decimal import Decimal, ROUND_HALF_UP
    import re

    # 1. Validate month format: YYYY-MM
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    year, month_number = map(int, month.split("-"))

    try:
        month_start = date_type(year, month_number, 1)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Invalid month",
        )

    next_month = (
        date_type(year + 1, 1, 1)
        if month_number == 12
        else date_type(year, month_number + 1, 1)
    )

    month_end = next_month - timedelta(days=1)

    # 2. Find employee
    employee = db.employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0},
    )

    if employee is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    # 3. Calculate working days from joining date through month-end.
    # Employees who join after this month have zero working days.
    joined_on = date_type.fromisoformat(
        str(employee["joined_on"])[:10]
    )

    working_days = 0

    first_working_date = max(month_start, joined_on)

    if first_working_date <= month_end:
        current_date = first_working_date

        while current_date <= month_end:
            if current_date.weekday() < 5:
                working_days += 1

            current_date += timedelta(days=1)

    # 4. Aggregate attendance metrics in MongoDB
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lt": next_month.isoformat(),
                },
            }
        },
        {
            "$addFields": {
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {
                            "dateString": "$date",
                            "format": "%Y-%m-%d",
                        }
                    }
                }
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
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$gte": ["$day_of_week", 2]},
                                    {"$lte": ["$day_of_week", 6]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },

                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },

                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },

                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },

                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    results = list(db.attendance_logs.aggregate(pipeline))

    # 5. Use zero totals if the employee has no attendance records.
    metrics = results[0] if results else {}

    present_days = metrics.get("present_days", 0)
    leave_days = metrics.get("leave_days", 0)
    late_count = metrics.get("late_count", 0)
    total_late_minutes = metrics.get("total_late_minutes", 0)
    total_overtime_minutes = metrics.get("total_overtime_minutes", 0)

    # 6. Calculate attendance percentage with half-up rounding.
    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = float(
            (
                Decimal(str(present_days))
                / Decimal(working_days)
                * Decimal("100")
            ).quantize(
                Decimal("0.0001"),
                rounding=ROUND_HALF_UP,
            )
        )

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
def department_summary(month: str, department: Optional[str] = None):
    import re
    import calendar
    from datetime import date as date_type
    from decimal import Decimal, ROUND_HALF_UP

    # 1. Validate month format
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    year, month_number = map(int, month.split("-"))

    try:
        month_start = date_type(year, month_number, 1)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid month")

    last_day = calendar.monthrange(year, month_number)[1]
    month_end = date_type(year, month_number, last_day)

    next_month = (
        date_type(year + 1, 1, 1)
        if month_number == 12
        else date_type(year, month_number + 1, 1)
    )

    month_start_str = month_start.isoformat()
    month_end_str = month_end.isoformat()
    next_month_str = next_month.isoformat()

    # 2. Filter eligible employees by joining date and optional department
    employee_match = {
        "joined_on": {"$lte": month_end_str}
    }

    if department:
        employee_match["department"] = department


    attendance_employee_match = {
        "employee.joined_on": {"$lte": month_end_str}
    }

    if department:
        attendance_employee_match["employee.department"] = department

    # 3. Aggregate attendance records in MongoDB, by department
    attendance_pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": month_start_str,
                    "$lt": next_month_str,
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
        {"$match": attendance_employee_match},
        {
            "$addFields": {
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {
                            "dateString": "$date",
                            "format": "%Y-%m-%d",
                        }
                    }
                }
            }
        },
        {
            "$group": {
                "_id": "$employee.department",

                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$gte": ["$day_of_week", 2]},
                                    {"$lte": ["$day_of_week", 6]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },

                # Average across individual eligible attendance records
                "avg_work_hours": {
                    "$avg": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            "$work_hours",
                            None,
                        ]
                    }
                },

                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },

                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },

                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },

                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
    ]

    attendance_results = list(
        db.attendance_logs.aggregate(attendance_pipeline)
    )

    # 4. Count eligible employees per department, including employees
    #    who have no attendance records this month.
    headcount_pipeline = [
        {"$match": employee_match},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
            }
        },
    ]

    headcounts = list(db.employees.aggregate(headcount_pipeline))

    # 5. Merge aggregation results by department.
    # These are department-level results, not loops over attendance docs.
    attendance_by_department = {
        item["_id"]: item for item in attendance_results
    }

    items = []

    for group in headcounts:
        department_name = group["_id"]
        attendance = attendance_by_department.get(department_name, {})

        average = attendance.get("avg_work_hours")

        if average is not None:
            average = float(
                Decimal(str(average)).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP,
                )
            )

        items.append({
            "department": department_name,
            "headcount": group["headcount"],
            "present_days": attendance.get("present_days", 0),
            "avg_work_hours": average,
            "late_count": attendance.get("late_count", 0),
            "total_late_minutes": attendance.get(
                "total_late_minutes", 0
            ),
            "leave_count": attendance.get("leave_count", 0),
            "on_duty_count": attendance.get("on_duty_count", 0),
        })

    items.sort(key=lambda item: item["department"] or "")

    return {
        "month": month,
        "items": items,
    }

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = 10,
    department: Optional[str] = None,
):
    import re
    from datetime import date as date_type

    # 1. Validate the month.
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    year, month_number = map(int, month.split("-"))

    try:
        month_start = date_type(year, month_number, 1)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid month")

    if limit < 1 or limit > 50:
        raise HTTPException(
            status_code=422,
            detail="limit must be between 1 and 50",
        )

    next_month = (
        date_type(year + 1, 1, 1)
        if month_number == 12
        else date_type(year, month_number + 1, 1)
    )

    # 2. Filter attendance records for the requested month.
    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lt": next_month.isoformat(),
                }
            }
        },

        # 3. Join attendance records with existing employees.
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},

        # 4. Optionally filter by department.
        {
            "$match": (
                {"employee.department": department}
                if department
                else {}
            )
        },

        # 5. Total late minutes per employee.
        {
            "$group": {
                "_id": "$emp_code",
                "name": {"$first": "$employee.name"},
                "department": {"$first": "$employee.department"},
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
            }
        },

        # 6. Exclude employees with no late minutes.
        {
            "$match": {
                "total_late_minutes": {"$gt": 0}
            }
        },

        # 7. Sort before calculating competition ranks.
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1,
            }
        },

        # 8. Equal totals receive equal ranks; the next rank is skipped.
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },

        # 9. Apply the limit to rank, retaining ties.
        {"$match": {"rank": {"$lte": limit}}},

        # 10. Return the required display order and field names.
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
                "rank": 1,
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
    ]

    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": items,
    }

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(None, alias="from"),
    to_date: str = Query(None, alias="to"),
):
    import re
    from datetime import date as date_type, datetime, timedelta
    from decimal import Decimal, ROUND_HALF_UP

    # 1. Validate the dates.
    if not from_date or not to_date:
        raise HTTPException(
            status_code=422,
            detail="from and to query parameters are required",
        )

    try:
        start_date = date_type.fromisoformat(from_date)
        end_date = date_type.fromisoformat(to_date)

        if start_date.isoformat() != from_date:
            raise ValueError
        if end_date.isoformat() != to_date:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="from and to must use YYYY-MM-DD format",
        )

    if end_date < start_date:
        raise HTTPException(
            status_code=422,
            detail="to must not be before from",
        )

    if (end_date - start_date).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="date range must not exceed 92 days",
        )

    # 2. Check that the department exists.
    if not db.employees.find_one(
        {"department": department},
        {"_id": 1},
    ):
        raise HTTPException(
            status_code=404,
            detail="Department not found",
        )

    # BSON dates used by MongoDB's $densify stage.
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_exclusive_dt = datetime.combine(
        end_date + timedelta(days=1),
        datetime.min.time(),
    )

    # 3. Seed one date, then fill every calendar day in the range.
    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateFromString": {
                        "dateString": from_date,
                        "format": "%Y-%m-%d",
                    }
                },
            }
        },
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_exclusive_dt],
                },
            }
        },
        {
            "$set": {
                "date_string": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date",
                    }
                },
                "is_working_day": {
                    "$and": [
                        {
                            "$gte": [
                                {"$dayOfWeek": "$date"},
                                2,
                            ]
                        },
                        {
                            "$lte": [
                                {"$dayOfWeek": "$date"},
                                6,
                            ]
                        },
                    ]
                },
            }
        },

        # 4. Count employees who have joined by each date.
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date_string"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "headcount_result",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {
                            "$arrayElemAt": [
                                "$headcount_result.count",
                                0,
                            ]
                        },
                        0,
                    ]
                }
            }
        },

        # 5. Get the day's attendance records for this department.
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date_string"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$eq": ["$date", "$$day"]
                            }
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "employee",
                        }
                    },
                    {"$unwind": "$employee"},
                    {
                        "$match": {
                            "$expr": {
                                "$eq": [
                                    "$employee.department",
                                    department,
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                [
                                                    "PRESENT",
                                                    "WFH",
                                                    "ON_DUTY",
                                                ],
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                "$late_minutes",
                                                0,
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "daily_metrics",
            }
        },
        {
            "$set": {
                "present_count": {
                    "$ifNull": [
                        {
                            "$arrayElemAt": [
                                "$daily_metrics.present_count",
                                0,
                            ]
                        },
                        0,
                    ]
                },
                "late_count": {
                    "$ifNull": [
                        {
                            "$arrayElemAt": [
                                "$daily_metrics.late_count",
                                0,
                            ]
                        },
                        0,
                    ]
                },
            }
        },

        # 6. Calculate attendance rate, rounded half-up to four decimals.
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {
                                                        "$divide": [
                                                            "$present_count",
                                                            "$headcount",
                                                        ]
                                                    },
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },

        # 7. Moving average over the current day and previous six rows.
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_raw": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0],
                        },
                    }
                },
            }
        },

        # 8. Return exactly the contract's fields.

        {
            "$project": {
                "_id": 0,
                "date": "$date_string",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$moving_avg_raw", None]},
                        None,
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    "$moving_avg_raw",
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                    ]
                },
            }
        },

        {"$sort": {"date": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))

    return {
        "department": department,
        "items": items,
    }

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = Query(None, alias="date_from"),
    date_to: Optional[str] = Query(None, alias="date_to"),
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = 1,
    page_size: int = 20,
):
    allowed_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed_endpoints:
        raise HTTPException(
            status_code=422,
            detail="Invalid endpoint name",
        )

    # First implementation: explain the attendance-list query.
    if endpoint == "attendance_list":
        if page < 1:
            raise HTTPException(422, "page must be at least 1")

        if page_size < 1 or page_size > 100:
            raise HTTPException(
                422,
                "page_size must be between 1 and 100",
            )

        allowed_statuses = {
            "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
        }

        if status is not None and status not in allowed_statuses:
            raise HTTPException(422, "Invalid status")

        from datetime import date as date_type

        for field_name, value in [
            ("date_from", date_from),
            ("date_to", date_to),
        ]:
            if value is not None:
                try:
                    parsed = date_type.fromisoformat(value)
                    if parsed.isoformat() != value:
                        raise ValueError
                except ValueError:
                    raise HTTPException(
                        422,
                        f"{field_name} must use YYYY-MM-DD format",
                    )

        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                422,
                "date_from must not be after date_to",
            )

        query = {}

        if emp_code:
            query["emp_code"] = emp_code

        if date_from or date_to:
            date_filter = {}
            if date_from:
                date_filter["$gte"] = date_from
            if date_to:
                date_filter["$lte"] = date_to
            query["date"] = date_filter

        if status:
            query["status"] = status

        skip = (page - 1) * page_size

        explain_result = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "projection": {"_id": 0, "id": 0},
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )

        return JSONResponse(
            content=json.loads(dumps({
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": explain_result,
            }))
        )


    if endpoint == "employee_monthly":
        import re
        from datetime import date as date_type

        # These parameters are required for this endpoint.
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required",
            )

        # Validate month format.
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(
                status_code=422,
                detail="month must use YYYY-MM format",
            )

        year, month_number = map(int, month.split("-"))

        month_start = date_type(year, month_number, 1)

        next_month = (
            date_type(year + 1, 1, 1)
            if month_number == 12
            else date_type(year, month_number + 1, 1)
        )

        # Confirm the employee exists.
        employee = db.employees.find_one(
            {"emp_code": emp_code},
            {"_id": 1},
        )

        if employee is None:
            raise HTTPException(
                status_code=404,
                detail="Employee not found",
            )

        # Same aggregation logic as employee_monthly().
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": month_start.isoformat(),
                        "$lt": next_month.isoformat(),
                    },
                }
            },
            {
                "$addFields": {
                    "day_of_week": {
                        "$dayOfWeek": {
                            "$dateFromString": {
                                "dateString": "$date",
                                "format": "%Y-%m-%d",
                            }
                        }
                    }
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
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {"$gte": ["$day_of_week", 2]},
                                        {"$lte": ["$day_of_week", 6]},
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$half_day", True]},
                                        0.5,
                                        1,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$status", "LEAVE"]},
                                1,
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": ["$late_minutes", 0]},
                                1,
                                0,
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                    "total_overtime_minutes": {
                        "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                    },
                }
            },
        ]

        explain_result = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        return JSONResponse(
            content=json.loads(dumps({
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": explain_result,
            }))
        )


    if endpoint == "department_summary":
        import re
        import calendar
        from datetime import date as date_type

        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required in YYYY-MM format",
            )

        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(
                status_code=422,
                detail="month must use YYYY-MM format",
            )

        year, month_number = map(int, month.split("-"))

        try:
            month_start = date_type(year, month_number, 1)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid month")

        last_day = calendar.monthrange(year, month_number)[1]
        month_end = date_type(year, month_number, last_day)

        next_month = (
            date_type(year + 1, 1, 1)
            if month_number == 12
            else date_type(year, month_number + 1, 1)
        )

        month_start_str = month_start.isoformat()
        month_end_str = month_end.isoformat()
        next_month_str = next_month.isoformat()

        employee_match = {
            "joined_on": {"$lte": month_end_str}
        }

        if department:
            employee_match["department"] = department

        attendance_employee_match = {
            "employee.joined_on": {"$lte": month_end_str}
        }

        if department:
            attendance_employee_match["employee.department"] = department

        attendance_pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": month_start_str,
                        "$lt": next_month_str,
                    }
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
            {"$match": attendance_employee_match},
            {
                "$addFields": {
                    "day_of_week": {
                        "$dayOfWeek": {
                            "$dateFromString": {
                                "dateString": "$date",
                                "format": "%Y-%m-%d",
                            }
                        }
                    }
                }
            },
            {
                "$group": {
                    "_id": "$employee.department",
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {"$gte": ["$day_of_week", 2]},
                                        {"$lte": ["$day_of_week", 6]},
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$half_day", True]},
                                        0.5,
                                        1,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "avg_work_hours": {
                        "$avg": {
                            "$cond": [
                                {
                                    "$and": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {"$ne": ["$work_hours", None]},
                                    ]
                                },
                                "$work_hours",
                                None,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": ["$late_minutes", 0]},
                                1,
                                0,
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                    "leave_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$status", "LEAVE"]},
                                1,
                                0,
                            ]
                        }
                    },
                    "on_duty_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$status", "ON_DUTY"]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
        ]

        headcount_pipeline = [
            {"$match": employee_match},
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                }
            },
        ]

        attendance_explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": attendance_pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        headcount_explain = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": headcount_pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        return JSONResponse(
            content=json.loads(dumps({
                "endpoint": endpoint,
                "explain": {
                    "attendance_pipeline": attendance_explain,
                    "headcount_pipeline": headcount_explain,
                },
            }))
        )


    if endpoint == "late_leaderboard":
        import re
        from datetime import date as date_type

        if not month or not re.fullmatch(
            r"\d{4}-(0[1-9]|1[0-2])", month
        ):
            raise HTTPException(
                status_code=422,
                detail="month must use YYYY-MM format",
            )

        year, month_number = map(int, month.split("-"))
        month_start = date_type(year, month_number, 1)

        next_month = (
            date_type(year + 1, 1, 1)
            if month_number == 12
            else date_type(year, month_number + 1, 1)
        )

        if limit < 1 or limit > 50:
            raise HTTPException(
                status_code=422,
                detail="limit must be between 1 and 50",
            )

        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": month_start.isoformat(),
                        "$lt": next_month.isoformat(),
                    }
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
            {
                "$match": (
                    {"employee.department": department}
                    if department
                    else {}
                )
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                }
            },
            {
                "$match": {
                    "total_late_minutes": {"$gt": 0}
                }
            },
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "_id": 1,
                }
            },
            {
                "$limit": limit
            },
        ]

        explain_result = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        return JSONResponse(
            content=json.loads(dumps({
                "endpoint": endpoint,
                "collection": "attendance_logs",
                "explain": explain_result,
            }))
        )

    if endpoint == "department_trend":
        from datetime import date as date_type, datetime, timedelta

        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to are required",
            )

        try:
            start_date = date_type.fromisoformat(from_date)
            end_date = date_type.fromisoformat(to_date)

            if (
                start_date.isoformat() != from_date
                or end_date.isoformat() != to_date
            ):
                raise ValueError
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="from and to must use YYYY-MM-DD format",
            )

        if end_date < start_date:
            raise HTTPException(
                status_code=422,
                detail="to must not be before from",
            )

        if (end_date - start_date).days + 1 > 92:
            raise HTTPException(
                status_code=422,
                detail="date range must not exceed 92 days",
            )

        if not db.employees.find_one(
            {"department": department},
            {"_id": 1},
        ):
            raise HTTPException(
                status_code=404,
                detail="Department not found",
            )

        start_dt = datetime.combine(start_date, datetime.min.time())
        end_exclusive_dt = datetime.combine(
            end_date + timedelta(days=1),
            datetime.min.time(),
        )

        # Keep this pipeline in sync with department_trend().
        pipeline = [
            {"$match": {"department": department}},
            {"$limit": 1},
            {
                "$project": {
                    "_id": 0,
                    "date": {
                        "$dateFromString": {
                            "dateString": from_date,
                            "format": "%Y-%m-%d",
                        }
                    },
                }
            },
            {
                "$densify": {
                    "field": "date",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [start_dt, end_exclusive_dt],
                    },
                }
            },
            {
                "$set": {
                    "date_string": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$date",
                        }
                    },
                    "is_working_day": {
                        "$and": [
                            {
                                "$gte": [
                                    {"$dayOfWeek": "$date"},
                                    2,
                                ]
                            },
                            {
                                "$lte": [
                                    {"$dayOfWeek": "$date"},
                                    6,
                                ]
                            },
                        ]
                    },
                }
            },

            # 4. Count employees who have joined by each date.
            {
                "$lookup": {
                    "from": "employees",
                    "let": {"day": "$date_string"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$department", department]},
                                        {"$lte": ["$joined_on", "$$day"]},
                                    ]
                                }
                            }
                        },
                        {"$count": "count"},
                    ],
                    "as": "headcount_result",
                }
            },
            {
                "$set": {
                    "headcount": {
                        "$ifNull": [
                            {
                                "$arrayElemAt": [
                                    "$headcount_result.count",
                                    0,
                                ]
                            },
                            0,
                        ]
                    }
                }
            },

            # 5. Get the day's attendance records for this department.
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"day": "$date_string"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$eq": ["$date", "$$day"]
                                }
                            }
                        },
                        {
                            "$lookup": {
                                "from": "employees",
                                "localField": "emp_code",
                                "foreignField": "emp_code",
                                "as": "employee",
                            }
                        },
                        {"$unwind": "$employee"},
                        {
                            "$match": {
                                "$expr": {
                                    "$eq": [
                                        "$employee.department",
                                        department,
                                    ]
                                }
                            }
                        },
                        {
                            "$group": {
                                "_id": None,
                                "present_count": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$in": [
                                                    "$status",
                                                    [
                                                        "PRESENT",
                                                        "WFH",
                                                        "ON_DUTY",
                                                    ],
                                                ]
                                            },
                                            {
                                                "$cond": [
                                                    {"$eq": ["$half_day", True]},
                                                    0.5,
                                                    1,
                                                ]
                                            },
                                            0,
                                        ]
                                    }
                                },
                                "late_count": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$gt": [
                                                    "$late_minutes",
                                                    0,
                                                ]
                                            },
                                            1,
                                            0,
                                        ]
                                    }
                                },
                            }
                        },
                    ],
                    "as": "daily_metrics",
                }
            },
            {
                "$set": {
                    "present_count": {
                        "$ifNull": [
                            {
                                "$arrayElemAt": [
                                    "$daily_metrics.present_count",
                                    0,
                                ]
                            },
                            0,
                        ]
                    },
                    "late_count": {
                        "$ifNull": [
                            {
                                "$arrayElemAt": [
                                    "$daily_metrics.late_count",
                                    0,
                                ]
                            },
                            0,
                        ]
                    },
                }
            },

            # 6. Calculate attendance rate, rounded half-up to four decimals.
            {
                "$set": {
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_working_day",
                                    {"$gt": ["$headcount", 0]},
                                ]
                            },
                            {
                                "$divide": [
                                    {
                                        "$floor": {
                                            "$add": [
                                                {
                                                    "$multiply": [
                                                        {
                                                            "$divide": [
                                                                "$present_count",
                                                                "$headcount",
                                                            ]
                                                        },
                                                        10000,
                                                    ]
                                                },
                                                0.5,
                                            ]
                                        }
                                    },
                                    10000,
                                ]
                            },
                            None,
                        ]
                    }
                }
            },

            # 7. Moving average over the current day and previous six rows.
            {
                "$setWindowFields": {
                    "sortBy": {"date": 1},
                    "output": {
                        "moving_avg_raw": {
                            "$avg": "$attendance_rate",
                            "window": {
                                "documents": [-6, 0],
                            },
                        }
                    },
                }
            },

            # 8. Return exactly the contract's fields.

            {
                "$project": {
                    "_id": 0,
                    "date": "$date_string",
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": 1,
                    "moving_avg_7d": {
                        "$cond": [
                            {"$eq": ["$moving_avg_raw", None]},
                            None,
                            {
                                "$divide": [
                                    {
                                        "$floor": {
                                            "$add": [
                                                {
                                                    "$multiply": [
                                                        "$moving_avg_raw",
                                                        10000,
                                                    ]
                                                },
                                                0.5,
                                            ]
                                        }
                                    },
                                    10000,
                                ]
                            },
                        ]
                    },
                }
            },

            {"$sort": {"date": 1}},
        ]

        explain_result = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        return JSONResponse(
            content=json.loads(dumps({
                "endpoint": endpoint,
                "collection": "employees",
                "explain": explain_result,
            }))
        )

    raise HTTPException(
        status_code=422,
        detail=f"Explain implementation pending for {endpoint}",
    )

# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
