def test_signup_signin_me_flow(client):
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


def test_signup_existing_username(client):
    r = client.post("/signup", json={"username": "dup", "password": "p"})
    assert r.status_code == 200
    r2 = client.post("/signup", json={"username": "dup", "password": "p2"})
    assert r2.status_code == 400


def test_signin_invalid_password(client):
    r = client.post("/signup", json={"username": "user2", "password": "pw"})
    assert r.status_code == 200
    r2 = client.post("/signin", json={"username": "user2", "password": "wrong"})
    assert r2.status_code == 401


def test_me_requires_auth(client):
    r = client.get("/me")
    assert r.status_code in (401, 403)


def test_me_rejects_bad_token(client):
    r = client.get("/me", headers={"Authorization": "Bearer notarealtoken"})
    assert r.status_code == 401
