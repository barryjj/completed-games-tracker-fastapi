from sqlalchemy import create_engine, Integer, String, DateTime, Date, Text, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.orm import declarative_base, sessionmaker, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON
from typing import Iterator
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
    username: Mapped[str] = mapped_column(String, unique=True, nullable=True, index=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=True)
    api_token: Mapped[str] = mapped_column(String, unique=True, nullable=True, index=True)
    steam_id64: Mapped[str | None] = mapped_column(String, nullable=True)
    steam_api_key: Mapped[str | None] = mapped_column(String, nullable=True)
    steam_last_synced_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    steam_last_dlc_synced_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    steam_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    steam_login_secure: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc))


class Game(Base):
    __tablename__ = "games"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # User-facing title override — strips junk suffixes (™, ®, ©) from imported titles.
    # If set, use this everywhere; fall back to title when None.
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # True if this entry is a DLC (add-on for another game)
    is_dlc: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    # True if this entry is itself a collection (e.g. Anniversary Collection)
    is_collection: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    # DLC -> base game, or standalone game -> collection it belongs to
    parent_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("games.id"), nullable=True, index=True)
    igdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    # User-override flags. When True, the corresponding field has been explicitly
    # set by the user and no heuristic (sync's _clean_title, enrichment worker,
    # backfills) is allowed to touch it. The user's edit is law.
    display_name_user_set: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_dlc_user_set: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_collection_user_set: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    parent_id_user_set: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc))

    @property
    def display_title(self) -> str:
        return self.display_name or self.title

    parent: Mapped["Game | None"] = relationship("Game", remote_side="Game.id", back_populates="children")
    children: Mapped[list["Game"]] = relationship("Game", back_populates="parent")
    releases: Mapped[list["GameRelease"]] = relationship("GameRelease", back_populates="game")


class GameRelease(Base):
    """One row per game+platform combination. Holds platform-specific metadata and the raw API payload."""
    __tablename__ = "game_releases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    game_id: Mapped[int] = mapped_column(Integer, ForeignKey("games.id"), nullable=False, index=True)
    # e.g. "Steam", "PS5", "PS4", "Switch", "iOS", "Manual"
    platform: Mapped[str] = mapped_column(String, nullable=False)
    # "steam" | "psn" | "manual"
    source: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # steam_app_id or psn_title_id — stored as string to handle both
    external_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # full API payload — mine later without needing schema changes
    raw_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # set when appdetails enrichment has run for this entry (null = never enriched)
    metadata_fetched_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc))

    game: Mapped["Game"] = relationship("Game", back_populates="releases")
    artwork: Mapped[list["GameArtwork"]] = relationship("GameArtwork", back_populates="release")
    library_entries: Mapped[list["UserLibraryEntry"]] = relationship("UserLibraryEntry", back_populates="release")

    __table_args__ = (UniqueConstraint("game_id", "platform", name="uq_release_game_platform"),)


class GameArtwork(Base):
    """All visual assets for a game release. Multiple rows per release, one per type+source combo."""
    __tablename__ = "game_artwork"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    release_id: Mapped[int] = mapped_column(Integer, ForeignKey("game_releases.id"), nullable=False)
    # "cover" | "header" | "hero" | "logo" | "background" | "icon"
    artwork_type: Mapped[str] = mapped_column(String, nullable=False)
    # "steam" | "psn" | "steamgriddb" | "manual"
    source: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)

    release: Mapped["GameRelease"] = relationship("GameRelease", back_populates="artwork")

    __table_args__ = (UniqueConstraint("release_id", "artwork_type", "source", name="uq_artwork_release_type_source"),)


class UserLibraryEntry(Base):
    """User's ownership of a specific game release. One row per user+release combo."""
    __tablename__ = "user_library"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    release_id: Mapped[int] = mapped_column(Integer, ForeignKey("game_releases.id"), nullable=False, index=True)
    playtime_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_played_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # single URL override for when the user wants different art than what was scraped
    cover_url_override: Mapped[str | None] = mapped_column(String, nullable=True)
    # True = entry hidden from the default library view (soundtracks, artbooks, etc.)
    is_hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    # True when the user explicitly toggled is_hidden — the auto-hide heuristic
    # must not touch this entry. Same pattern as the *_user_set flags on Game.
    is_hidden_user_set: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # "steam_import" | "psn_import" | "manual"
    import_source: Mapped[str] = mapped_column(String, nullable=False, default="manual", index=True)
    # if access comes from owning a parent collection, points to that collection's library entry
    parent_entry_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("user_library.id"), nullable=True)
    imported_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))

    user: Mapped["User"] = relationship("User")
    release: Mapped["GameRelease"] = relationship("GameRelease", back_populates="library_entries")
    parent_entry: Mapped["UserLibraryEntry | None"] = relationship("UserLibraryEntry", remote_side="UserLibraryEntry.id", back_populates="child_entries")
    child_entries: Mapped[list["UserLibraryEntry"]] = relationship("UserLibraryEntry", back_populates="parent_entry")
    achievements: Mapped[list["UserAchievement"]] = relationship("UserAchievement", back_populates="library_entry")
    completions: Mapped[list["Completion"]] = relationship("Completion", back_populates="library_entry")

    __table_args__ = (UniqueConstraint("user_id", "release_id", name="uq_library_user_release"),)


class UserAchievement(Base):
    """Trophy/achievement progress per user per game release. Platform-agnostic row — the
    library_entry_id already encodes which platform this belongs to."""
    __tablename__ = "user_achievements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    library_entry_id: Mapped[int] = mapped_column(Integer, ForeignKey("user_library.id"), nullable=False)
    # platform-specific identifier: Steam achievement API name or PSN trophy ID
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon_url: Mapped[str | None] = mapped_column(String, nullable=True)
    unlocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    unlocked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # platform-specific extras: trophy_type (bronze/silver/gold/platinum), rarity %, hidden flag, etc.
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    library_entry: Mapped["UserLibraryEntry"] = relationship("UserLibraryEntry", back_populates="achievements")

    __table_args__ = (UniqueConstraint("library_entry_id", "external_id", name="uq_achievement_entry_external"),)


class Completion(Base):
    __tablename__ = "completions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    library_entry_id: Mapped[int] = mapped_column(Integer, ForeignKey("user_library.id"), nullable=False)
    completed_at: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    # stored as string to handle "1", "1+", "2", "3+" etc.
    playthroughs: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc))

    user: Mapped["User"] = relationship("User")
    library_entry: Mapped["UserLibraryEntry"] = relationship("UserLibraryEntry", back_populates="completions")


def get_db() -> Iterator["Session"]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
