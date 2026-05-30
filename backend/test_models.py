import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        models.Base.metadata.drop_all(bind=engine)
        engine.dispose()


# --- helpers ---


def make_user(db, username="player1"):
    u = models.User(name=username, username=username, password_hash="x", api_token=f"tok-{username}")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def make_game(db, title="Elden Ring", is_dlc=False, is_collection=False, parent=None):
    g = models.Game(title=title, is_dlc=is_dlc, is_collection=is_collection, parent_id=parent.id if parent else None)
    db.add(g)
    db.commit()
    db.refresh(g)
    return g


def make_release(db, game, platform="Steam", source="steam", external_id=None):
    r = models.GameRelease(game_id=game.id, platform=platform, source=source, external_id=external_id)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def make_library_entry(db, user, release, import_source="steam_import"):
    e = models.UserLibraryEntry(user_id=user.id, release_id=release.id, import_source=import_source)
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


# --- Game ---


def test_create_game(db):
    g = make_game(db)
    assert g.id is not None
    assert g.title == "Elden Ring"
    assert g.is_dlc is False
    assert g.is_collection is False
    assert g.parent_id is None


def test_dlc_parent_relationship(db):
    base = make_game(db, "Elden Ring")
    dlc = make_game(db, "Shadow of the Erdtree", is_dlc=True, parent=base)
    db.refresh(base)

    assert dlc.is_dlc is True
    assert dlc.parent_id == base.id
    assert dlc.parent.title == "Elden Ring"
    assert any(c.id == dlc.id for c in base.children)


def test_collection_parent_relationship(db):
    collection = make_game(db, "Contra Anniversary Collection", is_collection=True)
    super_c = make_game(db, "Super C", parent=collection)
    op_c = make_game(db, "Operation C", parent=collection)
    db.refresh(collection)

    assert collection.is_collection is True
    assert len(collection.children) == 2
    assert {c.title for c in collection.children} == {"Super C", "Operation C"}


# --- GameRelease ---


def test_create_game_release(db):
    g = make_game(db)
    r = make_release(db, g, platform="Steam", source="steam", external_id="1245620")

    assert r.id is not None
    assert r.game_id == g.id
    assert r.external_id == "1245620"
    assert r.game.title == "Elden Ring"


def test_release_unique_constraint(db):
    g = make_game(db)
    make_release(db, g, platform="Steam")
    with pytest.raises(IntegrityError):
        make_release(db, g, platform="Steam")


def test_same_game_multiple_platforms(db):
    g = make_game(db)
    steam = make_release(db, g, platform="Steam", source="steam")
    ps5 = make_release(db, g, platform="PS5", source="psn")

    db.refresh(g)
    assert len(g.releases) == 2
    assert {r.platform for r in g.releases} == {"Steam", "PS5"}


def test_release_raw_data(db):
    g = make_game(db)
    r = models.GameRelease(game_id=g.id, platform="Steam", source="steam", raw_data={"playtime": 120, "achievements": 50})
    db.add(r)
    db.commit()
    db.refresh(r)

    assert r.raw_data["playtime"] == 120
    assert r.raw_data["achievements"] == 50


# --- GameArtwork ---


def test_create_artwork(db):
    g = make_game(db)
    r = make_release(db, g)
    art = models.GameArtwork(
        release_id=r.id, artwork_type="cover_v", source="steam", url="https://example.com/cover.jpg", width=600, height=900
    )
    db.add(art)
    db.commit()
    db.refresh(art)

    assert art.id is not None
    assert art.release.game.title == "Elden Ring"
    assert art.width == 600


def test_artwork_multiple_types_per_release(db):
    g = make_game(db)
    r = make_release(db, g)
    for artwork_type, url in [("cover_v", "cover.jpg"), ("cover_h", "header.jpg"), ("hero", "hero.jpg")]:
        db.add(models.GameArtwork(release_id=r.id, artwork_type=artwork_type, source="steam", url=url))
    db.commit()
    db.refresh(r)

    assert len(r.artwork) == 3
    assert {a.artwork_type for a in r.artwork} == {"cover_v", "cover_h", "hero"}


def test_artwork_same_type_different_sources(db):
    g = make_game(db)
    r = make_release(db, g)
    db.add(models.GameArtwork(release_id=r.id, artwork_type="cover_v", source="steam", url="steam_cover.jpg"))
    db.add(models.GameArtwork(release_id=r.id, artwork_type="cover_v", source="sgdb", url="sgdb_cover.jpg"))
    db.commit()
    db.refresh(r)

    covers = [a for a in r.artwork if a.artwork_type == "cover_v"]
    assert len(covers) == 2
    assert {a.source for a in covers} == {"steam", "sgdb"}


def test_artwork_unique_constraint(db):
    g = make_game(db)
    r = make_release(db, g)
    db.add(models.GameArtwork(release_id=r.id, artwork_type="cover_v", source="steam", url="cover.jpg"))
    db.commit()
    with pytest.raises(IntegrityError):
        db.add(models.GameArtwork(release_id=r.id, artwork_type="cover_v", source="steam", url="other.jpg"))
        db.commit()


# --- UserLibraryEntry ---


def test_create_library_entry(db):
    user = make_user(db)
    game = make_game(db)
    release = make_release(db, game)
    entry = make_library_entry(db, user, release)

    assert entry.id is not None
    assert entry.release.game.title == "Elden Ring"


def test_library_entry_unique_constraint(db):
    user = make_user(db)
    game = make_game(db)
    release = make_release(db, game)
    make_library_entry(db, user, release)
    with pytest.raises(IntegrityError):
        make_library_entry(db, user, release)


def test_collection_item_parent_entry(db):
    user = make_user(db)
    collection_game = make_game(db, "Contra Anniversary Collection", is_collection=True)
    super_c_game = make_game(db, "Super C", parent=collection_game)

    collection_release = make_release(db, collection_game, external_id="1018060")
    super_c_release = make_release(db, super_c_game, external_id="1018060")

    collection_entry = make_library_entry(db, user, collection_release)
    super_c_entry = models.UserLibraryEntry(
        user_id=user.id,
        release_id=super_c_release.id,
        import_source="manual",
        parent_entry_id=collection_entry.id,
    )
    db.add(super_c_entry)
    db.commit()
    db.refresh(super_c_entry)
    db.refresh(collection_entry)

    assert super_c_entry.parent_entry.id == collection_entry.id
    assert any(c.id == super_c_entry.id for c in collection_entry.child_entries)


# --- UserAchievement ---


def test_create_achievement(db):
    user = make_user(db)
    game = make_game(db)
    release = make_release(db, game, platform="Steam")
    entry = make_library_entry(db, user, release)

    ach = models.UserAchievement(
        library_entry_id=entry.id,
        external_id="ACH_BEAT_GAME",
        name="Elden Lord",
        description="Reached the Elden Throne",
        unlocked=True,
        unlocked_at=datetime.datetime(2026, 1, 15, tzinfo=datetime.UTC),
    )
    db.add(ach)
    db.commit()
    db.refresh(ach)

    assert ach.id is not None
    assert ach.unlocked is True
    assert ach.library_entry.release.platform == "Steam"


def test_psn_trophy_type_in_extra(db):
    user = make_user(db)
    game = make_game(db)
    release = make_release(db, game, platform="PS5", source="psn")
    entry = make_library_entry(db, user, release, import_source="psn_import")

    platinum = models.UserAchievement(
        library_entry_id=entry.id,
        external_id="trophy_0",
        name="Elden Lord",
        unlocked=True,
        extra={"trophy_type": "platinum", "rarity": 2.4},
    )
    db.add(platinum)
    db.commit()
    db.refresh(platinum)

    assert platinum.extra["trophy_type"] == "platinum"
    assert platinum.extra["rarity"] == 2.4


def test_achievements_platform_separation(db):
    user = make_user(db)
    game = make_game(db)
    steam_release = make_release(db, game, platform="Steam", source="steam")
    ps5_release = make_release(db, game, platform="PS5", source="psn")
    steam_entry = make_library_entry(db, user, steam_release, import_source="steam_import")
    ps5_entry = make_library_entry(db, user, ps5_release, import_source="psn_import")

    db.add(models.UserAchievement(library_entry_id=steam_entry.id, external_id="ACH_1", name="Steam Achievement", unlocked=True))
    db.add(
        models.UserAchievement(
            library_entry_id=ps5_entry.id, external_id="trophy_1", name="PS5 Trophy", unlocked=False, extra={"trophy_type": "gold"}
        )
    )
    db.commit()
    db.refresh(steam_entry)
    db.refresh(ps5_entry)

    assert steam_entry.achievements[0].name == "Steam Achievement"
    assert ps5_entry.achievements[0].extra["trophy_type"] == "gold"


def test_achievement_unique_per_entry(db):
    user = make_user(db)
    game = make_game(db)
    release = make_release(db, game)
    entry = make_library_entry(db, user, release)

    db.add(models.UserAchievement(library_entry_id=entry.id, external_id="ACH_1", name="First", unlocked=False))
    db.commit()
    with pytest.raises(IntegrityError):
        db.add(models.UserAchievement(library_entry_id=entry.id, external_id="ACH_1", name="Duplicate", unlocked=False))
        db.commit()


# --- Completion ---


def test_create_completion(db):
    user = make_user(db)
    game = make_game(db)
    release = make_release(db, game, platform="PS5", source="psn")
    entry = make_library_entry(db, user, release, import_source="psn_import")

    c = models.Completion(
        user_id=user.id,
        library_entry_id=entry.id,
        completed_at=datetime.date(2026, 1, 15),
        playthroughs="1",
        notes="Platinum",
    )
    db.add(c)
    db.commit()
    db.refresh(c)

    assert c.completed_at == datetime.date(2026, 1, 15)
    assert c.library_entry.release.platform == "PS5"
    assert c.library_entry.release.game.title == "Elden Ring"


def test_multiple_completions_same_game(db):
    user = make_user(db)
    game = make_game(db)
    ps5_release = make_release(db, game, platform="PS5", source="psn")
    steam_release = make_release(db, game, platform="Steam", source="steam")
    ps5_entry = make_library_entry(db, user, ps5_release, import_source="psn_import")
    steam_entry = make_library_entry(db, user, steam_release, import_source="steam_import")

    db.add(models.Completion(user_id=user.id, library_entry_id=ps5_entry.id, completed_at=datetime.date(2026, 1, 1), playthroughs="1"))
    db.add(models.Completion(user_id=user.id, library_entry_id=steam_entry.id, completed_at=datetime.date(2026, 6, 1), playthroughs="1"))
    db.commit()

    completions = db.query(models.Completion).filter_by(user_id=user.id).all()
    assert {c.library_entry.release.platform for c in completions} == {"PS5", "Steam"}


def test_playthroughs_string_values(db):
    user = make_user(db)
    game = make_game(db)
    release = make_release(db, game)
    entry = make_library_entry(db, user, release)

    for value in ("1", "1+", "2", "3+"):
        db.add(models.Completion(user_id=user.id, library_entry_id=entry.id, completed_at=datetime.date(2026, 1, 1), playthroughs=value))
    db.commit()

    assert {c.playthroughs for c in db.query(models.Completion).filter_by(user_id=user.id).all()} == {"1", "1+", "2", "3+"}
