# Roadmap

Rough grouping of planned work. No dates or priority scores — order within each section is approximate.

---

## Near-term

### Library UX
- Default library view hides DLC and games-within-collections (anything with `parent_id` set)
- Filter bar checkboxes: **Also include: [ ] DLC  [ ] Games in collections** — independent toggles, both off by default
- Manual add: require parent selection when DLC or "part of collection" is checked (can't be one without the other)
- Game detail pane: click a library row → slide-out panel showing full metadata, edit controls, completion history for that game, and child entries (DLC owned, games within a collection)

### Steam
- DLC sync: detect DLC app type via `appdetails`, auto-set `is_dlc=True` and link `parent_id` via `fullgame.appid`

---

## Medium-term

### IGDB / Twitch
- Twitch Client Credentials OAuth flow (`TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` env vars)
- IGDB search on manual game add: type-ahead lookup, select result, auto-fill title and store `igdb_id`
- Cover art via IGDB → `GameArtwork`

### SteamGridDB
- Cover art fallback for games missing Steam images
- "Get cover options" button on the game detail/edit pane — fetches options from SteamGridDB, user picks one

### Platform preferences
- User settings tab: check/uncheck platforms you own or want to track
- Library and completions filters respect this preference by default

---

## Later

### Stats & dashboard
- Completions per year chart
- Playtime breakdown
- Completion streaks, 52-games-a-year challenge tracker

### PSN integration
- Import PSN library and trophy data

### Historical import
- Import completions from Google Sheets / CSV
- Map columns to game title, platform, date completed

### Collections / sub-games view
- "What's in this collection" view from the detail pane
- Bulk-complete sub-games (e.g. finish all games in a collection)
