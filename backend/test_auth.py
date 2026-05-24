def _signup(client, username, password):
    return client.post(
        "/signup",
        data={
            "username": username,
            "password": password,
            "password_confirm": password,
        },
        follow_redirects=False,
    )


def test_signup_signin_me_flow(client):
    r = _signup(client, "testuser", "pass123")
    assert r.status_code == 302

    r2 = client.post("/signin", json={"username": "testuser", "password": "pass123"})
    assert r2.status_code == 200
    token = r2.json().get("token")
    assert token

    r3 = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert r3.status_code == 200
    assert r3.json()["name"] == "testuser"


def test_signup_existing_username(client):
    _signup(client, "dup", "p")
    r2 = _signup(client, "dup", "p2")
    assert r2.status_code == 422
    assert b"already taken" in r2.content


def test_signin_invalid_password(client):
    _signup(client, "user2", "pw")
    r2 = client.post("/signin", json={"username": "user2", "password": "wrong"})
    assert r2.status_code == 401


def test_me_requires_auth(client):
    r = client.get("/me")
    assert r.status_code in (401, 403)


def test_me_rejects_bad_token(client):
    r = client.get("/me", headers={"Authorization": "Bearer notarealtoken"})
    assert r.status_code == 401
