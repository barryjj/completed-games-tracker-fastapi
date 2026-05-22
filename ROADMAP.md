# Roadmap

Rough grouping of planned work. No dates or priority scores — order within each section is approximate.

---

## In progress / next up

### Unified Steam sync (branch: feature/unified-steam-sync)
- Single "Sync" button that gets everything in 3 API calls: GetOwnedGames (games + playtime) + rgOwnedApps via cookies (all owned app IDs) + GetAppList (name index cached 7 days)
- DLC = rgOwnedApps minus GetOwnedGames IDs; names resolved from GetAppList cache
- Replaces separate Sync Games / Sync DLC buttons and the old per-game appdetails scrape
- Old appdetails approach kept as fallback for users without cookies

### Steam OAuth / "Sign in through Steam"
- Replace manual API key + cookie fields with OpenID "Sign in through Steam" flow
- Captures session cookies automatically after login — no manual DevTools copy-paste
- API key still needed for GetOwnedGames; long-term goal is to eliminate it too
- Depends on Tauri for proper cookie capture (desktop) or a redirect flow (web)

### Library and completions polish
- Click a library row → slide-out detail pane showing metadata, edit controls, completion history, child DLC
- Grid view toggle on library page using Steam CDN cover art (already stored in GameArtwork table)
- Completions page: cover thumbnails alongside game titles

### Async job system
- Background tasks for sync operations (already slow; will get slower with 18k DLC)
- Toast notification in UI when job finishes with result summary
- Polling-based (no new dependencies); SSE upgrade later if needed

---

## Near-term

### Platforms table
- `platforms` table: `internal_name`, `display_name` (user-editable), `color_key`, `sort_order`, `is_system`
- `GameRelease.platform` becomes FK to platforms instead of free text
- Seed defaults: Steam, PS5, PS4, PS3, Switch, Xbox, iOS, Android, PC, Other
- Users can add custom platforms (NES, Dreamcast, etc.) and rename display names
- Color key maps to Catppuccin token — replaces current heuristic matching in `_platform_color_class`

### PSN integration
- PSN OAuth flow: open browser to login URL, user completes login, capture NPSSO token from cookies
- Token stored and refreshed (valid ~6 months); used to pull library and trophy data

---

## Medium-term

### IGDB / Twitch
- Twitch Client Credentials OAuth (`TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` env vars)
- IGDB search on manual game add: typeahead lookup, select result, auto-fill title, store `igdb_id`
- Cover art via IGDB → `GameArtwork`

### SteamGridDB
- Cover art fallback for games missing Steam/IGDB images
- "Get cover options" on game detail/edit pane — fetches options, user picks one
- API key stored in user settings

### Stats & dashboard / home page
- Customizable widget-based home page
- Widgets: completions per year chart, playtime breakdown, games added this year, completion streak, 52-games-a-year challenge tracker
- User can pick which widgets are shown and arrange them

### Historical import
- Import completions from Google Sheets / CSV
- Map columns to game title, platform, date completed

---

## Later

### Desktop packaging (Tauri)
- Wrap app in Tauri shell: FastAPI backend as sidecar, WebView for frontend
- Enables proper OAuth/cookie capture flows for Steam and PSN without manual copy-paste
- Bundles into a single .app / .exe
- Target Mac first (user's primary machine), Windows second

### Platform preferences
- User settings: check/uncheck platforms you own or want to track
- Library and completions filters respect this by default

### Collections / sub-games view
- "What's in this collection" view from detail pane
- Bulk-complete sub-games
