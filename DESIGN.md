# DESIGN.md

Design reference for the completed-games tracker. Keep this current as patterns evolve.
Read this before touching any template, CSS, or JS.

---

## Theme

**Catppuccin Mocha** (dark, default) / **Catppuccin Latte** (light).
Switched via `prefers-color-scheme` media query; user can override with a localStorage toggle.
Both palettes are defined in `frontend/static/css/theme.css`.

**Rule: no emoji in UI chrome.** Badge labels, headings, button text — plain text only.

---

## Catppuccin CSS Variables

These are the vars actually used in templates and CSS. All resolve correctly in both Mocha and Latte.

| Var | Mocha | Latte | Role |
|-----|-------|-------|------|
| `--ctp-base` | `#1e1e2e` | `#eff1f5` | Page background |
| `--ctp-mantle` | `#181825` | `#e6e9ef` | Sidebar / offcanvas bg |
| `--ctp-surface0` | `#313244` | `#ccd0da` | Card bg, input bg |
| `--ctp-surface1` | `#45475a` | `#bcc0cc` | Hover / raised surfaces, IGDB linked card bg |
| `--ctp-overlay0` | `#6c7086` | `#9ca0b0` | Placeholder text |
| `--ctp-text` | `#cdd6f4` | `#4c4f69` | Body text |
| `--ctp-subtext` | `#a6adc8` | `#5c5f77` | Secondary / muted text |
| `--ctp-mauve` | `#cba6f7` | `#8839ef` | Primary accent (buttons, active tabs, GIF badge) |
| `--ctp-lavender` | `#b4befe` | `#7287fd` | Hover on primary, range thumb hover |
| `--ctp-blue` | `#89b4fa` | `#1e66f5` | Steam platform badge |
| `--ctp-green` | `#a6e3a1` | `#40a02b` | Success / confirmed link (IGDB linked card border + checkmark, Xbox badge) |
| `--ctp-teal` | `#94e2d5` | `#179299` | Metacritic "Fair" score |
| `--ctp-sky` | `#89dceb` | `#04a5e5` | Apple platform badge |
| `--ctp-sapphire` | `#74c7ec` | `#209fb5` | PC platform badge |
| `--ctp-peach` | `#fab387` | `#fe640b` | Metacritic "Weak" score |
| `--ctp-yellow` | `#f9e2af` | `#df8e1d` | Metacritic "Mixed" score |
| `--ctp-red` | `#f38ba8` | `#d20f39` | Danger buttons, Nintendo badge, Metacritic "Bad" |
| `--ctp-crust` | `#11111b` | `#dce0e8` | Button text on colored backgrounds |

---

## Platform Badge Colors

Defined by `_platform_color_class()` in `backend/pages.py` and rendered via the `platform_color` Jinja2 filter.
Usage: `<span class="tag-badge {{ release.platform | platform_color }}">{{ release.platform }}</span>`

| Class | Color var | Matches |
|-------|-----------|---------|
| `tag-platform-steam` | `--ctp-blue` | "steam" |
| `tag-platform-playstation` | `--ctp-lavender` | "ps", "playstation", "psn" |
| `tag-platform-nintendo` | `--ctp-red` | "switch", "nintendo" |
| `tag-platform-xbox` | `--ctp-green` | "xbox" |
| `tag-platform-apple` | `--ctp-sky` | "ios", "mac", "apple", "iphone", "ipad" |
| `tag-platform-pc` | `--ctp-sapphire` | "pc", "windows" |
| `tag-platform-other` | `--ctp-subtext0` | everything else (NES, PS2, Game Boy, etc.) |

Other badge classes: `tag-dlc`, `tag-collection`, `tag-in-collection`.

**Note:** The `tag-platform-other` bucket is where all retro/manual platforms land today.
A platform table (with per-platform color assignment) is the next major feature — see ROADMAP.md.

---

## Component Patterns

### Tag badge
```html
<span class="tag-badge tag-platform-steam">Steam</span>
<span class="tag-badge tag-dlc">DLC</span>
```

### Detail pane block (tinted section with heading)
```html
<div class="cgt-detail-block mb-3">
  <h6 class="cgt-detail-block__heading">Section title</h6>
  <!-- content -->
</div>
```

### Detail pane metadata grid (label: value rows)
```html
<dl class="cgt-detail-meta small mb-0">
  <dt>Released</dt>
  <dd>2016</dd>
</dl>
```

### Hero + logo overlay (top of detail pane)
```html
<div class="cgt-detail-hero">
  <img class="cgt-detail-hero__img" src="..." alt="" loading="lazy"
       data-fallback="..." onerror="cgtHeroFailed(this)">
  <img class="cgt-detail-hero__logo" src="..." alt="" loading="lazy"
       data-fallback="..." onerror="this.style.display='none'; cgtHeroBlockCheck(this);">
</div>
```
The hero has a gradient overlay (`cgt-detail-hero::after`) so the logo reads on any background.

### IGDB linked card (green confirmation, used in both modals)
```html
<div id="*-igdb-linked-card" class="d-none rounded p-2 mb-2"
     style="border-left:3px solid var(--ctp-green); background:var(--ctp-surface1);">
  <div class="d-flex align-items-center gap-2">
    <span style="color:var(--ctp-green); line-height:1;">&#10003;</span>
    <span id="*-igdb-linked-name" class="fw-medium small"></span>
    <span id="*-igdb-linked-id" class="text-secondary small ms-1"></span>
    <button type="button" class="btn btn-link btn-sm p-0 ms-auto text-secondary"
            onclick="clearXxxIgdbSelection()">Change</button>
  </div>
</div>
```
JS shows/hides via `card.classList.remove('d-none'); card.style.display='flex'` and reverse.

### IGDB tab section (Search | By ID)
Bootstrap `nav nav-tabs` — NOT pills, NOT custom buttons. Active tab has class `active`.
Tab panel has `class="border border-top-0 rounded-bottom p-2"`.
```html
<ul class="nav nav-tabs mb-0" style="font-size:.85rem;">
  <li class="nav-item">
    <button type="button" class="nav-link active px-3 py-1" id="*-tab-text"
            onclick="switchXxxIgdbTab('text')">Text</button>
  </li>
  <li class="nav-item">
    <button type="button" class="nav-link px-3 py-1" id="*-tab-id"
            onclick="switchXxxIgdbTab('id')">By ID</button>
  </li>
</ul>
<div class="border border-top-0 rounded-bottom p-2">
  <!-- panes -->
</div>
```

### Platform chips (from IGDB game selection)
Appear below the platform input after selecting an IGDB game.
Single platform → auto-fill the input, no chips.
Multiple → clickable `btn btn-sm btn-outline-secondary rounded-pill` chips.
Selected chip gets `btn-primary` / loses `btn-outline-secondary`.

### Metacritic score chip
```html
<span class="cgt-score-chip" style="background-color: {{ color }};">{{ score }}</span>
```
Color thresholds: ≥85 green, ≥70 teal, ≥50 yellow, ≥20 peach, else red.

---

## Library Grid / List Views

| Mode | CSS class | Cover type | Aspect ratio |
|------|-----------|-----------|--------------|
| `grid_v` | `cgt-library-grid--grid_v` | `cover_v` | 600×900 (portrait) |
| `grid_h` | `cgt-library-grid--grid_h` | `cover_h` | 460×215 (landscape) |
| list | table rows | `cover_h` thumbnail | fixed height |

`_grid_cover_url(entry, orientation)` in `backend/pages.py` resolves the URL.
Fallback chain: UserArtwork → GameArtwork native (steam/psn) → GameArtwork sgdb.
**No cross-orientation fallback** — stretched art looks worse than a placeholder.
IGDB/manual entries without `cover_h` should use the SGDB picker to find one.

---

## Artwork System

### Types
| `artwork_type` | Shape | Sources |
|---------------|-------|---------|
| `cover_v` | Portrait 600×900 | Steam CDN, IGDB (`t_cover_big`), SGDB |
| `cover_h` | Landscape 460×215 | Steam CDN, SGDB |
| `hero` | Wide ~1920×620 | Steam CDN, IGDB (`t_1080p`), SGDB |
| `logo` | Transparent PNG | Steam CDN, SGDB (can be animated WebP/GIF) |

### Sources
| `source` | Description |
|---------|-------------|
| `steam` / `psn` | Native platform CDN — highest priority in GameArtwork |
| `igdb` | Fetched from IGDB API (cover_v and hero only) |
| `sgdb` | SteamGridDB — all types including animated |
| `user_url` / `user_upload` | Direct user input |

### Priority (highest → lowest)
1. `UserArtwork` for the entry (explicit SGDB picker pick or user upload)
2. `GameArtwork` from steam/psn (authoritative native art)
3. `GameArtwork` from sgdb/igdb (auto-fetched fallback)

### Animated art
SGDB returns animated WebP/GIF for logos, heroes, and covers.
We pass `types=static,animated` to all three SGDB endpoints.
Animated results carry `c.animated == true` in the API response — picker shows a GIF badge.
Browsers render animated WebP/GIF natively in `<img>` tags.

---

## IGDB Integration

- Auth: Twitch Client Credentials (stored as `twitch_client_id` / `twitch_client_secret` on User)
- Token cached in-process, refreshed when expired
- `version_parent = null` filter in search — excludes regional/hardware variants
- Direct ID lookup (`GET /integrations/igdb/game/{id}`) is the escape hatch for filtered-out games
- `fetch_game_brief()` → `{id, name, cover_url, platforms, year}` (same shape as search results)
- `fetch_game_details()` → summary, genres, year, artwork_urls (landscape)
- On link: saves `cover_v` to `GameArtwork` + summary/genres/year to `release.raw_data['igdb']`
- On unlink: clears `game.igdb_id`, `release.raw_data['igdb']`, `release.description`, marks IGDB `GameArtwork` invalid

---

## HTMX Conventions

- Use `hx-swap="none"` for actions that don't need to replace DOM (metadata refresh, delete with reload)
- Use `hx-on::after-request` to trigger follow-up fetches after successful mutations
- IGDB search and SGDB picker use plain `fetch()` — not HTMX — to avoid HTMX interference inside `hx-post` forms
- `hx-disabled-elt="this"` on "Load more" buttons prevents double-clicks
- `HX-Request: true` header required in tests to skip modal dropdown population

---

## Modal Conventions

### Add game modal (`#addGameModal`)
- Title field doubles as IGDB Text-tab search input (typeahead fires on `input` event)
- Tabs: **Text** | **By ID** — Bootstrap `nav nav-tabs`
- IGDB confirmation card collapses the tab section; **Change** restores it
- Platform chips appear after IGDB game selection; clicking a chip fills the platform input

### Edit game modal (`#editEntryModal`)
- IGDB section only shown for manual entries (`import_source == "manual"`)
- Platform field editable for manual entries only
- IGDB section sits between Display name and DLC/Collection checkboxes
- `editLibraryEntry()` populates all fields including `data-igdb-id` and `data-platform` from the edit button

---

## Detail Pane

Rendered into `#library-detail-content` (library) or `#completion-detail-content` (completions).
Partials: `partials/library_detail.html`, `partials/completion_detail.html`.

Structure (top to bottom):
1. Optional `hx-trigger="load"` metadata refresh trigger (hidden div)
2. `.cgt-pane-nav` — back-arrow injected by app.js when pane stack > 1
3. `.cgt-detail-hero` — hero image + logo overlay (omitted if neither exists)
4. `.cgt-detail-body`:
   - Platform / DLC / Collection / Hidden badges
   - Title block with optional parent breadcrumb (clickable if user owns parent)
   - Metadata `<dl>` (playtime, Steam ID, release date, developer, genre, Metacritic, etc.)
   - Description block (Steam short_description preferred; falls back to IGDB summary)
   - Completions list (library pane) or "Other completions" list (completion pane)
   - DLC / Games-in-collection child table (scrollable, thumbnails via `grid_cover_url('grid_h')`)
5. `.cgt-detail-actions` — pinned footer with "View in library" / "More" dropdown

### More dropdown items (in order)
Edit game → Refresh game metadata (Steam only) → Refresh IGDB metadata → Unlink IGDB →
Find vertical/horizontal cover / hero / logo (SGDB, if key set) → Reset overrides → Delete
