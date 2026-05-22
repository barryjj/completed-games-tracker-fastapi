# Roadmap

Rough grouping of planned work. No dates or priority scores — order within each section is approximate.

---

## In progress / next up

### Async job system
- Background tasks for all long-running sync operations (Steam games, DLC, future IGDB/SteamGridDB)
- SSE (`sse-starlette`) to push job completion back to the client
- Toast notification system in the UI — fires on any async job completion with result summary and appropriate color
- "Sync All" button: kicks off games + DLC sync together as background tasks

### Steam DLC — better approach
- Add `steamLoginSecure` + `sessionid` cookie fields to Steam settings (like API key)
- Use `store.steampowered.com/dynamicstore/userdata/` (`rgOwnedApps`) to get full owned app list including DLC in one request — replaces 10k × 0.3s `appdetails` scrape
- Existing `appdetails` approach as fallback for users who don't provide cookies

### Modal forms — phase 2 (branch: feature/modal-forms-and-polish)
- Add Completion modal (currently inline `<details>`)
- Edit Completion modal (reuse same modal, swap to PATCH)
- Edit Library Entry: replace filtered dropdowns with typeahead search for parent game / collection

---

## Near-term

### Game detail pane
- Click a library row → slide-out panel showing full metadata, edit controls, completion history, child entries (DLC, games within a collection)

### Cover art / grid view
- Grid view toggle on library page showing cover art
- Steam CDN cover URLs already stored in `GameArtwork` table

### Platforms table
- `platforms` table: `internal_name`, `display_name` (user-editable), `color_key`, `sort_order`, `is_system`
- `GameRelease.platform` becomes FK to platforms instead of free text
- Seed defaults: Steam, PS5, PS4, PS3, Switch, Xbox, iOS, Android, PC, Other
- Users can add custom platforms (NES, Dreamcast, etc.) and rename display names
- Color key maps to Catppuccin token — replaces current heuristic matching in `_platform_color_class`

### PSN integration
- PSN OAuth flow: open browser to login URL, user completes login, capture NPSSO token from cookies
- Token stored and refreshed (valid ~6 months); used to pull library and trophy data
- Similar cookie-capture pattern to Steam approach above

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
