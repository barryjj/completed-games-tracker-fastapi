# Roadmap

Rough grouping of planned work. No dates or priority scores — order within each section is approximate.

---

## Completed (recent)

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

### Steam sync operations consolidation ✅ (PR #57 area)
- One primary "Sync" button (full library) — what 95% of usage wants
- Power-user / diagnostic ops collapsed in a `<details>` block: Sync DLC only, Sync games only, Refresh App Catalog, Refresh Metadata, Test Cookies
- All ops go through the job system: started toast → background → completion toast, no matter how slow
- Job kind table (`_STEAM_KINDS`) is the only wiring needed when adding a new op
- Platform prefix on completion messages (`"Steam sync complete — ..."`) so PSN can drop in with `"PSN sync complete — ..."` later

### User-override flags + auto-hide for non-games ✅
- `display_name_user_set`, `is_dlc_user_set`, `is_collection_user_set`, `parent_id_user_set` on `Game`; `is_hidden` + `is_hidden_user_set` on `UserLibraryEntry`
- Pattern: any time a heuristic could stomp a user-editable field, the `_user_set` flag is checked first
- `_should_auto_hide` heuristic; library default filters `is_hidden=false`; per-row Hide / Unhide actions

### Library + completion detail panes ✅ (PR #64, later PRs)
- Click a library or completion row → slide-out Bootstrap offcanvas detail pane via HTMX
- Cover art, metadata, description, platform badge, completion history, DLC child list, back-navigation
- More dropdown with Edit / Refresh / Hide / Remove / SGDB pickers / IGDB link actions

### Grid view + polish rounds ✅ (PR #70 and subsequent)
- Three-way toggle: List / Grid (vertical) / Grid (horizontal)
- Cover-size slider, spacing slider, borderless toggle — all persisted to localStorage per orientation
- Completions page got the full grid treatment (PR #107 area)
- Server-side view-mode cookie kills the list-flash-to-grid lag
- Sticky bottom toolbar with + Add Game / + Log Completion
- Infinite scroll replacing pagination (PR #107); back-to-top button; PAGE_SIZE 200

### Artwork framework ✅ (PR #95, #96, #103, #104)
- Two-table design: `GameArtwork` (auto-sourced, shared) and `UserArtwork` (explicit user picks, per entry)
- Resolution priority: UserArtwork → GameArtwork native (steam/psn) → GameArtwork sgdb/igdb → placeholder
- Artwork type rename: `cover` → `cover_v`, `header` → `cover_h`; source `steamgriddb` → `sgdb`
- Deprecated override columns on `UserLibraryEntry` dropped (PR #104)
- URL verification background job: HEAD-checks `GameArtwork` rows, marks invalid on 404

### SteamGridDB integration ✅ (PR #83 area, #94, #96)
- Per-user SGDB API key; picker modal for vertical/horizontal covers, heroes, logos
- Animated WebP/GIF support; pagination ("Load more")
- Bulk fill-missing job via job system (started toast → completion toast with counts)
- SGDB writes to `UserArtwork`

### IGDB / Twitch integration ✅ (PR #102)
- Twitch Client Credentials OAuth (Client ID + Secret stored per-user)
- Access token cached in-process, auto-refreshed on expiry
- `igdb_id` nullable FK on `Game`; manual add flow has typeahead search + IGDB confirmation card
- Cover art from IGDB written to `GameArtwork` (source `igdb`); summary/genres/year in `release.raw_data['igdb']`
- Edit modal IGDB section for manual entries; unlink clears igdb_id + marks IGDB artwork invalid
- Direct ID lookup escape hatch for games filtered out of search results

### Platforms table ✅ (PR #106)
- `platforms` table: 76 rows seeded from IGDB, each with Catppuccin accent `color` and optional `display_name` override
- `GameRelease.platform_id` FK; `display_platform` + `platform_tag_class` properties handle linked/unlinked
- Settings > Platforms tab: rename display name, pick badge color, live filter, "Show only in library" toggle
- Full accent coverage across all platform families (Nintendo→red, PlayStation→lavender, Sega→yellow, etc.)

### Steam integration polish ✅ (various PRs)
- Steam OpenID identity: "Sign in through Steam" button; SteamID + persona + avatar captured at return
- SteamSpy fallback for `App NNNNNN` placeholder titles (PR #108)
- Periodic TTL metadata refresh: re-queues 50 stale entries after NULL-queue drains
- Enrichment worker: DLC reconciliation both ways; auto-hide two-tier heuristic
- Library polish: COALESCE sort, recently-played sort, missing-artwork filter

### Add/edit modal stability + detail pane consolidation ✅ (PR #101)
- Add-game modal no longer closes on search result load
- Edit modal parent selection uses live HTMX search; pre-fills from server-stamped data attrs
- Edit moved into More dropdown on both panes for consistent single-action-menu

---

## In progress / next up

### Sync match review
- Case: user manually adds "RE8" on Steam → later syncs → sync sees `Resident Evil Village` (appid 1196590) on the same platform
- Dedicated **review page** (not toast-based — could be overwhelming with bulk matches)
- Side-by-side per match: manual entry vs. detected game; user picks Merge / Keep Separate / Always Separate
- Merge: `UserLibraryEntry` survives (preserving completions), release row gets `external_id` + `raw_data` + artwork, `source` flips from `"manual"` to `"steam"` / `"psn"`; `display_name_user_set=True` protects the user's display name
- Review queue is platform-agnostic — PSN adds rows to the same queue without rework
- Building this before PSN means PSN sync gets proper duplicate handling from day one

### Historical import (after sync match review)
- Import completions from CSV / Google Sheets: map columns to game title, platform, date completed
- Platforms table ✅ and IGDB title-matching ✅ are prerequisites — both done
- Does NOT require PSN or Tauri: PSN games in the CSV create manual entries, which merge when PSN integration lands later via the sync match review queue
- Target use case: 2006–2012 era games across PS2, PS3, Xbox 360, etc. that predate any sync integration
- Runs through the sync match review queue so imported entries that overlap with Steam data surface for approval
- Auto-create library entries for games not already present; flag for review if a close match exists

### Achievements / trophies (Steam first, PSN-aware schema)
- **Schema design: unified, release-scoped**
  - `Achievement` table: `(release_id, source, api_name)` unique key — keyed to release, not game, because Steam achievements and PSN trophies are per-release (separate trophy lists for PS4 vs PS5 versions of the same game)
  - Columns: `name`, `description`, `icon_url`, `hidden`, `sort_order`, `global_unlock_pct` (nullable), `trophy_type` (nullable — bronze/silver/gold/platinum for PSN only), `extra_data` JSON (pressure valve for platform-specific fields without migrations)
  - `UserAchievement` table: `(user_id, achievement_id)` — `unlocked` bool, `unlocked_at` (nullable), `extra_data` JSON
  - `source` discriminator (`steam` / `psn`) is the only branching point; all queries and UI work the same regardless of platform
- **Display:** per-release, never cross-release summed. Grouped library view shows each platform's progress independently ("Steam: 23/47 | PS5: 8/32 trophies")
- **Steam implementation:** `GetSchemaForGame` (achievement list + icons, once per game) + `GetPlayerAchievements` (user unlock state) via enrichment worker / job system
- `achievements.total` already present in cached `appdetails` — surface as "X / 27" once player sync exists, not before
- Detail pane: "Achievements" section — earned / total + recent unlocks with icons
- Library / completion list + grid: progress cell in the vacated "Added" column slot (list view) or small badge on cards
- Filter / sort by achievement progress (e.g. "show games close to 100%")
- PSN trophy mapping is a follow-on once PSN integration exists — schema requires no redesign

---

## Near-term

### Desktop packaging / Tauri
- Wrap app in Tauri shell: FastAPI backend as sidecar, WebView for frontend
- Required for PSN cookie capture (NPSSO token interception after login) — the primary blocker for full PSN integration
- Steam cookie capture also lands here: one "Sign in through Steam" click → SteamID + persona + cookies all at once
- Web-only build keeps manual cookie fields as a fallback
- Target Mac first (primary machine), Windows second
- Bundles into a single .app / .exe

### PSN integration (after Tauri)
- PSN OAuth flow: open browser to login URL, user completes login, capture NPSSO token via Tauri WebView
- Token stored and refreshed (valid ~6 months); used to pull library and trophy data
- Platforms table ✅ already seeded with PS5/PS4/PS3/Vita/PSP rows
- Imported PSN games run through the sync match review queue to merge with any existing manual entries from historical import
- Trophy data writes to `UserAchievement` rows against PSN-source `Achievement` rows — no schema changes needed

### Library display grouping by game (cosmetic only — NOT a merge)
- User owns RE8 on PS4, PS5, AND Steam → library shows ONE row with three platform badges
- DB rows stay separate (per-platform completion data and achievement progress preserved)
- Schema already supports it — multiple `GameRelease` rows can point to one `Game.id`
- Toggle: "Group by game" checkbox in library filter row (default on)
- Detail pane shows per-platform breakdown with separate completion history and achievement progress per release

### Sort name field
- `sort_name` nullable column on `Game`; auto-populated from `display_name` (or `title`) on create/edit unless explicitly overridden
- Lets users fix franchise sort order (e.g. "DmC: Devil May Cry" → sort as "Devil May Cry 0") without touching the display name
- Low urgency — can land any time as a small standalone PR

### User-configurable DLC auto-hide keywords
- System-default patterns (current `_AUTO_HIDE_RE`) seeded into a `dlc_hide_keywords` table; users can add/disable entries
- Low urgency; current heuristic covers the common cases well enough

---

## Medium-term

### Stats & dashboard / home page
- Customizable widget-based home page
- Widgets: completions per year chart, playtime breakdown, games added this year, completion streak, 52-games-a-year challenge tracker
- User can pick which widgets are shown and arrange them
- Deferred until library has more non-Steam data (PSN, historical import) so the stats are actually interesting

### Navbar avatar
- Small profile picture in the navbar alongside the username
- Requires avatar-source picker: which integration wins (Steam / PSN / uploaded), fallback for no integrations
- Steam avatar URL already captured — Steam users get a freebie once the picker exists

### Steam news / announcements in detail pane
- `ISteamNews/GetNewsForApp` returns dated announcements per appid; cached and shown in the library detail pane as a "Latest news" section
- Natural follow-on to achievements work (same extra-per-game-data neighborhood)

### External store / cross-reference links
- IGDB: link when `igdb_id` is set (detail pane More dropdown)
- PSN: store URL once PSN integration lands
- Possibly: HowLongToBeat link via title search

### Mosaic export
- "Export mosaic" button: picks N covers (filter-aware / completion-aware / user-selectable) and stitches into a single image
- Use case: Steam library theme image, social-media shareable
- Server-side PIL; multiple aspect ratios + grid dimensions

---

## Later

### Library nesting / grouping
- Default view: parent collapsed with `[N DLC] ▸` indicator, expandable inline
- Search match on DLC name: parent expanded with matching DLC highlighted
- DLC-only filter: flat list with parent name shown alongside each row

### Platform preferences
- User settings: check/uncheck platforms you own or want to track
- Library and completions filters respect this by default

### Collections / sub-games view
- "What's in this collection" view from detail pane
- Bulk-complete sub-games

### Bulk re-apply heuristics
- One-shot endpoint that walks every Steam release with cached `raw_data["appdetails"]` and re-runs post-fetch classification logic
- No network calls — uses already-cached data. Useful when a heuristic bug is fixed without wanting to re-enrich
- Parked because per-entry refresh covers the immediate need
