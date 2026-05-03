import os

# Use a file-backed SQLite for tests; set before importing the app
DB_PATH = os.path.join(os.path.dirname(__file__), "test.db")
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


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_create_and_get_user():
    # create user
    r = client.post("/users", json={"name": "bob"})
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "bob"
    assert "id" in data

    uid = data["id"]
    # fetch user
    r2 = client.get(f"/users/{uid}")
    assert r2.status_code == 200
    assert r2.json()["name"] == "bob"


def test_update_and_delete_user():
    # create user
    r = client.post("/users", json={"name": "charlie"})
    assert r.status_code == 200
    data = r.json()
    uid = data["id"]

    # update user
    r2 = client.patch(f"/users/{uid}", json={"name": "charlie-updated"})
    assert r2.status_code == 200
    assert r2.json()["name"] == "charlie-updated"

    # delete user
    r3 = client.delete(f"/users/{uid}")
    assert r3.status_code == 204

    # fetch deleted -> 404
    r4 = client.get(f"/users/{uid}")
    assert r4.status_code == 404


def test_list_users():
    # ensure there is at least one user
    r = client.post("/users", json={"name": "dave"})
    assert r.status_code == 200
    r2 = client.get("/users")
    assert r2.status_code == 200
    data = r2.json()
    assert isinstance(data, list)
    assert any(u.get("name") == "dave" for u in data)
