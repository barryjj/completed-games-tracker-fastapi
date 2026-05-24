# Roadmap

Rough grouping of planned work. No dates or priority scores — order within each section is approximate.

---

## In progress / next up

### Toast notifications ✅ (PR #55 / styling polish PR #56)
- Bottom-right toast container in `base.html`, stacks vertically, auto-dismiss after 10s
- HTMX out-of-band (OOB) swap pattern so any endpoint can push a toast without per-page wiring
- Catppuccin-tinted backgrounds with colored accent stripe (success/danger variants)

### Async job system + background sync ✅ (PR #57)
- In-process job tracker (`backend/jobs.py`) keyed by user_id, status enum (queued/running/done/failed)
- Sync endpoints kick off `asyncio.create_task` and return a "started" toast immediately
- `GET /integrations/jobs/poll` polled every 5s from `base.html`; returns OOB completion toasts for any of the user's jobs that finished since the last poll
- `notified` flag on each job ensures toasts surface exactly once
- Works across page navigation — the poller lives in `base.html` so the user gets the toast wherever they are when the sync finishes
- No new dependencies; SSE upgrade considered later if polling feels rough

### Steam sync operations consolidation ✅ (this PR)
- One primary "Sync" button (full library) — what 95% of usage wants
- Power-user / diagnostic ops collapsed in a `<details>` block: Sync DLC only, Sync games only, Refresh App Catalog, Refresh Metadata, Test Cookies
- All ops go through the job system: started toast → background → completion toast, no matter how slow
- Job kind table (`_STEAM_KINDS`) is the only wiring needed when adding a new op
- Platform prefix on completion messages (`"Steam sync complete — ..."`) so PSN can drop in with `"PSN sync complete — ..."` later
- Removed Fix Display Names / Fix Collection Flags buttons from UI; endpoints and functions kept for future user-configurable cleanup rules
- New `steam.sync_dlc_only()` (cookie-based DLC refresh using already-synced games as the baseline)
- New `steam.refresh_app_catalog()` (force re-fetch the 200k Steam app catalog when the 7-day cache misses something new)

### User-override flags + auto-hide for non-games ✅ (this PR)
- `display_name_user_set`, `is_dlc_user_set`, `is_collection_user_set`, `parent_id_user_set` on `Game`; `is_hidden` + `is_hidden_user_set` on `UserLibraryEntry`
- Pattern: any time a heuristic could stomp a user-editable field, the `_user_set` flag is checked first. True means "the user said so; don't touch."
- Existing edit modal sets all four `Game` flags on save
- Manual add: every Game flag is True from creation (the user typed every field, so they own them all)
- New ALL CAPS → Title Case normalization in `_clean_title` with explicit acronym/Roman-numeral preservation list (idempotent, respects user override)
- `_should_auto_hide` heuristic flags soundtracks / artbooks / cosmetic packs based on `appdetails.type=="music"` or title patterns; enrichment worker applies it (respecting `is_hidden_user_set`)
- Library: default query filters `is_hidden=false`; "Show hidden" checkbox toggles
- Per-row Hide / Unhide actions (both set `is_hidden_user_set=True` so the heuristic stays out of the way)
- One-shot `POST /library/backfill-hidden` endpoint to apply the heuristic across existing entries without waiting for the enrichment worker

### Library detail pane ✅ (this PR)
- Click a library row → slide-out (Bootstrap offcanvas) detail pane via HTMX
- Single place for cover art + appdetails description + parent navigation + completion history
- Child DLC list with click-through to swap the pane to the child's detail
- Edit / Hide / Unhide / Remove actions inside the pane (Edit still opens the existing modal for now)
- Reduces the need for inline nesting/grouping — clicking a parent reveals its children in the pane

### Sync match review (same platform, NOT cross-platform)
- Case: user manually adds "RE8" on Steam → later syncs Steam library → sync sees `Resident Evil Village` (appid 1196590) on the same platform with a similar title
- Dedicated **review page** (not toast-based — could be overwhelming with bulk matches if user did heavy manual setup before syncing)
- Side-by-side per match: manual entry vs. detected Steam/PSN game; user picks Merge / Keep Separate / Always Separate (suppresses the pair from future review)
- Merge: same `UserLibraryEntry` survives (preserves any logged completions), release row gets the platform's `external_id`, `raw_data`, artwork; `source` flips from `"manual"` to `"steam_import"` / `"psn_import"`; `display_name_user_set` already True from manual add means the cleanup heuristic still won't touch the user's display name
- Triggered: sync queues matches for review (doesn't block sync completion); also accessible from a "Review possible duplicates" link

### Library display grouping by game (cosmetic only — NOT a merge)
- User owns RE8 on PS4, PS5, AND Steam → library shows ONE row with three platform badges
- DB rows stay separate (per-platform completion data preserved)
- Schema already supports it — multiple `GameRelease` rows can point to one `Game.id`; this is a query + template change only
- Toggle: "Group by game" checkbox in library filter row (default on)
- Detail pane shows per-platform breakdown with separate completion history per release

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
