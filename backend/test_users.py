def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_get_user_by_id(client):
    r = client.post("/signup", json={"username": "alice", "password": "pw"})
    assert r.status_code == 200
    uid = r.json()["id"]

    r2 = client.get(f"/users/{uid}")
    assert r2.status_code == 200
    assert r2.json()["id"] == uid


def test_get_user_not_found(client):
    r = client.get("/users/99999")
    assert r.status_code == 404
