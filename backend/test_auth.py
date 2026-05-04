import os

# Use a separate file-backed SQLite for auth tests; set before importing the app
DB_PATH = os.path.join(os.path.dirname(__file__), "test_auth.db")
if os.path.exists(DB_PATH):
    try:
        os.remove(DB_PATH)
    except OSError:
        pass
os.environ.setdefault("DATABASE_URL", f"sqlite:///{DB_PATH}")

from fastapi.testclient import TestClient
from backend.main import app
import backend.models as models

# Ensure tables are created for the test DB before starting the test client
models.init_db()

client = TestClient(app)


def test_signup_signin_me_flow():
    r = client.post("/signup", json={"username": "testuser", "password": "pass123"})
    assert r.status_code == 200
    data = r.json()
    assert "id" in data

    r2 = client.post("/signin", json={"username": "testuser", "password": "pass123"})
    assert r2.status_code == 200
    token = r2.json().get("token")
    assert token

    r3 = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert r3.status_code == 200
    assert r3.json()["id"] == data["id"]


def test_signup_existing_username():
    r = client.post("/signup", json={"username": "dup", "password": "p"})
    assert r.status_code == 200
    r2 = client.post("/signup", json={"username": "dup", "password": "p2"})
    assert r2.status_code == 400


def test_signin_invalid_password():
    r = client.post("/signup", json={"username": "user2", "password": "pw"})
    assert r.status_code == 200
    r2 = client.post("/signin", json={"username": "user2", "password": "wrong"})
    assert r2.status_code == 401
