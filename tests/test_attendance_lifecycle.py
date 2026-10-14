from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

def test_punch_in_success_normal_shift(client):
    # Punch in for EMP0001 on a new date: 2026-07-20 at 09:28:00 IST
    dt_ist = datetime(2026, 7, 20, 9, 28, 0, tzinfo=IST)
    ms = int(dt_ist.timestamp() * 1000)

    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms, "status": "PRESENT"})
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["date"] == "2026-07-20"
    assert data["status"] == "PRESENT"
    assert data["late_minutes"] == 0
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert data["half_day"] is False
    assert data["history"] == []
    assert "_id" not in data
    assert "id" not in data

def test_punch_in_late_minutes_rule_r2(client):
    # Shift start is 09:30. Grace period 10 minutes (09:40:00).
    # 1. Exactly 09:40:00 -> late_minutes = 0
    dt_grace = datetime(2026, 7, 21, 9, 40, 0, tzinfo=IST)
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": int(dt_grace.timestamp() * 1000)})
    assert res.status_code == 201
    assert res.json()["late_minutes"] == 0

    # 2. 09:40:01 -> strictly > 10 min -> late_minutes = 10 (floor minutes since 09:30)
    dt_late1 = datetime(2026, 7, 22, 9, 40, 1, tzinfo=IST)
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": int(dt_late1.timestamp() * 1000)})
    assert res.status_code == 201
    assert res.json()["late_minutes"] == 10

    # 3. 10:15:59 -> strictly > 10 min -> late_minutes = 45 (floor minutes since 09:30)
    dt_late2 = datetime(2026, 7, 23, 10, 15, 59, tzinfo=IST)
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": int(dt_late2.timestamp() * 1000)})
    assert res.status_code == 201
    assert res.json()["late_minutes"] == 45

def test_punch_in_overnight_shift_r1(client):
    # EMP0005 has overnight shift: 22:00 to 06:00
    # Punch in on 2026-07-21 at 01:15:00 IST (earlier than shift_end 06:00)
    # R1: belongs to the shift that started the previous day (2026-07-20)!
    dt_overnight = datetime(2026, 7, 21, 1, 15, 0, tzinfo=IST)
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": int(dt_overnight.timestamp() * 1000)})
    assert res.status_code == 201
    data = res.json()
    assert data["date"] == "2026-07-20"

def test_punch_in_duplicate_conflict(client):
    dt = datetime(2026, 7, 6, 9, 28, 0, tzinfo=IST)
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": int(dt.timestamp() * 1000)})
    assert res.status_code == 409
    assert "already punched in" in res.json()["detail"]

def test_punch_in_unknown_employee(client):
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert res.status_code == 404

def test_punch_out_success_and_derived_rules(client):
    # EMP0006 on 2026-07-07 has an open punch-in at 04:05:00 UTC (09:35 IST)
    # Let's punch out at 13:40:00 UTC (19:10 IST)
    # Work time: 13:40 - 04:05 = 9 hours 35 minutes = 34500 seconds / 3600 = 9.58333... -> 9.58 hrs
    # Shift end is 18:30 IST. Punch out is 19:10 IST -> 40 minutes after shift end (>= 30) -> overtime = 40
    # work_hours 9.58 >= 4.50 -> half_day = False
    dt_out = datetime(2026, 7, 7, 13, 40, 0, tzinfo=timezone.utc)
    ms_out = int(dt_out.timestamp() * 1000)

    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0006", "punched_at": ms_out})
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0006"
    assert data["date"] == "2026-07-07"
    assert data["punch_out"] == ms_out
    assert data["work_hours"] == 9.58
    assert data["overtime_minutes"] == 40
    assert data["half_day"] is False

    # Second punch-out must yield 409
    res_dup = client.post("/attendance/punch-out", json={"emp_code": "EMP0006", "punched_at": ms_out})
    assert res_dup.status_code == 409

def test_punch_out_half_day_rule_r5(client):
    # Create an employee and punch in
    client.post("/employees", json={
        "emp_code": "EMP0088", "name": "Part Time", "email": "part@example.com",
        "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-01"
    })
    dt_in = datetime(2026, 7, 15, 9, 30, 0, tzinfo=IST)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0088", "punched_at": int(dt_in.timestamp() * 1000)})

    # Punch out after 4 hours 10 minutes = 4.17 hours (< 4.50)
    dt_out = dt_in + timedelta(hours=4, minutes=10)
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0088", "punched_at": int(dt_out.timestamp() * 1000)})
    assert res.status_code == 200
    data = res.json()
    assert data["work_hours"] == 4.17
    assert data["half_day"] is True

def test_punch_out_invalid_timing_422(client):
    # Punch out earlier than punch in
    dt_past = datetime(2026, 7, 6, 8, 0, 0, tzinfo=IST)
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0006", "punched_at": int(dt_past.timestamp() * 1000)})
    assert res.status_code == 422

def test_list_attendance(client):
    res = client.get("/attendance?date_from=2026-07-06&date_to=2026-07-08")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 8
    # Sorted by date descending, then emp_code ascending
    dates = [d["date"] for d in data["items"]]
    assert dates == sorted(dates, reverse=True)

def test_regularize_attendance_success_and_audit_history(client):
    # EMP0001 on 2026-07-06: PRESENT, punch_in 03:58:00 UTC (09:28 IST)
    # Correct punch_in to 04:35:00 UTC (10:05 IST) -> late_minutes becomes 35
    dt_new_in = datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc)
    ms_new_in = int(dt_new_in.timestamp() * 1000)

    payload = {
        "reason": "Correcting biometric device recording",
        "regularized_by": "manager.kumar",
        "punch_in": ms_new_in,
    }

    res = client.patch("/attendance/EMP0001/2026-07-06", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["late_minutes"] == 35
    assert len(data["history"]) == 1
    h = data["history"][0]
    assert h["by"] == "manager.kumar"
    assert h["reason"] == "Correcting biometric device recording"
    assert "punch_in" in h["changes"]
    assert "late_minutes" in h["changes"]
    assert h["changes"]["late_minutes"]["from"] == 0
    assert h["changes"]["late_minutes"]["to"] == 35

def test_regularize_to_absent_clears_punches(client):
    payload = {
        "status": "ABSENT",
        "reason": "Employee informed no-show",
        "regularized_by": "hr.lead",
    }
    res = client.patch("/attendance/EMP0001/2026-07-06", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ABSENT"
    assert data["punch_in"] is None
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert data["late_minutes"] == 0
    assert data["half_day"] is False

def test_regularize_no_changes_422(client):
    payload = {
        "status": "PRESENT",
        "reason": "Accidental submit without change",
        "regularized_by": "hr.lead",
    }
    res = client.patch("/attendance/EMP0001/2026-07-06", json=payload)
    assert res.status_code == 422
    assert "No changes detected" in res.json()["detail"]
