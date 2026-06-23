import datetime
import os
from collections.abc import Iterator

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import Mapped, Session, declarative_base, mapped_column, relationship, sessionmaker
from sqlalchemy.types import JSON

# All 14 Catppuccin accent names valid for the Platform.color field.
CTP_ACCENTS = (
    "rosewater",
    "flamingo",
    "pink",
    "mauve",
    "red",
    "maroon",
    "peach",
    "yellow",
    "green",
    "teal",
    "sky",
    "sapphire",
    "blue",
    "lavender",
)


def _platform_heuristic_css(name: str) -> str:
    """Return a tag-platform-* CSS class from a raw platform name string.

    Used when no Platform row is linked (fallback). Outputs Catppuccin accent
    class names (tag-platform-red, etc.) to match the DB-driven path.
    Checks are ordered from most-specific to least to avoid false matches
    (e.g. "pc engine" must come before the generic "pc" check).
    """
    p = name.lower()
    if "steam" in p:
        return "tag-platform-teal"
    if "playdate" in p:
        return "tag-platform-rosewater"
    if "wonderswan" in p:
        return "tag-platform-pink"
    if "arcade" in p:
        return "tag-platform-flamingo"
    if any(kw in p for kw in ("quest", "oculus")):
        return "tag-platform-blue"
    if "neo geo" in p:
        return "tag-platform-maroon"
    if any(kw in p for kw in ("turbografx", "pc engine", "supergrafx")):
        return "tag-platform-mauve"
    if any(kw in p for kw in ("atari", "3do", "evercade")):
        return "tag-platform-peach"
    if any(kw in p for kw in ("sega", "dreamcast", "genesis", "saturn", "master system", "game gear", "mega drive", "sg-1000")):
        return "tag-platform-yellow"
    if "ps" in p or "playstation" in p:
        return "tag-platform-lavender"
    if any(
        kw in p
        for kw in (
            "switch",
            "nintendo",
            "wii",
            "nes",
            "snes",
            "famicom",
            "virtual boy",
            "game boy",
            "gameboy",
            "n64",
            "gamecube",
            "satellaview",
            "64dd",
            "dsi",
        )
    ):
        return "tag-platform-red"
    if "xbox" in p:
        return "tag-platform-green"
    if "android" in p:
        return "tag-platform-green"
    if any(kw in p for kw in ("ios", "mac", "apple", "iphone", "ipad", "browser")):
        return "tag-platform-sky"
    if any(kw in p for kw in ("amiga", "dos", "linux")):
        return "tag-platform-sapphire"
    if "pc" in p and "engine" not in p:
        return "tag-platform-sapphire"
    if "windows" in p:
        return "tag-platform-sapphire"
    return "tag-platform-other"


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
    # Steam's display name from the OpenID flow. Stored only for "Signed in
    # as <name>" UI affordance — not used in any lookup or auth decision.
    steam_persona_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # Steam profile avatar URL (medium size from GetPlayerSummaries).
    # Decorative only — shown next to persona name on the Steam configure page.
    steam_avatar_url: Mapped[str | None] = mapped_column(String, nullable=True)
    steam_last_synced_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    steam_last_dlc_synced_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    steam_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    steam_login_secure: Mapped[str | None] = mapped_column(String, nullable=True)
    # SteamGridDB API key — used to look up community cover art for manual
    # entries, PSN entries, or any DLC/game whose Steam art is missing/ugly.
    steamgriddb_api_key: Mapped[str | None] = mapped_column(String, nullable=True)
    # Twitch / IGDB credentials (client credentials flow — no user OAuth).
    # Used for IGDB game search, cover art, and platform data.
    twitch_client_id: Mapped[str | None] = mapped_column(String, nullable=True)
    twitch_client_secret: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.UTC))


class PlatformFamily(Base):
    """Groups platforms by manufacturer / ecosystem (PlayStation, Nintendo, etc.).

    color: default Catppuccin accent for all member platforms. Individual
           Platform.color still wins when set — family color is the fallback.
    igdb_id: IGDB platform_family id, nullable for custom families.
    """

    __tablename__ = "platform_families"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    igdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    color: Mapped[str | None] = mapped_column(String, nullable=True)

    platforms: Mapped[list["Platform"]] = relationship("Platform", back_populates="family")


class Platform(Base):
    """A gaming platform — either sourced from IGDB or user-created.

    name: IGDB canonical name (immutable for IGDB rows). For custom rows, the
          user-set name used for matching against GameRelease.platform strings.
    display_name: What shows in the UI. Defaults to name. User can rename freely
                  (e.g. "Nintendo Entertainment System" → "NES").
    color: A Catppuccin accent key ("red", "green", "teal", …). None = inherit
           from family, then fall back to string heuristic.
    family_id: FK to PlatformFamily — nullable, used for grouping and bulk
               color assignment.
    is_custom: True for non-IGDB entries (e.g. the "Steam" custom platform).
    """

    __tablename__ = "platforms"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    igdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    color: Mapped[str | None] = mapped_column(String, nullable=True)
    family_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("platform_families.id"), nullable=True)
    is_custom: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    family: Mapped["PlatformFamily | None"] = relationship("PlatformFamily", back_populates="platforms")
    releases: Mapped[list["GameRelease"]] = relationship("GameRelease", back_populates="platform_obj")
    aliases: Mapped[list["PlatformAlias"]] = relationship("PlatformAlias", back_populates="platform", cascade="all, delete-orphan")

    @property
    def display_title(self) -> str:
        return self.display_name or self.name

    @property
    def effective_color(self) -> str | None:
        """Own color → family color → None."""
        return self.color or (self.family.color if self.family else None)

    @property
    def css_class(self) -> str:
        c = self.effective_color
        if c:
            return f"tag-platform-{c}"
        return _platform_heuristic_css(self.name)


class PlatformAlias(Base):
    """User-managed alternate names / abbreviations for a platform.

    e.g. "PS4", "PSX", "SNES" all resolve to their canonical Platform rows
    via resolve_platform_id(). No restrictions on alias content — users can
    add whatever they recognise.
    """

    __tablename__ = "platform_aliases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    platform_id: Mapped[int] = mapped_column(Integer, ForeignKey("platforms.id", ondelete="CASCADE"), nullable=False, index=True)
    alias: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.UTC))

    platform: Mapped["Platform"] = relationship("Platform", back_populates="aliases")


def resolve_platform_id(db: Session, platform_name: str) -> int | None:
    """Look up a Platform row by name, display_name, or alias and return its id.

    Checks in order:
      1. Exact match on Platform.name (canonical IGDB / custom name)
      2. Case-insensitive match on Platform.display_name (user-renamed label)
      3. Case-insensitive match on PlatformAlias.alias (abbreviations / nicknames)

    Returns None if no match — callers leave platform_id NULL rather than
    inventing a row. Never creates new Platform rows.
    """
    if not platform_name:
        return None
    name = platform_name.strip()
    row = db.query(Platform).filter(Platform.name == name).first()
    if row:
        return row.id
    row = db.query(Platform).filter(Platform.display_name.ilike(name)).first()
    if row:
        return row.id
    alias = db.query(PlatformAlias).filter(PlatformAlias.alias.ilike(name)).first()
    return alias.platform_id if alias else None


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
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.UTC))

    @property
    def display_title(self) -> str:
        return self.display_name or self.title

    parent: Mapped["Game | None"] = relationship("Game", remote_side="Game.id", back_populates="children")
    children: Mapped[list["Game"]] = relationship("Game", back_populates="parent")
    releases: Mapped[list["GameRelease"]] = relationship("GameRelease", back_populates="game")
    canonical_artwork: Mapped[list["GameArtwork"]] = relationship(
        "GameArtwork",
        back_populates="game",
        primaryjoin="GameArtwork.game_id == Game.id",
        foreign_keys="GameArtwork.game_id",
    )
    user_artwork: Mapped[list["UserArtwork"]] = relationship(
        "UserArtwork",
        back_populates="game",
        primaryjoin="UserArtwork.game_id == Game.id",
        foreign_keys="UserArtwork.game_id",
    )


class GameRelease(Base):
    """One row per game+platform combination. Holds platform-specific metadata and the raw API payload."""

    __tablename__ = "game_releases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    game_id: Mapped[int] = mapped_column(Integer, ForeignKey("games.id"), nullable=False, index=True)
    # e.g. "Steam", "PS5", "PS4", "Switch", "iOS", "Manual"
    platform: Mapped[str] = mapped_column(String, nullable=False)
    # FK to the platforms table — nullable so old/unrecognised strings still work.
    platform_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("platforms.id"), nullable=True, index=True)
    # "steam" | "psn" | "manual"
    source: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # steam_app_id or psn_title_id — stored as string to handle both
    external_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # full API payload — mine later without needing schema changes
    raw_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # set when appdetails enrichment has run for this entry (null = never enriched)
    metadata_fetched_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.UTC))

    game: Mapped["Game"] = relationship("Game", back_populates="releases")
    platform_obj: Mapped["Platform | None"] = relationship("Platform", back_populates="releases")
    artwork: Mapped[list["GameArtwork"]] = relationship("GameArtwork", back_populates="release")
    library_entries: Mapped[list["UserLibraryEntry"]] = relationship("UserLibraryEntry", back_populates="release")

    __table_args__ = (UniqueConstraint("game_id", "platform", name="uq_release_game_platform"),)

    @property
    def display_platform(self) -> str:
        """Display label — linked Platform's display_title when available, else raw string."""
        if self.platform_obj:
            return self.platform_obj.display_title
        return self.platform

    @property
    def platform_tag_class(self) -> str:
        """CSS class for the platform badge — DB colour when linked, heuristic fallback."""
        if self.platform_obj:
            return self.platform_obj.css_class
        return _platform_heuristic_css(self.platform)


class GameArtwork(Base):
    """Visual assets for a game release or game (platform-sourced or auto-fetched).

    Scope:
      - release_id set, game_id null  → platform-specific art (Steam CDN, PSN, etc.)
      - game_id set, release_id null  → game-level canonical art for grouped view
      At least one must be set.

    artwork_type values: 'cover_v' | 'cover_h' | 'hero' | 'logo' | 'icon' | 'background'
    source values:       'steam' | 'psn' | 'sgdb' | 'igdb'

    Resolution priority (lower = more specific, wins first):
      UserArtwork (entry or game level) > GameArtwork native (steam/psn) > GameArtwork sgdb

    sort_order: within a (release/game, artwork_type, source) group, 0 = preferred candidate.
    is_valid: set False when the URL is confirmed broken during the verification pass.
    """

    __tablename__ = "game_artwork"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # One of release_id / game_id must be set.
    release_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("game_releases.id"), nullable=True)
    game_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("games.id"), nullable=True)
    artwork_type: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    # The native API field name this URL came from, e.g. 'header_image',
    # 'GAMEHUB_COVER_ART'. Useful for debugging and future re-fetching.
    source_type_raw: Mapped[str | None] = mapped_column(String, nullable=True)
    url: Mapped[str] = mapped_column(String, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Verification state — set by the background URL-check pass.
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    verified_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Within a (release/game, artwork_type, source) group: 0 = preferred, higher = alternatives.
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    release: Mapped["GameRelease | None"] = relationship("GameRelease", back_populates="artwork")
    game: Mapped["Game | None"] = relationship("Game", back_populates="canonical_artwork", foreign_keys=[game_id])

    __table_args__ = (
        # SQLite treats NULLs as distinct in unique constraints, so:
        #   release-level rows (release_id set, game_id null) → governed by first constraint
        #   game-level rows (game_id set, release_id null)    → governed by second constraint
        UniqueConstraint("release_id", "artwork_type", "source", name="uq_artwork_release_type_source"),
        UniqueConstraint("game_id", "artwork_type", "source", name="uq_artwork_game_type_source"),
    )


class UserArtwork(Base):
    """Artwork explicitly chosen by a user — overrides GameArtwork at render time.

    Scope:
      - entry_id set, game_id null → override for a specific platform entry (unique view)
      - game_id set, entry_id null → canonical for this game in grouped/cross-platform view
      One of the two must be set.

    source values: 'sgdb' (auto-fill or picker), 'user_url', 'user_upload'
    For user_upload, file_path is set; url is derived from the upload-serve route.
    """

    __tablename__ = "user_artwork"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    entry_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("user_library.id"), nullable=True, index=True)
    game_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("games.id"), nullable=True, index=True)
    artwork_type: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String, nullable=True)  # user_upload only
    mime_type: Mapped[str | None] = mapped_column(String, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.UTC))

    user: Mapped["User"] = relationship("User")
    entry: Mapped["UserLibraryEntry | None"] = relationship("UserLibraryEntry", back_populates="user_artwork")
    game: Mapped["Game | None"] = relationship("Game", back_populates="user_artwork", foreign_keys=[game_id])

    __table_args__ = (
        UniqueConstraint("user_id", "entry_id", "artwork_type", name="uq_user_artwork_entry_type"),
        UniqueConstraint("user_id", "game_id", "artwork_type", name="uq_user_artwork_game_type"),
    )


class UserLibraryEntry(Base):
    """User's ownership of a specific game release. One row per user+release combo."""

    __tablename__ = "user_library"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    release_id: Mapped[int] = mapped_column(Integer, ForeignKey("game_releases.id"), nullable=False, index=True)
    playtime_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_played_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # True = entry hidden from the default library view (soundtracks, artbooks, etc.)
    is_hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    # True when the user explicitly toggled is_hidden — the auto-hide heuristic
    # must not touch this entry. Same pattern as the *_user_set flags on Game.
    is_hidden_user_set: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # "steam_import" | "psn_import" | "manual"
    import_source: Mapped[str] = mapped_column(String, nullable=False, default="manual", index=True)
    # if access comes from owning a parent collection, points to that collection's library entry
    parent_entry_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("user_library.id"), nullable=True)
    imported_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.UTC))
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.UTC), onupdate=lambda: datetime.datetime.now(datetime.UTC)
    )

    user: Mapped["User"] = relationship("User")
    release: Mapped["GameRelease"] = relationship("GameRelease", back_populates="library_entries")
    parent_entry: Mapped["UserLibraryEntry | None"] = relationship(
        "UserLibraryEntry", remote_side="UserLibraryEntry.id", back_populates="child_entries"
    )
    child_entries: Mapped[list["UserLibraryEntry"]] = relationship("UserLibraryEntry", back_populates="parent_entry")
    achievements: Mapped[list["UserAchievement"]] = relationship("UserAchievement", back_populates="library_entry")
    completions: Mapped[list["Completion"]] = relationship("Completion", back_populates="library_entry")
    user_artwork: Mapped[list["UserArtwork"]] = relationship("UserArtwork", back_populates="entry")

    __table_args__ = (UniqueConstraint("user_id", "release_id", name="uq_library_user_release"),)

    @property
    def title(self) -> str:
        return self.release.game.display_name or self.release.game.title


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


class SyncMatchCandidate(Base):
    """Potential duplicate match between a manual library entry and a synced platform game.

    Created by the match-detection pass (run automatically after sync, or on demand).
    Reviewed by the user on /library/match-review.

    status values:
      pending       – awaiting review
      merged        – user approved the merge; manual entry absorbed the synced data
      dismissed – user chose to keep entries distinct (filtered from default view,
                      re-surfaceable via "Show previously skipped" toggle)
    """

    __tablename__ = "sync_match_candidates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # The manual UserLibraryEntry that may be a duplicate
    manual_entry_id: Mapped[int] = mapped_column(Integer, ForeignKey("user_library.id", ondelete="CASCADE"), nullable=False, index=True)
    # The platform source and external ID of the synced game
    platform_source: Mapped[str] = mapped_column(String, nullable=False)  # "steam" | "psn"
    external_id: Mapped[str] = mapped_column(String, nullable=False)  # appid or psn title id
    synced_title: Mapped[str] = mapped_column(String, nullable=False)  # title from the sync source
    # 0.0–1.0 confidence score
    match_score: Mapped[float] = mapped_column(Float, nullable=False)
    # pending | merged | dismissed
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending", index=True)
    # optional user note when keeping separate
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.UTC))
    reviewed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    manual_entry: Mapped["UserLibraryEntry"] = relationship("UserLibraryEntry", foreign_keys=[manual_entry_id])

    __table_args__ = (UniqueConstraint("manual_entry_id", "platform_source", "external_id", name="uq_match_candidate"),)


class Completion(Base):
    __tablename__ = "completions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    library_entry_id: Mapped[int] = mapped_column(Integer, ForeignKey("user_library.id"), nullable=False)
    completed_at: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    # stored as string to handle "1", "1+", "2", "3+" etc.
    playthroughs: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.UTC))

    user: Mapped["User"] = relationship("User")
    library_entry: Mapped["UserLibraryEntry"] = relationship("UserLibraryEntry", back_populates="completions")


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
