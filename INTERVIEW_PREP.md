# Technical Interview Preparation Guide
## HROne Software Engineer Trainee — Attendance & Analytics API

This guide prepares you for the live technical interview and code walkthrough with HROne engineering leaders. It explains your exact codebase, the non-obvious engineering choices made, and how to defend them clearly and honestly.

---

## 1. End-to-End Architectural Walkthrough

### The Request Lifecycle
1. **Request Ingestion & Validation (FastAPI & Pydantic v2)**:
   - When a request hits an endpoint (e.g., `POST /attendance/punch-in`), Pydantic validates payload schemas, types, and constraints (e.g., `emp_code` regex `^EMP\d{4,6}$`, minimum/maximum values, time formats). Invalid payloads immediately return `422 Unprocessable Entity` with FastAPI's standard detail list.
2. **Business Domain Processing (Python & Decimal Math)**:
   - Dates and shift boundaries are resolved in **Indian Standard Time (IST, UTC+05:30)**.
   - For overnight shifts (where `shift_end <= shift_start`, e.g., 22:00 to 06:00), punch-ins before `shift_end` are attributed to the shift that began on the previous calendar day.
   - All duration calculations use Python's `Decimal` module configured with `ROUND_HALF_UP` to prevent banker's rounding errors.
3. **Database Layer (MongoDB 7.0+ & PyMongo)**:
   - Natural business keys (`emp_code`, `(emp_code, date)`) are enforced with database-level unique indexes.
   - Complex reporting queries are executed as **server-side MongoDB aggregation pipelines** (using `$densify`, `$setWindowFields`, `$lookup`, and `$group`), ensuring zero collection scans (`IXSCAN` everywhere) and avoiding memory-intensive Python in-memory loops.

---

## 2. Deep Dive: Key Functions & Design Rationale

### `init_indexes()`
- **Purpose**: Creates required unique and compound indexes idempotently during startup via the FastAPI `lifespan` context manager.
- **Indexes Created**:
  - `employees`: `{emp_code: 1}` (unique), `{department: 1, joined_on: 1, emp_code: 1}`.
  - `attendance_logs`: `{emp_code: 1, date: 1}` (unique), `{date: -1, emp_code: 1}`, `{emp_code: 1, punch_in: -1}`, `{date: 1, late_minutes: 1, emp_code: 1}`.
- **Why**: Eliminates `COLLSCAN` across all queries, satisfies explain benchmarks on 100k+ documents, and guarantees database-level race protection.

### `determine_attendance_date(punch_in_utc, shift_start, shift_end)`
- **Purpose**: Implements Rule R1. Maps any punch-in instant to its authoritative attendance date.
- **Edge Case Handled**: Overnight shift where an employee punches in at 01:15 AM on Tuesday. Because 01:15 is earlier than the 06:00 shift end, the shift belongs to Monday.

### `compute_late_minutes(punch_in_utc, att_date_str, shift_start)`
- **Purpose**: Implements Rule R2.
- **Strict Grace Period**: Evaluates `diff_sec > 600`. A punch at exactly 09:40:00 is within grace and gives 0. A punch at 09:40:01 is strictly after grace and yields 10 floor minutes since 09:30.

### `compute_work_hours(punch_in_utc, punch_out_utc)`
- **Purpose**: Implements Rule R4.
- **Formula**: `(punch_out - punch_in).total_seconds() / 3600`, rounded half-up to 2 decimals using `Decimal.quantize(..., rounding=ROUND_HALF_UP)`.

### `regularize_attendance(...)` (`PATCH /attendance/{emp_code}/{date}`)
- **Purpose**: Manual attendance correction with an append-only audit trail (`history`).
- **Optimistic Locking**: Uses `{"_id": record["_id"], "history": {"$size": len(record.get("history", []))}}`. If two admins edit simultaneously, the first write succeeds and the second triggers a 409 Conflict rather than overwriting history.

---

## 3. The Three Hard Analytics Pipelines

### 1. Department Summary (`GET /analytics/departments/summary`)
- **Challenge**: Headcount must include employees who have 0 logs in the month and exclude employees who joined after month-end.
- **Solution**: The pipeline originates on `employees` (matching `joined_on <= month_end`), groups by department to establish headcount, and uses `$lookup` to join the department's logs.

### 2. Late Leaderboard (`GET /analytics/leaderboard/late`)
- **Challenge**: Standard competition ranking (1, 2, 2, 4) where ties share ranks and subsequent ranks are skipped. `limit` applies after ranking.
- **Solution**: Uses `$setWindowFields` with `$rank: {}` sorted by `total_late_minutes` descending. The `limit` filter is `$match: { rank: { $lte: limit } }`, ensuring tied employees at the cutoff are not artificially truncated.

### 3. Department Trend (`GET /analytics/departments/{department}/trend`)
- **Challenge**: Returns exactly one row per calendar day across the range, including weekends and days with no records, with a 7-day moving average.
- **Solution**: Uses `$densify` on `date_dt` with unit `day` to fill missing days natively inside MongoDB. The moving average is calculated via `$setWindowFields` with `window: { documents: [-6, "current"] }` over non-null `attendance_rate` values.

---

## 4. Likely Technical Interview Questions & Answers

### Q1: Why did you use `Decimal` with `ROUND_HALF_UP` instead of Python's `round()`?
> *"Python's built-in `round()` implements banker's rounding (round-half-to-even), meaning `round(2.125, 2)` rounds down to `2.12` because 2 is even. Rule R4 explicitly requires round-half-up, where 2.125 must round to 2.13. Using `Decimal(str(seconds)) / Decimal('3600')` with `ROUND_HALF_UP` guarantees arithmetic precision without float approximation artifacts."*

### Q2: How does your code prevent race conditions if two identical punch-in requests arrive simultaneously?
> *"We do not rely on an application-level 'check-then-insert' pattern because two threads can both find no existing document and both proceed to write. Instead, we enforce a database-level unique compound index on `{emp_code: 1, date: 1}`. MongoDB serializes concurrent writes on the same index key; one insert succeeds and the other raises a `DuplicateKeyError` (code 11000), which FastAPI catches and translates into an HTTP 409 Conflict."*

### Q3: Why store timestamps as epoch milliseconds in API responses, but BSON dates in MongoDB?
> *"Epoch milliseconds are integer values without timezone ambiguity or parsing drift across client platforms. Inside MongoDB, BSON dates allow native date operators (`$dayOfWeek`, `$dateToString`, `$densify`) and range indexing, giving us the best of both worlds: robust database analytics and a clean, unambiguous client contract."*

### Q4: How would this architecture scale if attendance grew 100x (e.g. 10 million records)?
> *"While our compound indexes prevent collection scans, running multi-stage `$lookup` and `$densify` across millions of documents for every trend request introduces latency. At 100x scale, I would introduce daily pre-aggregated summary documents updated incrementally via MongoDB Change Streams or Celery background jobs. Real-time dashboard queries could then read pre-computed daily rollups in $O(1)$ time."*

---

## 5. Potential Live Coding Scenarios & How to Solve Them

### Scenario A: "Add a grace period configuration per employee"
- **Approach**: In `employees` schema, add an optional `grace_period_minutes: int = 10`. Update `compute_late_minutes()` to accept `grace_period_minutes` and evaluate `diff_sec > (grace_period_minutes * 60)`.

### Scenario B: "Add a filter for `half_day` in the attendance listing"
- **Approach**: Add `half_day: Optional[bool] = Query(None)` to `list_attendance()`. In the query dictionary `q`, if `half_day is not None`, set `q["half_day"] = half_day`.

### Scenario C: "Support half-day count in employee monthly summary"
- **Approach**: In `employee_monthly` aggregation, add `half_day_count: {"$sum": {"$cond": [{"$and": ["$is_weekday", {"$eq": ["$half_day", True]}]}, 1, 0]}}` and return it in the response model.
