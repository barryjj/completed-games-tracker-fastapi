# Roadmap

Rough grouping of planned work. No dates or priority scores — order within each section is approximate.

---

## In progress / next up

### Toast notifications (started)
- Bottom-right toast container in `base.html`, stacks vertically
- Auto-dismiss after ~10s; manual X to close
- Two variants: success (Catppuccin green) and error (Catppuccin red)
- HTMX out-of-band (OOB) swap pattern so any endpoint can push a toast without per-page wiring
- Replaces the current inline `#steam-flash` / `#steam-hub-status` div pattern

### Async job system + background sync
- Pairs with toasts above: sync runs as a background task, user can navigate away
- In-process job tracker keyed by user_id, simple status enum (queued/running/done/failed)
- `GET /jobs/{id}` polling endpoint returning current status + result summary
- Small JS poller in `base.html` that surfaces the toast on completion
- No new dependencies; SSE upgrade considered later if polling feels rough

### Hidden flag + auto-hide heuristic
- `is_hidden: bool` on `UserLibraryEntry` (per-user, not per-game)
- Library default view filters `is_hidden=false`; "Show hidden" toggle reveals them
- Enrichment worker auto-flags entries when `appdetails` indicates soundtrack/artbook/OST etc. (categories or `type=music`, plus title heuristics)
- Manual hide/unhide from the row's action menu

### Library detail pane
- Click a library row → slide-out detail pane showing metadata, edit controls, completion history, child DLC
- Single place for cover art + appdetails description + parent navigation
- Reduces the need for inline nesting/grouping (clicking a parent already reveals children)

### Library nesting / grouping (after detail pane)
- Default view: parent collapsed with `[N DLC] ▸` indicator, expandable inline
- Search match on DLC name: parent expanded with matching DLC highlighted
- DLC-only filter: flat list with parent name shown alongside each row
- Sort by added date meaningful only at parent level

### Cover art grid view
- Grid toggle on library page using Steam CDN cover art (already stored in `GameArtwork` table)
- Completions page: cover thumbnails alongside game titles

### Steam OAuth / "Sign in through Steam"
- Replace manual API key + cookie fields with OpenID "Sign in through Steam" flow
- Captures session cookies automatically after login — no manual DevTools copy-paste
- API key still needed for GetOwnedGames; long-term goal is to eliminate it too
- Depends on Tauri for proper cookie capture (desktop) or a redirect flow (web)

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
