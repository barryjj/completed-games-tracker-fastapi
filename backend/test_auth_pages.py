from backend import models


def _signup_and_login(client, username="testuser", password="testpass"):
    client.post("/signup", data={"username": username, "password": password, "password_confirm": password})
    r = client.post("/login", data={"username": username, "password": password}, follow_redirects=False)
    client.cookies.set("session", r.cookies["session"])


# --- signup page ---


def test_signup_page_loads(client):
    r = client.get("/signup", follow_redirects=False)
    assert r.status_code == 200
    assert b"Create Account" in r.content


def test_signup_creates_account_and_redirects(client):
    r = client.post(
        "/signup",
        data={
            "username": "newuser",
            "password": "secret123",
            "password_confirm": "secret123",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/library"
    assert "session" in r.cookies


def test_signup_password_mismatch(client):
    r = client.post(
        "/signup",
        data={
            "username": "newuser",
            "password": "abc",
            "password_confirm": "xyz",
        },
    )
    assert r.status_code == 422
    assert b"do not match" in r.content


def test_signup_duplicate_username(client):
    data = {"username": "dupeuser", "password": "pw", "password_confirm": "pw"}
    client.post("/signup", data=data)
    r = client.post("/signup", data=data)
    assert r.status_code == 422
    assert b"already taken" in r.content


def test_login_page_links_to_signup(client):
    r = client.get("/login")
    assert b"/signup" in r.content


# --- account page ---


def test_account_page_loads(client):
    _signup_and_login(client)
    r = client.get("/account")
    assert r.status_code == 200
    assert b"tab-profile" in r.content


def test_account_requires_auth(client):
    r = client.get("/account", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["location"]


def test_update_display_name(client):
    _signup_and_login(client)
    r = client.post("/account/display-name", data={"name": "Barry"})
    assert r.status_code == 200
    assert b"updated" in r.content


def test_update_username_success(client):
    _signup_and_login(client, username="oldname", password="pw")
    r = client.post(
        "/account/username",
        data={
            "new_username": "newname",
            "current_password": "pw",
        },
    )
    assert r.status_code == 200
    assert b"updated" in r.content


def test_update_username_wrong_password(client):
    _signup_and_login(client)
    r = client.post(
        "/account/username",
        data={
            "new_username": "whatever",
            "current_password": "wrongpass",
        },
    )
    assert r.status_code == 422
    assert b"incorrect" in r.content


def test_update_username_taken(client, db_session):
    _signup_and_login(client, username="user1", password="pw")
    # create a second user directly
    from backend.users import signup_user

    signup_user(db_session, "user2", "pw")
    r = client.post(
        "/account/username",
        data={
            "new_username": "user2",
            "current_password": "pw",
        },
    )
    assert r.status_code == 422
    assert b"already taken" in r.content


def test_update_password_success(client):
    _signup_and_login(client, password="oldpass")
    r = client.post(
        "/account/password",
        data={
            "current_password": "oldpass",
            "new_password": "newpass",
            "new_password_confirm": "newpass",
        },
    )
    assert r.status_code == 200
    assert b"updated" in r.content


def test_update_password_wrong_current(client):
    _signup_and_login(client, password="correct")
    r = client.post(
        "/account/password",
        data={
            "current_password": "wrong",
            "new_password": "newpass",
            "new_password_confirm": "newpass",
        },
    )
    assert r.status_code == 422
    assert b"incorrect" in r.content


def test_update_password_mismatch(client):
    _signup_and_login(client, password="correct")
    r = client.post(
        "/account/password",
        data={
            "current_password": "correct",
            "new_password": "aaa",
            "new_password_confirm": "bbb",
        },
    )
    assert r.status_code == 422
    assert b"do not match" in r.content


def test_new_password_works_for_login(client):
    _signup_and_login(client, username="changer", password="oldpass")
    client.post(
        "/account/password",
        data={
            "current_password": "oldpass",
            "new_password": "newpass",
            "new_password_confirm": "newpass",
        },
    )
    client.get("/logout")
    r = client.post("/login", data={"username": "changer", "password": "newpass"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/library"


# --- delete account ---


def test_delete_account_wrong_username(client):
    _signup_and_login(client, username="keeper", password="pw")
    r = client.post("/account/delete", data={"confirm_username": "wrong"})
    assert r.status_code == 422
    assert b"did not match" in r.content


def test_delete_account_success(client, db_session):
    _signup_and_login(client, username="goner", password="pw")
    r = client.post("/account/delete", data={"confirm_username": "goner"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    assert db_session.query(models.User).filter_by(username="goner").first() is None


def test_deleted_account_cannot_login(client):
    _signup_and_login(client, username="ghost", password="pw")
    client.post("/account/delete", data={"confirm_username": "ghost"})
    r = client.post("/login", data={"username": "ghost", "password": "pw"}, follow_redirects=False)
    assert r.status_code == 401
