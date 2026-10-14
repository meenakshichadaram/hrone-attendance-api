from unittest.mock import patch
from pymongo.errors import ConnectionFailure

def test_health_success(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

def test_health_failure(client):
    with patch("app.main.check_db_ping", side_effect=ConnectionFailure("Mongo down")):
        res = client.get("/health")
        assert res.status_code == 503
        assert "Database unavailable" in res.json()["detail"]

def test_create_employee_success(client):
    payload = {
        "emp_code": "EMP0099",
        "name": "Ananya Roy",
        "email": "ananya@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0099"
    assert data["name"] == "Ananya Roy"
    assert isinstance(data["created_at"], int)
    assert data["created_at"] > 100000000000
    assert "_id" not in data

def test_create_employee_duplicate_emp_code(client):
    payload = {
        "emp_code": "EMP0001",
        "name": "Duplicate Asha",
        "email": "asha.dup@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 409
    assert "already exists" in res.json()["detail"]

def test_create_employee_equal_shift_times(client):
    payload = {
        "emp_code": "EMP0098",
        "name": "Invalid Shift",
        "email": "invalid@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "09:30",
        "joined_on": "2026-07-01",
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 422

def test_list_employees_pagination_and_sorting(client):
    res = client.get("/employees?page=1&page_size=2")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 6
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert len(data["items"]) == 2
    assert data["items"][0]["emp_code"] == "EMP0001"
    assert data["items"][1]["emp_code"] == "EMP0002"

def test_list_employees_department_filter(client):
    res = client.get("/employees?department=Sales")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 2
    emp_codes = [e["emp_code"] for e in data["items"]]
    assert sorted(emp_codes) == ["EMP0003", "EMP0004"]
