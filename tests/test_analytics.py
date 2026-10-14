def test_employee_monthly_analytics_regular_employee(client):
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23
    assert data["present_days"] == 2.0
    assert data["leave_days"] == 0
    assert data["late_count"] == 0
    assert data["total_late_minutes"] == 0
    assert data["attendance_pct"] == 8.70  # (2.0 / 23) * 100 = 8.6956... -> 8.70

def test_employee_monthly_analytics_mid_month_joiner(client):
    # EMP0002 joined on 2026-07-13. Working days from July 13 to July 31 = 15 working days
    res = client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 15
    assert data["present_days"] == 1.0
    assert data["attendance_pct"] == 6.67  # (1.0 / 15) * 100 = 6.666... -> 6.67

def test_employee_monthly_half_day_counting(client):
    # EMP0004 has July 7 (WFH, 3.5 hrs -> half day = 0.5) and July 8 (ABSENT = 0.0)
    res = client.get("/analytics/employees/EMP0004/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 23
    assert data["present_days"] == 0.5
    assert data["attendance_pct"] == 2.17  # (0.5 / 23) * 100 = 2.1739... -> 2.17

def test_employee_monthly_unknown_employee(client):
    res = client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
    assert res.status_code == 404

def test_department_summary(client):
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    items = data["items"]
    dept_map = {item["department"]: item for item in items}

    assert "Engineering" in dept_map
    assert "Sales" in dept_map
    assert "Support" in dept_map

    # Check Engineering: EMP0001 (Jan 5) + EMP0002 (Jul 13) -> headcount = 2
    eng = dept_map["Engineering"]
    assert eng["headcount"] == 2
    assert eng["present_days"] == 3.0
    assert eng["avg_work_hours"] == 9.04
    assert eng["late_count"] == 0
    assert eng["total_late_minutes"] == 0

    # Check Sales: EMP0003 + EMP0004 -> headcount = 2
    sales = dept_map["Sales"]
    assert sales["headcount"] == 2
    assert sales["present_days"] == 1.5
    assert sales["avg_work_hours"] == 6.29
    assert sales["late_count"] == 1
    assert sales["total_late_minutes"] == 35
    assert sales["leave_count"] == 1

def test_department_summary_filtered(client):
    res = client.get("/analytics/departments/summary?month=2026-07&department=Sales")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["department"] == "Sales"

def test_late_leaderboard(client):
    res = client.get("/analytics/leaderboard/late?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    items = data["items"]
    assert len(items) == 1
    assert items[0]["emp_code"] == "EMP0003"
    assert items[0]["rank"] == 1
    assert items[0]["total_late_minutes"] == 35

def test_department_trend(client):
    res = client.get("/analytics/departments/Sales/trend?from=2026-07-01&to=2026-07-10")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Sales"
    items = data["items"]
    assert len(items) == 10  # Exactly 10 days, July 1 to July 10 inclusive

    # July 4 and 5 are weekend days
    jul4 = next(item for item in items if item["date"] == "2026-07-04")
    assert jul4["is_working_day"] is False
    assert jul4["attendance_rate"] is None

    # July 6 is Monday with 1 present full day out of 2 headcount -> 0.5 rate
    jul6 = next(item for item in items if item["date"] == "2026-07-06")
    assert jul6["is_working_day"] is True
    assert jul6["present_count"] == 1.0
    assert jul6["attendance_rate"] == 0.5
    assert jul6["moving_avg_7d"] == 0.125

def test_department_trend_unknown_department(client):
    res = client.get("/analytics/departments/NonExistent/trend?from=2026-07-01&to=2026-07-05")
    assert res.status_code == 404

def test_admin_explain_endpoints(client):
    # 1. attendance_list
    res1 = client.get("/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-31")
    assert res1.status_code == 200
    d1 = res1.json()
    assert d1["endpoint"] == "attendance_list"
    assert d1["collection"] == "attendance_logs"
    assert "explain" in d1

    # 2. employee_monthly
    res2 = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
    assert res2.status_code == 200
    d2 = res2.json()
    assert d2["endpoint"] == "employee_monthly"
    assert d2["collection"] == "attendance_logs"

    # 3. department_summary
    res3 = client.get("/admin/explain/department_summary?month=2026-07")
    assert res3.status_code == 200
    d3 = res3.json()
    assert d3["endpoint"] == "department_summary"
    assert d3["collection"] == "employees"

    # 4. late_leaderboard
    res4 = client.get("/admin/explain/late_leaderboard?month=2026-07")
    assert res4.status_code == 200
    d4 = res4.json()
    assert d4["endpoint"] == "late_leaderboard"
    assert d4["collection"] == "attendance_logs"

    # 5. department_trend
    res5 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-05")
    assert res5.status_code == 200
    d5 = res5.json()
    assert d5["endpoint"] == "department_trend"
    assert d5["collection"] == "employees"
