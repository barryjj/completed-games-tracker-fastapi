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

### Bulk SGDB "fill in the gaps" job ✅ (this PR)
- `POST /integrations/steamgriddb/fill-missing` with `image_type=v|h|hero|logo`
- Walks every visible library entry; for each one missing art of the given type (no override AND no `GameArtwork` row of the right type), looks it up on SGDB and writes the top candidate
- Runs through the job system — started toast on click, completion toast with counts (filled / no_candidate / skipped / errored) when done
- Single errored entry doesn't abort the whole run; logged and counted
- Dropdown on the SGDB configure page with four types (vertical covers / horizontal covers / hero images / logos), only shown when an API key is saved

### Steam integration polish (avatar + ID64 cleanup + hub enrichment) ✅ (this PR)
- Drop the Steam ID64 input field from the configure page — OpenID owns the SteamID now; manual paste is no longer an option
- New `steam_avatar_url` column on User, populated from `GetPlayerSummaries` (`avatarmedium`) at OpenID return time
- Configure page shows the avatar + persona + SteamID in a single tight identity row
- New "Forget Steam sign-in" button that clears SteamID + persona + avatar without touching API key / cookies
- Credentials form no longer accepts `steam_id64`; "Clear Credentials" only wipes API key + cookies
- Integrations hub Steam card surfaces the avatar + a compact enrichment status line (lazily loads the existing `/integrations/steam/enrichment-status` partial)

### Toolbar collapse + completions grid port ✅ (this PR)
- Both library and completions toolbars now have a slim top strip (primary action + Filters / View toggle buttons) over two independently-collapsible drawers
- Drawer state is persisted in localStorage per page (`cgt-lib-*-open`, `cgt-comp-*-open`); first-time users land with both open
- Completions page got the full grid treatment: view-mode toggle (List / Grid v / Grid h), size + gap sliders, borderless toggle, list-view thumbnails
- New `completion_card.html` partial reuses the library's `.cgt-library-grid` classes so size / gap / borderless controls drive it via the same CSS variables
- Grid completion card has a bottom date strip — completions are date-driven, so the date earns visible space (unlike library cards which are cover-only)
- Drawer toggle helpers (`cgtToggleDrawer` / `cgtInitDrawer`) live in `app.js` and are shared across pages

### Round-3 polish: drawer toggle, back-nav, app-id titles, SGDB pagination ✅ (this PR)
- **3px peach DLC stripe removed.** DLC differentiation now comes from the placeholder card (cover-less for DLC whose Steam library_600x900 doesn't exist).
- **Drawer toggle (View/Filters) actually toggles now.** Bootstrap's `.d-flex` / `.row` ship `display: flex !important` which beats the browser default `[hidden] { display: none }`. Added universal `[hidden] { display: none !important; }` override.
- **Detail-pane back-navigation.** When the user clicks through to a child DLC (or a parent) inside the detail pane, a "←" button appears in the pane header. Per-pane stack tracked via HTMX events; fresh opens reset.
- **"App {appid}" placeholder titles fixed.** Enrichment worker now backfills the title from `appdetails.name` when the stored title is the `f"App {appid}"` sync-time fallback. Per-entry refresh-metadata endpoint gets the same treatment. Respects `display_name_user_set`.
- **Re-clean display names button** re-added under Steam config → More sync options. The backfill endpoint now skips entries the user has manually edited (`display_name_user_set`) and re-applies the heuristic regardless of whether `display_name` was previously set. Idempotent.
- **SteamGridDB picker pagination.** SGDB's `/grids/game/{id}` is now paged through with `?page=N`; the picker modal has a "Load more from SteamGridDB" button at the end that replaces itself with the next 20 candidates plus a fresh button.

### Round-2 polish: detail-pane crash, DLC fallback rethink, server-side view-mode ✅ (PR before this)
- **Detail pane was blank** on any entry with non-null `metadata_fetched_at`: SQLite stores DateTime as offset-naive but our `_needs_metadata_refresh` did `datetime.now(UTC) - fetched_at`, which 500s. Helper now treats naive as UTC.
- **DLC vertical covers no longer fall back to parent's portrait art.** Steam often 404s a DLC's `library_600x900.jpg`; the previous fallback chain quietly substituted the base game's cover, so DLC cards visually duplicated the base. Vertical fallback removed (placeholder shows instead — DLC is now distinguishable at a glance); horizontal fallback kept since Steam reliably serves DLC `header.jpg`. Same change applied to library + completions cards.
- **Completion grid date strip removed.** The bottom date overlay made borderless vs non-borderless look inconsistent and was redundant with the list view + detail pane. Cards are now cover-only, matching library.
- **Enrichment status line drops when idle.** When `pending == 0` the partial renders nothing — the auto-refresh-on-detail-pane behavior handles staleness invisibly so there's nothing to surface.
- **Server-side view-mode cookie.** Backend reads `cgt-library-view-mode` / `cgt-completions-view-mode` cookies and renders the right view on first paint. Toggle JS writes the cookie alongside localStorage. Kills the "list flashes for a second, then pops into grid" lag the old JS-only redirect caused.

### Polish bugs + sticky bottom toolbar + stale-only auto-refresh ✅ (PR before this)
- **JS load-order fix.** Toolbar drawer helpers (`cgtToggleDrawer` / `cgtInitDrawer`) now defined inline in `base.html` instead of in `app.js` (which is deferred and wasn't ready when per-page inline scripts ran). View-mode buttons + grid sliders work again.
- **Catppuccin checkbox styling.** Native `accent-color: var(--ctp-mauve)` plus checked/focus overrides so Borderless / Show hidden / form modal checkboxes match the theme.
- **DLC cover fallback chain extended to list/grid.** Server-side batched query attaches parent-game artwork URLs to each DLC entry as `_fallback_v` / `_fallback_h` (no N+1). Templates emit them as `data-fallback`; new `cgtCoverFallback` enhancements handle list-row thumbs and grid card covers separately.
- **Dedicated thumbnail column** in library + completion list views — fixed 80px width so titles don't wrap around the image.
- **Sticky bottom toolbar.** + Add Game and + Log Completion moved out of the top toolbar into a fixed-position bottom bar. Body gets bottom-padding so content scrolls past it. Removes the "jarring top button" and the FAB-overlapping-covers problem in one move.
- **Stale-only auto-refresh on detail-pane open.** Detail endpoints check `_needs_metadata_refresh` (Steam + null-or-7+-days-old). If stale, the rendered partial includes a hidden HTMX trigger that fires the refresh endpoint in the background. Current data shows immediately; the next pane open picks up the refresh.
- Enrichment status messaging on the integrations hub updated to describe the new per-pane auto-refresh behavior instead of a stale "X enriched" count.

### Periodic TTL metadata refresh ✅ (this session)
- `requeue_stale_metadata()` in `steam.py`: when the NULL-queue is drained, re-queues the 50 most-recently-added Steam entries whose `metadata_fetched_at` is older than 30 days, ordered by appid DESC (newest releases first)
- Enrichment worker calls it immediately after draining the queue; sleeps 5 minutes only when both passes return nothing
- Keeps cold entries from going stale indefinitely without hammering Steam on every worker tick

### Add-game modal stability + edit modal unification + detail pane consolidation ✅ (PR #101)
- Add-game modal no longer closes when DLC/collection search results load — fixed with JS `addEventListener` + `event.target === this` to discriminate the form's own HTMX events from bubbled child inputs
- Adding a game reloads the full library view (respecting active filters) via `htmx.ajax` instead of blindly prepending a row
- Collection/DLC parent search inputs write the selected game's label back into the search box so selection is visible
- Edit modal parent selection now uses live HTMX search (matching the add modal) instead of a static `<select>`; pre-fills correctly from `_parent_release_id` / `_parent_label` stamped server-side
- Default view no longer hides Steam games assigned to a collection — removed the `parent_id IS NULL` constraint, keeping only `is_dlc == False`
- Edit moved into the More dropdown on both library and completion detail panes ("Edit game" / "Edit completion") for a consistent single-action-menu; also fixed missing parent data attrs on the detail pane's Edit item

### Steam news / announcements in detail pane (future)
- `ISteamNews/GetNewsForApp` returns dated announcements per appid; cached and shown in the library detail pane as a "Latest news" section
- Each item has a date so we can also show a "this game had news posted since you last viewed it" indicator
- Follow-on to the achievements work; same neighborhood (extra per-game data sources)

### Navbar avatar (future)
- Today the navbar shows the app username (`corrosivefrost`) as plain text; would be nicer with a small profile picture
- Blocked on building an avatar-source picker: which integration wins (Steam / PSN / uploaded), what's the default when no integrations are connected, fallback for users with multiple connected services
- Steam avatar URL is already captured (see "Steam integration polish" above), so once the picker exists, Steam users get a freebie
- Roadmap-only for now

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

### Library cleanup — DLC leak, collection detection, auto-hide overhaul ✅ (PR #92)
- **Default view DLC leak fixed.** Filter changed to `(parent_id IS NULL AND is_dlc = FALSE) OR import_source = 'manual'` — DLC entries with no parent (e.g. free standalone DLC) no longer bleed into the default view.
- **Collection detection replaced with `_COLLECTION_RE` regex.** Word-boundary matching (`\b`); "collection" is end-of-title-anchored so mid-word matches (Recollection, Collection Agency) are avoided. JS-side `COLLECTION_KEYWORDS` list trimmed to match — removed bundle/chronicles/archives/legacy/origins/"collection" (end-of-title can't be checked with `includes()`).
- **`backfill_collection_flags` now corrects false positives.** Sets `is_collection = False` where the current regex disagrees with a previous `True` flag. Respects `is_collection_user_set`. "Re-detect collections" button added to Steam → More sync options.
- **Two-tier `_should_auto_hide`.** Tier 1 gate-free (no `is_dlc` required): music/video/episode type, beta type, demo type + "demo" in title, "beta" anywhere in title. Tier 2 DLC-only gate: cosmetic/pack/pass/bonus content title patterns. Separates type-based hides (always safe) from title-pattern hides (too risky without the DLC gate).
- **HTML entity unescaping.** `_clean_title()` calls `html.unescape()` for display names; Jinja2 `html_unescape` filter added for short descriptions in detail panes.

### Recently played sort + missing artwork filter ✅ (PR #93)
- **Recently played sort.** Library `?sort=recently_played` orders by `last_played_at DESC NULLS LAST` (SQLAlchemy 2.x via `.desc().nulls_last()`). Sort dropdown added to library toolbar alongside existing name sort.
- **Missing artwork filter.** `?missing_art=true` shows only entries with no cover art for the current orientation (checked against UserArtwork and valid GameArtwork). Orientation-aware: grid_v checks vertical, grid_h checks horizontal. Checkbox in library toolbar.

### Artwork framework ✅ (feature/artwork-framework)
- **Two-table design.** `GameArtwork` (auto-sourced, shared across users) and `UserArtwork` (explicit user picks, per entry or per game). Replaces the deprecated `cover_url_override_*` / `hero_url_override` / `logo_url_override` columns on `UserLibraryEntry`.
- **Resolution priority** (implemented in rendering, not data):
  1. `UserArtwork` for this entry → user explicitly chose this
  2. `UserArtwork` for this game → canonical for grouped view
  3. Valid `GameArtwork` for this release, native sources (steam/psn) before sgdb
  4. Valid `GameArtwork` for this game (game-level canonical)
  5. Placeholder
- **`GameArtwork` extended.** `release_id` now nullable (supports game-level rows with no release). Added: `game_id` FK, `is_valid` / `verified_at` for URL health tracking, `sort_order`, `mime_type`, `source_type_raw`, `created_at`. Two scoped unique constraints (release-level + game-level).
- **Artwork type rename.** `'cover'` → `'cover_v'`, `'header'` → `'cover_h'` for consistency. Source `'steamgriddb'` → `'sgdb'`. All code and DB rows migrated.
- **SGDB write paths switched.** Picker and bulk fill now write to `UserArtwork` instead of override columns. Auto-fetch logo/hero likewise.
- **Data migration.** Override column values copied into `UserArtwork` (source='sgdb'). Override columns kept deprecated until rendering is confirmed stable; will be dropped in follow-on migration.
- **Pending follow-on work:**
  - URL verification background job: HEAD-check `GameArtwork` URLs, try alternate Steam patterns, set `is_valid=False` on 404
  - Drop deprecated override columns from `UserLibraryEntry` (after rendering confirmed stable)
  - Game-level artwork lookups (game_id set, release_id null) for the grouped/cross-platform view

---

## Near-term

### IGDB / Twitch integration
- Agreed priority: IGDB → Platforms → PSN → Historical import
- Twitch Client Credentials OAuth (Client ID + Secret stored per-user on the integrations page, same pattern as SGDB)
- Access token fetched and cached (expires hourly); auto-refreshed on use
- `igdb_id` nullable column on `Game`; populated when a manual add is matched to IGDB
- Manual add flow: typeahead search against IGDB `/games` endpoint, user picks a result, title + `igdb_id` auto-filled
- Cover art pulled from IGDB `/covers` → written to `GameArtwork` (source `"igdb"`)
- Platform data from IGDB `/platforms` used to seed the platforms table (see below)
- Enrichment path: background worker can fill `igdb_id` on existing manual entries by title-matching (optional, user-triggered)

### Platforms table (after IGDB)
- `platforms` table: `internal_name`, `display_name` (user-editable), `color_key`, `sort_order`, `is_system`, **`igdb_id`** (nullable int)
- Seeded from IGDB's `/platforms` endpoint so our IDs align with IGDB from day one — no reconciliation needed when IGDB integration lands
- Platform taxonomy is complex (Xbox naming, handheld generations, backward-compat edge cases); IGDB has already solved it, so we don't invent our own
- When this lands: `GameRelease.platform` free-text replaced by `platform_id` FK; `_platform_color_class` regex replaced by a table lookup; manual add modal gets a platform dropdown instead of free text
- All current releases are Steam so the backfill migration is a trivial one-row update

### PSN integration (after platforms)
- PSN OAuth flow: open browser to login URL, user completes login, capture NPSSO token from cookies
- Token stored and refreshed (valid ~6 months); used to pull library and trophy data
- Platforms table must exist first — PSN games need proper platform rows (PS5, PS4, PS3, Vita, etc.)

### Historical import (after PSN)
- Import completions from CSV / Google Sheets: map columns to game title, platform, date completed
- Requires platforms table + IGDB title-matching to resolve old games to proper `igdb_id` and `platform_id`
- Target use case: 2006–2012 era games across PS2, PS3, Xbox 360, etc. that predate any sync integration

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

### Historical import
- Import completions from Google Sheets / CSV
- Map columns to game title, platform, date completed

### Achievements / trophies
- Unified concept across platforms: Steam achievements first, PSN trophies once PSN lands
- New `Achievement` table keyed by `(game_id, source, api_name)` storing name, description, icon URL, hidden flag
- New `UserAchievement` linking user × achievement with unlock timestamp + percent (some platforms expose global unlock rate)
- Steam fetch: `GetSchemaForGame` (per game, once) for the achievement list + icons; `GetPlayerAchievements` (per user × game) for unlock state. Both go through the existing enrichment worker / job system.
- Note: `achievements.total` is already present in the `appdetails` payload we already fetch — but showing a bare "Achievement Count: 27" without earned count is not useful. Display as "X / 27" once player sync exists.
- Detail pane: "Achievements" section showing earned / total + recent unlocks with icons
- Library + completion list/grid: optional badge like "✓ 100%" or "23/47"
- Filter / sort by achievement progress (e.g. "show games close to 100%")
- Phases TBD — at minimum: schema + Steam fetch, then UI surfaces, then PSN trophy mapping when PSN integration exists

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
