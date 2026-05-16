import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from backend import models
from backend.main import app
from backend.models import get_db


@pytest.fixture()
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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
