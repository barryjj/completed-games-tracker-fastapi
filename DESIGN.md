# DESIGN.md

Design reference for the completed-games tracker. Keep this current as patterns evolve.
Read this before touching any template, CSS, or JS.

---

## Theme

Three palettes, all defined in `frontend/static/css/theme.css`:

| Palette | Mode | Role |
|---------|------|------|
| **Nord** | light | Default light theme |
| **Catppuccin Latte** | light | Opt-in alternative |
| **Catppuccin Mocha** | dark | Default dark theme |

Two attributes drive them, and the split matters:

- `data-bs-theme` is only ever `light` or `dark` — it is Bootstrap's own
  attribute and Bootstrap does not understand any other value.
- `data-palette` is `nord` | `latte` | `mocha` and selects the palette.

So Latte is `data-bs-theme="light" data-palette="latte"`, and every existing
`html[data-bs-theme="light"] …` component rule keeps applying to both light
palettes without being duplicated. **Scope new light-mode rules to
`data-bs-theme`, not `data-palette`,** unless the rule is genuinely
palette-specific.

Palette-specific literals belong in the palette block as `--cgt-*` variables
(see `--cgt-btn-primary`, `--cgt-stat-stroke`) — never hardcoded into a
component rule, or the third palette silently inherits the wrong colour.

The boot script in `base.html` / `login.html` / `signup.html` resolves the
palette before first paint and migrates pre-Nord `light`/`dark` values stored
in `localStorage`. Absent a stored value, `prefers-color-scheme` picks Nord or
Mocha.

**Rule: no emoji in UI chrome.** Badge labels, headings, button text — plain text only.

---

## Navigation & Page Layout

Navbar: **Home · Library · Completions · Tools** on the left; **gear (Settings) + user dropdown** on the right.

- **Home** (`/`) — landing page after login. Static widget grid (Home v1) reusing the
  tool-card + stat-row components: completions this year vs. goal (flamingo accent) with a
  12-month bar strip (`.cgt-month-bars`, single hue, tooltips carry exact counts), library
  platform breakdown (badge + count rows linking to the filtered library), recently
  completed list with cover thumbnails (rows deep-link to `/completions?detail=N`),
  needs-attention counts. Widget pin/customize is restructure phase 3.

- **Tools** (`/tools`) — recurring operations as `.cgt-tool-card` cards: Steam sync, match
  review, spreadsheet import, artwork. The yellow pending-matches badge lives on the Tools
  nav link. Anything the user *does* on a schedule belongs here.
- **Settings** (`/settings`) — gear icon. Grouped left nav (`.cgt-settings-nav`, NOT tabs):
  *Account* (Profile / Security / Appearance) + *Configuration* (Platforms, Integrations).
  Section switching is client-side with `?section=` URL persistence. The Integrations
  section shows `.cgt-service-row` rows (status + Configure) — configuration only; actions
  live on Tools. Per-service configure pages stay at `/integrations/steam` etc. and light
  up the gear in the navbar.
- Old URLs redirect: `/account[?tab=X]` → `/settings[?section=X]`, `/integrations` → `/tools`.
- Page headings: `<h4>` + small muted subtitle (see `/tools`, Import Review). Library and
  Completions deliberately have no heading — the nav underline is the page indicator.

---

## Catppuccin CSS Variables

These are the vars actually used in templates and CSS. All resolve in all three
palettes. The table lists Mocha and Latte; **Nord** supplies the same variable
names — nine accents are published Nord values and five (`maroon`, `flamingo`,
`rosewater`, `lavender`, `pink`) are mixes of two adjacent Nord colours, with
the ratio recorded inline in `theme.css` so they can be re-derived rather than
guessed at.

| Var | Mocha | Latte | Role |
|-----|-------|-------|------|
| `--ctp-base` | `#1e1e2e` | `#eff1f5` | Page background |
| `--ctp-mantle` | `#181825` | `#e6e9ef` | Sidebar / offcanvas bg |
| `--ctp-surface0` | `#313244` | `#ccd0da` | Card bg, input bg |
| `--ctp-surface1` | `#45475a` | `#bcc0cc` | Hover / raised surfaces, IGDB linked card bg |
| `--ctp-overlay0` | `#6c7086` | `#9ca0b0` | Placeholder text |
| `--ctp-overlay1` | `#7f849c` | `#8c8fa1` | Detail-pane meta labels (`dt`), light-mode outline-button borders |
| `--ctp-text` | `#cdd6f4` | `#4c4f69` | Body text |
| `--ctp-subtext` | `#a6adc8` | `#5c5f77` | Secondary / muted text |
| `--ctp-subtext0` | `#a6adc8` | `#6c6f85` | Unmatched platform badge, match-review side labels |
| `--ctp-subtext1` | `#bac2de` | `#5c5f77` | Detail-pane description text, match-result strip |
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

Platforms are stored in the `platforms` table. Each row has a `color` field (Catppuccin accent key)
that drives the badge CSS class. Use `release.platform_tag_class` for the CSS class and
`release.display_platform` for the label text — these properties handle linked/unlinked cases.

Usage: `<span class="tag-badge {{ release.platform_tag_class }}">{{ release.display_platform }}</span>`

CSS classes are `tag-platform-{accent}` where accent is any of the 14 Catppuccin accent names:

| Accent | Default group |
|--------|--------------|
| `red` | Nintendo (all: NES/SNES/N64/GameCube/Wii/Wii U/Switch/Switch 2/handhelds/add-ons) |
| `lavender` | PlayStation (all: PS1–5/PSP/Vita/PSVR/PSVR2) |
| `green` | Xbox (all), Android |
| `yellow` | Sega (all: Genesis/Saturn/Dreamcast/Game Gear/Master System/Sega CD/32X/SG-1000) |
| `peach` | Atari (all), 3DO, Evercade |
| `sapphire` | PC (Windows), Linux, DOS, Amiga |
| `sky` | Mac, iOS, Web browser |
| `teal` | Steam |
| `mauve` | TurboGrafx / NEC PC Engine family |
| `maroon` | Neo Geo family |
| `blue` | Meta Quest / Oculus VR headsets |
| `flamingo` | Arcade |
| `pink` | WonderSwan family |
| `rosewater` | Playdate |
| `other` | Unmatched string fallback (grey, uses `--ctp-subtext0`) |

The `platform_color` Jinja filter still exists for legacy uses — it now accepts either a
`Platform` model instance or a raw string, falling back to the string heuristic.

**Legacy semantic classes** (`tag-platform-steam`, `tag-platform-nintendo`, etc.) are kept for
unlinked entries using the string heuristic. New code should use the accent-key classes.

Other badge classes: `tag-dlc`, `tag-collection`, `tag-in-collection`.

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

### Tool card (Tools page)
```html
<div class="cgt-tool-card">
  <div class="cgt-tool-card__title">Steam sync <span class="badge bg-success">Connected</span></div>
  <div class="small text-secondary">Status line…</div>
  <div class="cgt-tool-card__actions">
    <button class="btn btn-primary btn-sm" hx-post="…" hx-swap="none" hx-disabled-elt="this">Sync</button>
    <a href="…" class="btn btn-surface btn-sm">Configure</a>
  </div>
</div>
```
Title strip is uppercase small-caps with badges inline; actions row pins to the card
bottom via `margin-top: auto`. Action buttons use `hx-swap="none"` — results arrive as
OOB toasts. Muted placeholder cards add `.cgt-tool-card--muted`.

### Tool-card stat row (big labeled numbers)
```html
<div class="cgt-tool-stats">
  <div class="cgt-tool-stat cgt-tool-stat--teal">
    <div class="cgt-tool-stat__value">9,945</div>
    <div class="cgt-tool-stat__label">Games</div>
  </div>
</div>
```
All body text in a tool card — blurbs, the account identity block, breakdown
rows — uses `.cgt-tool-card__body`, which declares the size in `rem`. Do not
reach for Bootstrap's `.small` inside a card: three separate opt-ins is how the
sizes drifted apart in the first place, and `.small` is `em`-based so it
compounds when nested.

A connected integration card drops its explanatory blurb — the identity block
and the counts say the state, and instructions for a job already done are
noise. Keep the copy in the disconnected branch only.

Values are the only colored text; labels stay muted ink (identity is never color-alone).
**Accent meanings are semantic — reuse them, don't invent new assignments:**

| Accent | Meaning |
|--------|---------|
| `teal` | Steam games |
| `peach` | DLC (matches `tag-dlc`) |
| `lavender` | totals |
| `yellow` | pending / needs attention |
| `green` | matched / certain |
| `blue` | new / informational |
| `pink` | needs manual input (confidence-coding roadmap item) |
| `maroon` | missing / gap |
| `--muted` modifier | zero / nothing to do |

Side-by-side triplets (teal/peach/lavender, green/blue/pink) are validated for
colorblind separation.

Values render in the palette's **published** accent, and carry a hairline
outline (`paint-order: stroke fill` + `-webkit-text-stroke` with
`--cgt-stat-stroke`) so pale accents keep a defined edge on light surfaces.
Light mode used to mix every accent 65% toward `--ctp-text`; that stripped an
average 42% of their saturation and did it unevenly (teal lost 29%, pink 55%),
which broke the very triplet separation above. **Don't reintroduce a
lightness/ink mix on stat values** — the stroke is what solves legibility, and
it leaves the hue intact. The tool grid uses `grid-auto-rows: 1fr` so all cards in
a row share the tallest card's height — uniform tiles are structural, not dependent on
text happening to wrap evenly.

### Settings left-nav item
```html
<div class="cgt-settings-nav__group">Configuration</div>
<a class="cgt-settings-nav__item" data-section="platforms" href="?section=platforms">Platforms</a>
```
`.active` gets mauve text + tinted bg + 2px left accent. Sections are `.settings-section`
divs toggled by JS; the content column flips `.cgt-settings-content--wide` for the
platforms table.

### Integration service row (Settings → Integrations)
```html
<div class="cgt-service-row">
  <div class="cgt-service-row__body">
    <div class="d-flex align-items-center gap-2">
      <span class="fw-semibold small">Steam</span> <span class="badge bg-success">Connected</span>
    </div>
    <div class="small text-secondary">status one-liner</div>
  </div>
  <a href="/integrations/steam" class="btn btn-surface btn-sm">Configure</a>
</div>
```

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

`_grid_cover_url(entry, orientation)` in `backend/pages_common.py` resolves the URL.
Fallback chain: UserArtwork → GameArtwork native (steam/psn) → GameArtwork sgdb.
**No cross-orientation fallback** — stretched art looks worse than a placeholder.
IGDB/manual entries without `cover_h` should use the SGDB picker to find one.

### Filter memory is opt-in, and lives in cookies

Every filtered page — Library, Completions, Import review, PSN review — carries
a **"Remember filters"** checkbox. Unticked (the default) nothing persists;
ticked, the current values are stored and re-stored on each change; unticking
deletes them.

**Cookies, not localStorage.** The server reads these to bind filters into the
FIRST render, so the list paints already filtered. localStorage was tried for
this in PR #123 and abandoned: the server can't read it, so the page renders
unfiltered and JS re-applies afterwards — a visible flash plus a wasted
round-trip. Grid size/gap/borderless *may* use localStorage because they are
pure CSS and never change what the server renders.

Keys are `cgt-<page>-remember` and `cgt-<page>-<filter>`; Import review adds a
tab segment (`cgt-import-<tab>-<filter>`) because each tab has its own candidate
pool. An explicit query param always beats a stored value. `q` is never
persisted on any page — a remembered search string is surprising in a way a
remembered platform or sort is not.

### Long-list pages: actions go in the sticky footer

Library, Completions, Import review and PSN review all use
`.cgt-sticky-actions` — a `position: fixed; bottom: 0` bar. **Bulk actions and
the back-to-top button belong inside it**, not in a static div above the table.

PSN review originally had its bulk Confirm/Dismiss above the list: with a long
queue you tick rows, scroll down to tick more, and the buttons are off screen
above you — with no way back up, since back-to-top also lives in that bar. If a
page can grow past one screen, its actions go in the footer.

`body:has(.cgt-sticky-actions)` already adds the bottom padding so the last row
isn't hidden behind it.

### Grid view state (size / gap / borderless)

All three persist in `localStorage` and **all three apply to
`document.documentElement`** — CSS vars for size and gap, a
`cgt-grid-borderless` class for borderless.

**Never attach grid view state to an element inside the swapped content
region.** The grid lives in `#library-content` / `#completions-content`, which
HTMX replaces on every search, filter, sort and page. Borderless originally set
a class on `.cgt-library-grid` and was silently wiped by every swap, while the
checkbox — outside that region — stayed checked. The toggle read as on while
being off, and only worked if you clicked it twice. Size and gap never had the
bug purely because they were on `documentElement` from the start.

Apply the saved value unconditionally on load (`applyBorderless(saved)`), not
just when it's on, so an off state actually clears.

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

- **Self-refetching wrappers (`hx-get` self + `hx-select="#own-id"`) MUST carry
  `hx-disinherit="hx-select"`.** `hx-select` is inherited: without it, any child that makes its
  own request (a `hx-trigger="load"` status div, an action button) filters *its* response
  through the parent's selector, matches nothing, and swaps in nothing — the element is
  silently deleted with no error anywhere. This ate the PSN snapshot report and silently
  suppressed the Tools enrichment status before being found (2026-07-18).
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
Find vertical/horizontal cover / hero / logo (SGDB, if key set) → Logo position (when a
hero + logo exist) → Reset overrides → Delete

### Hero logo position
`entry.logo_position` (nullable) anchors the logo overlay: `top-left` / `top-center` /
`top-right` / `center` / `bottom-center` / `bottom-right` / `hidden`; NULL = bottom-left
default. `entry.logo_scale` sizes it: `small` / `large` / `xlarge`; NULL = default
(38% x 50% box). CSS modifiers `cgt-detail-hero__logo--{anchor}` and `--scale-{size}`.
Applies on all five hero+logo surfaces (library pane, completion pane, hero-block refresh
partial, import-review cards, match-review preview); `hidden` also suppresses the SGDB
logo auto-fetch. Set via `POST /library/entries/{id}/logo-position` / `/logo-scale`
(allowlisted values), surfaced as hover fly-out submenus in both panes' More dropdowns.
