import os
import pathlib
from bson import json_util
import pytest
from fastapi.testclient import TestClient

from app.main import app, db, init_indexes

@pytest.fixture(scope="session", autouse=True)
def setup_indexes():
    init_indexes()

@pytest.fixture(autouse=True)
def seed_sample_data():
    """Resets database to the sample dataset before each test."""
    here = pathlib.Path(__file__).parent.parent / "sample_data"
    opts = json_util.JSONOptions(tz_aware=True)
    for name in ("employees", "attendance_logs"):
        docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
        db[name].delete_many({})
        db[name].insert_many(docs)
    init_indexes()

@pytest.fixture
def client():
    return TestClient(app)
