# Employee Attendance & Analytics API

A high-performance, production-grade backend service for employee attendance tracking, manual audit-trail regularizations, and database-native analytics. Built with **FastAPI**, **MongoDB 7.0+**, and **PyMongo**, following the official HROne software engineering specification.

---

## 1. Tech Stack & Environment

- **Python**: 3.11+ (tested on Python 3.13)
- **Framework**: FastAPI (Asynchronous ASGI application with Pydantic v2 validation)
- **Database**: MongoDB 6.0+ / 7.0+ (utilizing aggregation pipelines, `$densify`, and `$setWindowFields`)
- **Driver**: PyMongo 4.7+
- **Testing**: Pytest & HTTPX TestClient

---

## 2. Core Features & Business Rules

1. **Strict Attendance Lifecycle**:
   - **R1 Timezone & Attendance Date**: Shift times and attendance days are calculated in Indian Standard Time (IST, UTC+05:30). Overnight shifts (`shift_end <= shift_start`, e.g., 22:00–06:00) map punch-ins before shift end to the shift that started the previous day. All instants are truncated to whole seconds.
   - **R2 Late Grace Period**: Strict 10-minute grace window from `shift_start`. Punch-ins strictly after grace calculate floor minutes since `shift_start` (e.g. 09:40:00 -> 0 min; 09:40:01 -> 10 min; 10:15:59 -> 45 min).
   - **R3 Overtime Calculation**: Whole minutes from `shift_end` to `punch_out`, counted only if $\ge 30$ minutes; otherwise 0. Properly spans midnight for overnight shifts.
   - **R4 Work Hours**: Exact duration in hours rounded to 2 decimal places using half-up arithmetic (`Decimal.quantize(..., rounding=ROUND_HALF_UP)`).
   - **R5 Half Day**: Flags `half_day = True` when rounded `work_hours < 4.50`. Counts as 0.5 of a present day.
   - **R6 Presence Status**: `PRESENT`, `WFH`, and `ON_DUTY` count as present; `ABSENT` and `LEAVE` do not.
   - **R7 Working Days**: Monday through Friday calendar logic. Handles mid-month joiners by counting working days from `joined_on`.

2. **Concurrency & Race Condition Prevention**:
   - Unique database constraint on natural key `(emp_code, date)` prevents simultaneous duplicate punch-ins at the storage engine level, returning `409 Conflict`.
   - Atomic document updates (`find_one_and_update`) prevent race conditions on simultaneous punch-outs.
   - Optimistic locking on history array length prevents concurrent regularizations from silently losing audit trails.

3. **Database-Native Aggregations (No Python In-Memory Loops)**:
   - **Employee Monthly Summary**: Accurately accounts for mid-month joiners, weekdays, half-days, and attendance rate.
   - **Department Summary**: Starts on `employees` collection to guarantee zero-log employees are accounted for in headcount (R9). Averages work hours across present records with recorded times.
   - **Late Leaderboard**: Implements standard competition ranking (1, 2, 2, 4) using `$rank: {}` in MongoDB `$setWindowFields`. Accurately handles ties at the `limit` boundary.
   - **Department Trend**: Employs `$densify` for continuous daily series across weekends and non-record days, alongside a 7-day moving window average via `$setWindowFields`.

4. **Query Performance & Explain Verification**:
   - All query patterns and aggregation pipelines are backed by compound indexes, ensuring `IXSCAN` operations and zero `COLLSCAN`.
   - `GET /admin/explain/{endpoint}` exposes raw `executionStats` MongoDB plans for query audit.

---

## 3. Project Structure

```text
hrone-attendance-api/
├── app/
│   ├── __init__.py
│   └── main.py              # Complete application code (FastAPI routes, helpers, models, aggregations)
├── sample_data/
│   ├── employees.json       # Seed documents for employees
│   └── attendance_logs.json # Seed documents for attendance logs
├── tests/
│   ├── conftest.py          # Pytest fixtures and database seeding harness
│   ├── test_system_and_employees.py
│   ├── test_attendance_lifecycle.py
│   ├── test_analytics.py
│   └── test_concurrency_and_edge_cases.py
├── DATA_MODEL.md            # Stored data specifications
├── DECISIONS.md             # Key technical decisions and concurrency trade-offs
├── openapi.yaml             # API contract and OpenAPI 3.0 specification
├── PROBLEM_STATEMENT.docx   # Original assignment specification
├── pytest.ini               # Pytest path and runner configuration
├── README.md                # Project documentation
├── requirements.txt         # Project dependencies
├── REVIEW.md                # Detailed defect catalog and remediation report
└── sample_seed.py           # Seed script for populating MongoDB
```

---

## 4. Setup & Running

### Prerequisites
- Python 3.11+
- MongoDB 6.0+ or 7.0+ running locally or on MongoDB Atlas (Free Tier)

### Environment Variables
The application reads configuration from environment variables (defaults supplied):
- `MONGO_URI`: MongoDB connection string (default: `mongodb://localhost:27017`)
- `MONGO_DB`: Target database name (default: `attendance_db`)

### Installation (Windows PowerShell)

```powershell
# 1. Create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt

# 3. Seed sample data into MongoDB
python sample_seed.py

# 4. Start the server
uvicorn app.main:app --port 8000 --reload
```

### Installation (Linux / macOS)

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Seed sample data into MongoDB
python sample_seed.py

# 4. Start the server
uvicorn app.main:app --port 8000 --reload
```

---

## 5. API Documentation & Interactive Exploration

Once the server is running, the interactive Swagger UI is available at:
- **Swagger UI**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`

---

## 6. Example API Requests

### 1. Record Punch-In
```bash
curl -X POST "http://localhost:8000/attendance/punch-in" \
     -H "Content-Type: application/json" \
     -d '{"emp_code": "EMP0001", "punched_at": 1783310280000, "status": "PRESENT"}'
```

### 2. Record Punch-Out
```bash
curl -X POST "http://localhost:8000/attendance/punch-out" \
     -H "Content-Type: application/json" \
     -d '{"emp_code": "EMP0001", "punched_at": 1783343100000}'
```

### 3. Regularize Attendance (Audit Trail)
```bash
curl -X PATCH "http://localhost:8000/attendance/EMP0001/2026-07-06" \
     -H "Content-Type: application/json" \
     -d '{
       "reason": "Biometric device sync error",
       "regularized_by": "admin.sharma",
       "punch_in": 1783310280000
     }'
```

### 4. Department Summary Analytics
```bash
curl -X GET "http://localhost:8000/analytics/departments/summary?month=2026-07"
```

### 5. Late Leaderboard (Competition Ranking)
```bash
curl -X GET "http://localhost:8000/analytics/leaderboard/late?month=2026-07&limit=10"
```

### 6. Department Trend (Daily Gap-Filled Series)
```bash
curl -X GET "http://localhost:8000/analytics/departments/Sales/trend?from=2026-07-01&to=2026-07-10"
```

### 7. Query Plan Explain
```bash
curl -X GET "http://localhost:8000/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-31"
```

---

## 7. Running Tests

The test suite covers unit behaviors, edge cases, MongoDB aggregations, and concurrent race condition simulations:

```bash
# Run all tests via pytest
pytest -v
```

---

## 8. Implementation Notes & Verification Status

- **Status**: 100% complete. All endpoints specified in `openapi.yaml` and `PROBLEM_STATEMENT.docx` are implemented and verified.
- **Automated Tests**: 32 test cases passed, covering happy paths, error codes (404, 409, 422, 503), competition ranking ties, mid-month joiner working days, and concurrency protection.
- **Clean Repository**: No `.env`, secrets, Dockerfile, or temporary virtual environment artifacts are included.
