import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ["TESTING"] = "1"

from backend import models
from backend.main import app
from backend.models import get_db

# ── The suite must not touch the network (#201) ─────────────────────────────
#
# Every PSN review action calls kick_psn_enrichment, which spawns a task that
# fetches PSN store metadata and SteamGridDB artwork for real. TestClient drains
# the event loop on exit, so each of those tests blocked in teardown until the
# calls returned. Measured 2026-08-19: the suite went from ~28s to ~197s with
# only 25s of CPU -- 13% utilisation, the rest waiting on the internet. The same
# test in isolation was 0.21s.
#
# Slow is the least of it. A test run could reach live services with whatever
# credentials the environment happens to hold, and results depended on network
# conditions rather than on the code.


@pytest.fixture(autouse=True)
def no_outbound_http(monkeypatch, request):
    """Fail any real HTTP request, loudly, naming the URL.

    Blocks httpx's REAL transports only. TestClient drives the app through
    httpx as well, over its own ASGI transport, so that keeps working -- it is
    genuine outbound traffic that stops here.

    A test that wants to exercise a fetch should patch the function it calls
    (`patch("backend.steam.httpx.get", ...)`, as several already do). One that
    genuinely needs the transport can ask for the `allow_http` marker.
    """
    if request.node.get_closest_marker("allow_http"):
        return

    import httpx

    def _blocked(self, request_, *args, **kwargs):
        raise RuntimeError(
            f"Test tried to reach {request_.url} over the network. Patch the call instead, or mark the test with @pytest.mark.allow_http."
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked, raising=False)

    async def _blocked_async(self, request_, *args, **kwargs):
        return _blocked(self, request_, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked_async, raising=False)


@pytest.fixture(autouse=True)
def no_background_enrichment(monkeypatch, request):
    """Don't spawn enrichment tasks that can only fail now that HTTP is blocked.

    Opt back in with the `enrichment` marker; a test asserting that enrichment
    FIRES should patch the kicker with a spy, which several already do and which
    still works because this only replaces the default.
    """
    if request.node.get_closest_marker("enrichment"):
        return
    from backend import integrations

    monkeypatch.setattr(integrations, "kick_psn_enrichment", lambda user: [], raising=False)


@pytest.fixture()
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(conn, _record):
        conn.execute("PRAGMA foreign_keys=ON")

    models.Base.metadata.create_all(bind=engine)
    yield engine
    models.Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    """Direct database session — use this when tests need to set up or inspect DB state."""
    Session = sessionmaker(bind=db_engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db_engine):
    """Test HTTP client wired to the same in-memory DB as db_session."""
    TestSession = sessionmaker(bind=db_engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, follow_redirects=True) as c:
        yield c
    app.dependency_overrides.clear()
