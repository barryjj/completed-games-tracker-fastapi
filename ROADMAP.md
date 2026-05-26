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

### Library detail pane ✅ (PR #64)
- Click a library row → slide-out (Bootstrap offcanvas) detail pane via HTMX
- Single place for cover art + appdetails description + parent navigation + completion history
- Child DLC list with click-through to swap the pane to the child's detail
- Edit / Hide / Unhide / Remove actions inside the pane (Edit still opens the existing modal for now)
- Reduces the need for inline nesting/grouping — clicking a parent reveals its children in the pane

### Completion detail pane ✅ (this PR)
- Click a completion row → slide-out offcanvas mirroring the library detail pane
- Cover art, platform badge, **completion-specific** facts (date, playthroughs, notes), library context (playtime, store link)
- Lists other completions of the same game with HTMX click-through to swap the pane
- Edit reuses existing edit-completion modal
- "View in library ↗" link navigates to `/library?detail={entry_id}` which auto-opens the library pane for that game

### Grid view polish round 2 ✅ (this PR)
- **Borderless toggle** — flips `.cgt-library-grid--borderless` to strip borders, radius, and hover lift for a true Steam-library edge-to-edge mosaic
- **Cover-size slider** — second range input controls `--cgt-grid-card-min` driving `grid-template-columns: repeat(auto-fill, minmax(...))`. Range and default scale with orientation (100-320 vertical, 200-480 horizontal). Per-orientation localStorage keys so portrait/landscape have independent preferences.
- **Catppuccin-styled sliders** — rectangular thumb in mauve, matches the rest of the design's hard-edged controls. `.cgt-range` class applies anywhere.
- **Cover-only cards by default** — dropped title/badges below the cover for a clean mosaic look. Placeholder cards (no matching-orientation art) show centered title + platform inside the card itself.
- **DLC stripe indicator** — 3px peach stripe at the top of DLC cards. Quiet in default-view, useful in DLC/Everything views.
- **Hidden indicator** — small em-dash badge top-right of hidden entries (visible when Show hidden is on).
- **List view row thumbnails** — small 64×30 header.jpg thumbnail in the title cell. Manual entries without artwork get no thumbnail rather than a blank rectangle.
- Placeholder fallback chain: cover image renders alongside an always-present placeholder div; CSS hides the placeholder while the cover is present, so a cover img onerror that removes the cover automatically reveals the placeholder. No JS state machine needed.

### Mosaic export (future)
- "Export mosaic" button that picks N covers (filter-aware? completion-aware? user-selectable?) and stitches them into a single image
- Use case: theme image for Steam library, social-media shareable
- Server-side: PIL or similar; offer multiple aspect ratios + grid dimensions
- Roadmap-only for now

### Library grid view ✅ (PR #70)
- Three-way toggle in the library header: List / Grid (vertical covers) / Grid (horizontal covers)
- View mode is server-rendered via `?view_mode=...` query param; localStorage persists the preference across visits (one-time JS redirect when URL lacks the param)
- Spacing slider in the header controls `--cgt-grid-gap` CSS variable; persisted to localStorage
- Vertical grid uses Steam `library_600x900.jpg`; horizontal uses `header.jpg`. **No cross-orientation borrowing** — entries without the matching artwork get a clean gradient placeholder card rather than a stretched/squished image.
- Cards click straight through to the existing detail pane (same delegated handler matches both `tr` and `div` with `.library-row-clickable`)
- Library list query eager-loads `GameArtwork` via `contains_eager(release).selectinload(artwork)` so cards don't N+1
- Reusable across other pages later (completions grid is its own roadmap item)
- SteamGridDB integration placeholder card added to /integrations as a foundation for the later cover-art lookup feature

### Custom cover art via SteamGridDB ✅ (this PR)
- Per-user SGDB API key on `/integrations/steamgriddb` configure page
- "Find vertical cover" / "Find horizontal cover" entries in the library detail pane's More dropdown (only shown when a key is saved)
- Steam entries look up by appid via `/games/steam/{appid}`; non-Steam entries fall back to title autocomplete
- Picker modal shows up to 20 candidates filtered to the matching aspect ratio (600x900 or 460x215/920x430)
- Clicking a candidate POSTs to `/library/entries/{id}/cover-override` → writes URL to `cover_url_override_v` or `_h` → grid/detail render the new cover on next load
- Especially important for manual entries (no Steam artwork available) and PSN entries (different art catalog)

### Bulk SGDB "fill in the gaps" job (next)
- One-shot endpoint that walks every library entry missing matching-orientation artwork, runs SGDB lookup, applies the top candidate
- User-triggered from the SGDB configure page; reports counts (filled / no-candidate / skipped) when done
- Skips entries that already have an override or matching-orientation `GameArtwork`

### Per-entry refresh metadata + dropdown actions menu ✅ (PR #69)
- New `POST /library/entries/{id}/refresh-metadata` — synchronous one-off appdetails fetch for a single Steam entry, bypasses the background worker's queue
- Re-runs the worker's post-fetch logic (promote/demote `is_dlc`, link `parent_id`, auto-hide) so a misclassified entry self-corrects in one click
- Handles 429 gracefully with a "rate-limiting, try again" toast — no DB writes on failure
- Both detail panes get a **More ▾** dropdown footer: secondary actions (Refresh, Hide/Unhide, Remove on library; Refresh, Delete on completion) move into the dropdown, primary actions (Edit, View in library) stay visible
- Pattern scales for future actions: "Add to completions" on a library entry, IGDB lookup, etc.

### Bulk re-apply heuristics (deferred — when we change heuristic logic without re-fetching)
- One-shot endpoint that walks every Steam release with cached `raw_data["appdetails"]` and re-runs the post-fetch classification logic
- No network calls — uses already-cached data. Useful when we fix a heuristic bug and don't want to spend 10 hours re-enriching
- Parked because per-entry refresh covers the immediate need; bulk re-apply is a "we changed classification logic" fix, not a daily-use tool

### Library polish round A ✅ (PR #66)
- Library sort: `COALESCE(display_name, title) COLLATE NOCASE` so display order matches what you see (fixes "Influent DLC" landing before number-prefixed entries)
- DLC reconciliation now goes both ways: appdetails `type=game` + currently `is_dlc=True` + not user-set → demote to False. Catches misclassifications from the rgOwnedApps subtraction.
- Auto-hide regex expanded with patterns from real-world fighting-game DLC: character/season/ultimate/stage/kombat pass, skin/costume/outfit/cinematic/customization pack, add-on bundle, avatar skin/costume, DLC playable character, Deluxe upgrade
- Auto-hide is now hard-gated on `is_dlc=True` — games never get auto-hidden by any heuristic
- Steam store link in the detail pane (Steam entries only, opens new tab)
- "Auto-hide non-games" button moved off the library page → Steam configure page (under "More sync options" with the other backfills)

### External store / cross-reference links
- Detail pane links to canonical sources for each entry
- Steam: store URL (done in polish round A)
- PSN: store URL once PSN integration lands
- IGDB: link when `igdb_id` is set
- SteamGridDB: artwork browser link for cover-art lookup once cover override is wired up
- Possibly: HowLongToBeat link once we have title-based search

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

### Steam OpenID identity ✅ (this PR)
- "Sign in through Steam" button on the configure page
- OpenID 2.0 redirect → Steam → return with signed params → POST back for verification
- SteamID parsed from `claimed_id`; persona name fetched via GetPlayerSummaries when API key is set
- API key + cookies still require manual paste (Steam doesn't issue API keys via OpenID, and session-cookie capture needs Tauri)
- Pure web — no Tauri prerequisite for this part

### Steam cookie capture (Tauri-only)
- Eliminates the `steamLoginSecure` / `sessionid` manual paste step
- Requires the Tauri desktop wrapper to host a WebView that can intercept Steam's response cookies after login
- Pair with the existing OpenID identity flow: one "Sign in through Steam" click → SteamID + persona + cookies all captured at once
- Web-only build keeps the manual cookie fields as a fallback

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
