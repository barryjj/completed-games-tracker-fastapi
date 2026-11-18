"""The detail pane's view of one achievement set (#210)."""

import datetime

from backend import achievements, models


def _user(db) -> models.User:
    u = models.User(name="t", username="t", password_hash="x", api_token="tok")
    db.add(u)
    db.commit()
    return u


def _define(db, source, set_id, rows):
    """rows: (name, tier, group_id, rarity, hidden). Returns the definitions."""
    out = []
    for i, (name, tier, group, rarity, hidden) in enumerate(rows):
        d = models.AchievementDefinition(
            source=source,
            set_id=set_id,
            external_id=str(i),
            name=name,
            description=f"{name} desc",
            tier=tier,
            group_id=group,
            group_name=None if group in (None, "default") else f"GAME [{group.upper()}]",
            rarity_pct=rarity,
            hidden=hidden,
            icon_url=f"https://x/{i}.png",
            sort_order=i,
        )
        db.add(d)
        out.append(d)
    db.flush()
    return out


def _earn(db, user, defs, when):
    for d, at in zip(defs, when, strict=True):
        db.add(models.UserAchievement(user_id=user.id, definition_id=d.id, earned=at is not None, earned_at=at))
    db.commit()


def _t(day):
    return datetime.datetime(2025, 1, day, 12, 0)


def test_psn_set_matches_sonys_weighted_progress_and_splits_add_ons(db_session):
    """ASTRO BOT's shape: platinum earned, base game complete, two add-ons
    open. Sony weights by points and leaves the platinum out."""
    user = _user(db_session)
    defs = _define(
        db_session,
        "psn",
        "NPWR1",
        [
            ("Plat", "platinum", "default", 13.6, False),
            ("Gold", "gold", "default", 40.0, False),
            ("Silver", "silver", "default", 60.0, False),
            ("Bronze", "bronze", "default", 70.0, False),
            ("Pack bronze", "bronze", "001", 20.0, False),
            ("Pack secret", "silver", "001", 5.0, True),
        ],
    )
    _earn(db_session, user, defs, [_t(4), _t(2), _t(3), _t(1), None, None])

    v = achievements.set_view(db_session, user.id, "psn", "NPWR1")

    assert (v["noun"], v["earned"], v["total"], v["complete"]) == ("Trophies", 4, 6, False)
    # gold 90 + silver 30 + bronze 15 of 90+30+15+15+30: 135/180.
    assert v["percent"] == 75
    assert [(t["tier"], t["earned"], t["total"]) for t in v["tiers"]] == [
        ("platinum", 1, 1),
        ("gold", 1, 1),
        ("silver", 1, 2),
        ("bronze", 1, 2),
    ]
    assert [(g["name"], g["base"], g["earned"], g["total"], g["percent"]) for g in v["groups"]] == [
        ("Full game", True, 4, 4, 100),
        ("GAME [001]", False, 0, 2, 0),
    ]
    # Rarest earned, not latest; the locked secret is rarer but not earned.
    assert v["rarest"]["name"] == "Plat"
    # Unlocked newest first; locked in game order, a secret one stays secret.
    assert [i["name"] for i in v["unlocked"]] == ["Plat", "Silver", "Gold", "Bronze"]
    assert [(i["name"], i["description"], i["grey"]) for i in v["locked"]] == [
        ("Pack bronze", "Pack bronze desc", True),
        ("Hidden trophy", None, True),
    ]


def test_steam_set_counts_plainly_and_uses_the_grey_icon(db_session):
    user = _user(db_session)
    rows = [("A", None, None, 50.0, False), ("B", None, None, 2.0, False), ("C", None, None, 9.0, False)]
    defs = _define(db_session, "steam", "440", rows)
    defs[2].icon_locked_url = "https://x/2_g.png"
    _earn(db_session, user, defs, [_t(1), _t(2), None])

    v = achievements.set_view(db_session, user.id, "steam", "440")

    assert (v["noun"], v["earned"], v["total"], v["percent"]) == ("Achievements", 2, 3, 67)
    assert v["tiers"] is None and v["groups"] == []
    assert v["rarest"]["name"] == "B"
    assert v["locked"][0]["icon_url"] == "https://x/2_g.png" and v["locked"][0]["grey"] is False


def test_a_single_group_psn_set_has_no_split(db_session):
    user = _user(db_session)
    defs = _define(db_session, "psn", "NPWR2", [("A", "bronze", "default", 10.0, False), ("B", "gold", "default", 9.0, False)])
    _earn(db_session, user, defs, [_t(1), _t(2)])

    v = achievements.set_view(db_session, user.id, "psn", "NPWR2")

    assert v["groups"] == [] and v["complete"] is True and v["percent"] == 100


def test_strips_cap_and_count_the_rest(db_session):
    user = _user(db_session)
    defs = _define(db_session, "steam", "1", [(f"A{i}", None, None, None, False) for i in range(25)])
    _earn(db_session, user, defs, [_t(1 + i % 20) if i < 13 else None for i in range(25)])

    v = achievements.set_view(db_session, user.id, "steam", "1")

    assert (len(v["unlocked"]), v["unlocked_more"]) == (10, 3)
    assert (len(v["locked"]), v["locked_more"]) == (10, 2)
    # No rarity anywhere: the latest earned stands in.
    assert v["rarest"] is v["unlocked"][0]


def test_summary_only_set_shows_counts_and_nothing_else(db_session):
    """Steam's batch progress records counts without rows."""
    user = _user(db_session)
    _define(db_session, "steam", "2", [("A", None, None, None, False)])
    db_session.add(models.UserAchievementSet(user_id=user.id, source="steam", set_id="2", earned=29, total=66))
    db_session.commit()

    v = achievements.set_view(db_session, user.id, "steam", "2")

    assert (v["earned"], v["total"], v["percent"], v["items"], v["rarest"]) == (29, 66, 44, [], None)


def test_nothing_known_is_none(db_session):
    user = _user(db_session)
    _define(db_session, "steam", "3", [("A", None, None, None, False)])
    assert achievements.set_view(db_session, user.id, "steam", "3") is None


def test_a_steam_release_shows_its_own_set_and_an_attached_pc_trophy_set(db_session):
    game = models.Game(title="G")
    db_session.add(game)
    db_session.flush()
    steam = models.GameRelease(
        game_id=game.id,
        platform="Steam",
        source="steam",
        external_id="99",
        raw_data={achievements.PC_SET_KEY: {"npCommunicationId": "NPWR9"}},
    )
    psn = models.GameRelease(game_id=game.id, platform="PS5", source="psn", external_id="PPSA1", raw_data={"npCommunicationId": "NPWR8"})
    assert achievements.sets_of(steam) == [("steam", "99"), ("psn", "NPWR9")]
    assert achievements.sets_of(psn) == [("psn", "NPWR8")]


# ─── In the panes ─────────────────────────────────────────────────────────────

_HX = {"HX-Request": "true"}


def _login(client, db, username="achuser"):
    client.post("/signup", data={"username": username, "password": "pw", "password_confirm": "pw"})
    r = client.post("/login", data={"username": username, "password": "pw"}, follow_redirects=False)
    client.cookies.set("session", r.cookies["session"])
    return db.query(models.User).filter_by(username=username).one()


def _psn_entry(db, user, npwr="NPWR1"):
    game = models.Game(title="ASTRO BOT")
    db.add(game)
    db.flush()
    release = models.GameRelease(game_id=game.id, platform="PS5", source="psn", external_id="PPSA1", raw_data={"npCommunicationId": npwr})
    db.add(release)
    db.flush()
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id)
    db.add(entry)
    defs = _define(
        db,
        "psn",
        npwr,
        [
            ("Astro-nomical!", "platinum", "default", 13.6, False),
            ("Unforgettable!", "silver", "default", 76.4, False),
            ("Timely Rescue", "bronze", "001", 22.5, False),
        ],
    )
    _earn(db, user, defs, [_t(4), _t(2), None])
    return entry


def test_library_pane_shows_the_box_below_completions(client, db_session):
    user = _login(client, db_session)
    entry = _psn_entry(db_session, user)
    db_session.add(models.Completion(user_id=user.id, library_entry_id=entry.id, completed_at=datetime.date(2025, 1, 4)))
    db_session.commit()

    body = client.get(f"/library/entries/{entry.id}/detail", headers=_HX).text

    assert "cgt-ach" in body and "Trophies" in body
    assert body.index("Completions") < body.index("Rarest earned")
    assert "Astro-nomical!" in body and "13.6% of players" in body
    assert "Full game" in body and "1 add-on" in body
    assert f"/library/entries/{entry.id}/achievements?source=psn&amp;set_id=NPWR1" in body
    assert "View all 3" in body


def test_an_entry_without_a_set_has_no_box(client, db_session):
    user = _login(client, db_session)
    game = models.Game(title="Manual")
    db_session.add(game)
    db_session.flush()
    release = models.GameRelease(game_id=game.id, platform="NES", source="manual")
    db_session.add(release)
    db_session.flush()
    entry = models.UserLibraryEntry(user_id=user.id, release_id=release.id)
    db_session.add(entry)
    db_session.commit()

    assert "cgt-ach" not in client.get(f"/library/entries/{entry.id}/detail", headers=_HX).text


def test_full_list_groups_filters_and_links_back(client, db_session):
    user = _login(client, db_session)
    entry = _psn_entry(db_session, user)
    url = f"/library/entries/{entry.id}/achievements?source=psn&set_id=NPWR1"

    body = client.get(url, headers=_HX).text
    # The kind rides in the summary; the bar is just Back and the title.
    assert "of 3 trophies earned" in body
    assert 'class="cgt-ach-list__title text-truncate" title="ASTRO BOT">ASTRO BOT</span>' in body
    assert "Full game" in body and "Add-on · GAME [001]" in body
    assert body.index("Astro-nomical!") < body.index("Timely Rescue")
    assert "cgt-ach-row--locked" in body
    assert f'hx-get="/library/entries/{entry.id}/detail"' in body
    # Its own Back, not the pane stack's.
    assert "cgt-pane-nav" not in body

    locked = client.get(url + "&show=locked", headers=_HX).text
    assert "Timely Rescue" in locked and "Astro-nomical!" not in locked
    pack = client.get(url + "&group=001", headers=_HX).text
    assert "Timely Rescue" in pack and "Unforgettable!" not in pack


def test_full_list_only_serves_the_entrys_own_sets(client, db_session):
    user = _login(client, db_session)
    entry = _psn_entry(db_session, user)
    _psn_entry(db_session, user, npwr="NPWR2")

    assert client.get(f"/library/entries/{entry.id}/achievements?source=psn&set_id=NPWR2", headers=_HX).status_code == 404
    other = _login(client, db_session, username="someoneelse")
    assert other.id != user.id
    assert client.get(f"/library/entries/{entry.id}/achievements?source=psn&set_id=NPWR1", headers=_HX).status_code == 404


def test_completion_pane_box_returns_to_the_completion(client, db_session):
    user = _login(client, db_session)
    entry = _psn_entry(db_session, user)
    c = models.Completion(user_id=user.id, library_entry_id=entry.id, completed_at=datetime.date(2025, 1, 4))
    db_session.add(c)
    db_session.commit()

    body = client.get(f"/completions/{c.id}/detail", headers=_HX).text
    assert f"set_id=NPWR1&amp;completion_id={c.id}" in body
    assert 'hx-target="#completion-detail-content"' in body

    lst = client.get(f"/library/entries/{entry.id}/achievements?source=psn&set_id=NPWR1&completion_id={c.id}", headers=_HX).text
    assert f'hx-get="/completions/{c.id}/detail"' in lst
