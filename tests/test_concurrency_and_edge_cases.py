import concurrent.futures
from datetime import datetime, timezone, timedelta
from app.main import db

IST = timezone(timedelta(hours=5, minutes=30))

def test_concurrent_punch_in_race_condition(client):
    # Two identical punch-in requests arrive at the exact same instant
    dt = datetime(2026, 7, 25, 9, 30, 0, tzinfo=IST)
    ms = int(dt.timestamp() * 1000)

    payload = {"emp_code": "EMP0001", "punched_at": ms, "status": "PRESENT"}

    # Use thread pool to send simultaneous requests
    def do_punch():
        return client.post("/attendance/punch-in", json=payload)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch) for _ in range(5)]
        responses = [f.result() for f in futures]

    status_codes = [r.status_code for r in responses]
    # Exactly one 201 Created and the rest 409 Conflict!
    assert status_codes.count(201) == 1
    assert status_codes.count(409) == 4

def test_leaderboard_ties_and_cutoff_rule(client):
    # Setup test: create 4 employees with ties
    # EMP1: 50 mins late -> Rank 1
    # EMP2: 30 mins late -> Rank 2
    # EMP3: 30 mins late -> Rank 2 (tie with EMP2)
    # EMP4: 10 mins late -> Rank 4 (rank 3 skipped!)
    db.employees.insert_many([
        {"emp_code": "EMP0011", "name": "Tie A", "email": "a@ex.com", "department": "Ops", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01", "created_at": datetime.now(timezone.utc)},
        {"emp_code": "EMP0012", "name": "Tie B", "email": "b@ex.com", "department": "Ops", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01", "created_at": datetime.now(timezone.utc)},
        {"emp_code": "EMP0013", "name": "Tie C", "email": "c@ex.com", "department": "Ops", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01", "created_at": datetime.now(timezone.utc)},
        {"emp_code": "EMP0014", "name": "Tie D", "email": "d@ex.com", "department": "Ops", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01", "created_at": datetime.now(timezone.utc)},
    ])
    db.attendance_logs.insert_many([
        {"emp_code": "EMP0011", "date": "2026-08-03", "status": "PRESENT", "punch_in": datetime.now(timezone.utc), "punch_out": None, "work_hours": None, "late_minutes": 50, "overtime_minutes": 0, "half_day": False, "history": []},
        {"emp_code": "EMP0012", "date": "2026-08-03", "status": "PRESENT", "punch_in": datetime.now(timezone.utc), "punch_out": None, "work_hours": None, "late_minutes": 30, "overtime_minutes": 0, "half_day": False, "history": []},
        {"emp_code": "EMP0013", "date": "2026-08-03", "status": "PRESENT", "punch_in": datetime.now(timezone.utc), "punch_out": None, "work_hours": None, "late_minutes": 30, "overtime_minutes": 0, "half_day": False, "history": []},
        {"emp_code": "EMP0014", "date": "2026-08-03", "status": "PRESENT", "punch_in": datetime.now(timezone.utc), "punch_out": None, "work_hours": None, "late_minutes": 10, "overtime_minutes": 0, "half_day": False, "history": []},
    ])

    # Request leaderboard with limit=2
    # Rules: "limit applies after ranking: return every employee whose rank <= limit."
    # Both EMP0012 and EMP0013 have rank=2, so rank <= 2 returns all 3 employees (EMP0011, EMP0012, EMP0013)!
    # Order of rows: total_late_minutes descending, then emp_code ascending
    res = client.get("/analytics/leaderboard/late?month=2026-08&limit=2&department=Ops")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3
    assert items[0]["emp_code"] == "EMP0011"
    assert items[0]["rank"] == 1
    assert items[1]["emp_code"] == "EMP0012"
    assert items[1]["rank"] == 2
    assert items[2]["emp_code"] == "EMP0013"
    assert items[2]["rank"] == 2

    # If limit=3, rank <= 3 still returns the same 3 employees because rank 3 is skipped!
    res3 = client.get("/analytics/leaderboard/late?month=2026-08&limit=3&department=Ops")
    assert len(res3.json()["items"]) == 3

    # If limit=4, EMP0014 is included with rank 4
    res4 = client.get("/analytics/leaderboard/late?month=2026-08&limit=4&department=Ops")
    items4 = res4.json()["items"]
    assert len(items4) == 4
    assert items4[3]["emp_code"] == "EMP0014"
    assert items4[3]["rank"] == 4

def test_department_summary_future_joiner_and_zero_logs(client):
    # Create an employee in Finance who joined in June 2026 (has 0 logs)
    # And an employee in Finance who joins in August 2026 (future joiner)
    db.employees.insert_many([
        {"emp_code": "EMP0050", "name": "Zero Logs", "email": "zero@ex.com", "department": "Finance", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-06-01", "created_at": datetime.now(timezone.utc)},
        {"emp_code": "EMP0051", "name": "Future Joiner", "email": "future@ex.com", "department": "Finance", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-08-01", "created_at": datetime.now(timezone.utc)},
    ])

    res = client.get("/analytics/departments/summary?month=2026-07&department=Finance")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    fin = items[0]
    # Headcount must count EMP0050 (0 logs) but exclude EMP0051 (future joiner) -> headcount = 1!
    assert fin["headcount"] == 1
    assert fin["present_days"] == 0.0
    assert fin["avg_work_hours"] is None
    assert fin["late_count"] == 0
    assert fin["total_late_minutes"] == 0
