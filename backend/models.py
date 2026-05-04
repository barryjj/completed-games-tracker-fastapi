from sqlalchemy import create_engine, Integer, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, Mapped, mapped_column
import datetime
import os

DB_URL = os.getenv("DATABASE_URL", "sqlite:///backend/app.db")
engine = create_engine(DB_URL, connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # Authentication fields
    username: Mapped[str] = mapped_column(String, unique=True, nullable=True, index=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=True)
    api_token: Mapped[str] = mapped_column(String, unique=True, nullable=True, index=True)
    # Use timezone-aware UTC datetime to avoid deprecation warnings
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc))


def init_db():
    Base.metadata.create_all(bind=engine)
