# Tauri desktop shell — dev shell + Steam/PSN cookie capture

## Context

The agreed roadmap sequence is **Tauri → PSN → achievements**. Tauri gates the sign-in story
for both platform integrations: a WebView we control can read Steam's session cookies
(`steamLoginSecure`, `sessionid`) and PSN's NPSSO token at login time, replacing manual
DevTools cookie-pasting. Packaging (PyInstaller sidecar, bundled .app, installers) is
**explicitly deferred** — this phase assumes the repo checkout + `.venv` exist on the machine,
and the Tauri app launches the backend from them. User-confirmed staging:

- **Step 1** — Tauri shell replaces browsing to `localhost:8000`: app launch starts the Python
  backend and opens the UI in a window.
- **Step 2** — Steam: capture `sessionid` + `steamLoginSecure` from a WebView login; when stored
  values go stale (sync fails), offer re-capture.
- **Step 3** — PSN framework: settings page → login window → NPSSO capture + storage, plus a
  cheap validity test. Real PSN library/trophy queries remain in the later PSN phase.

Copy this plan into `docs/tauri-desktop-plan.md` on the first branch so it survives model
handoff, and update `ROADMAP.md` (mark the Tauri item in-progress, record the three steps and
the packaging deferral) per the roadmap-maintenance rule.

## Current state (exploration findings — verified 2026-07-15)

- **Credentials** are plaintext columns on `users` (`backend/models.py:119-130`):
  `steam_id64`, `steam_api_key`, `steam_session_id` (= cookie `sessionid`),
  `steam_login_secure` (= cookie `steamLoginSecure`). No encryption anywhere.
- **Manual paste endpoint**: `POST /integrations/steam/credentials` →
  `save_steam_credentials` (`backend/integrations.py:52-76`), form fields `steam_api_key`,
  `steam_session_id`, `steam_login_secure`. This is the drop-in point — captured cookies can
  be posted to it unchanged.
- **What the cookies do**: exactly one authenticated call —
  `https://store.steampowered.com/dynamicstore/userdata/` (`backend/steam.py:579-604`,
  `_fetch_owned_appids`). ⇒ **capture must target `store.steampowered.com`**, not
  steamcommunity.com (`steamLoginSecure` is per-domain).
- **Expiry detection**: stale cookies return HTTP 200 with empty `rgOwnedApps`;
  `steam.py:600-603` raises `ValueError("Steam session cookies have expired…")`, surfaced via
  the in-memory job registry (`integrations.py:353-354` → `jobs.mark_failed`) and the job
  poller toast. Manual check button: `POST /integrations/steam/test-cookies`
  (`integrations.py:208-247`).
- **Steam OpenID** sign-in already works pure-web (`integrations.py:110-182`) and stores only
  `steam_id64` + persona/avatar. Unchanged by this work.
- **Auth**: page routes require the `session` cookie (value = user's `api_token`,
  `pages_common.py:526-534`); the Tauri WebView authenticates like any browser and WKWebView's
  default store persists it across launches. No CORS middleware; no hardcoded host/port.
- **DB path gotcha**: `DB_URL` default is relative — `sqlite:///backend/app.db`
  (`backend/models.py:92`), cwd-dependent. The spawned backend must run with
  **cwd = repo root** to hit the same DB as today.
- **Startup**: `lifespan` (`backend/main.py:112-126`) auto-runs Alembic to head and spawns two
  asyncio workers (skipped when `TESTING` is set). Do not set `TESTING` in the spawn env.
- **PSN**: zero backend surface. Only forward-compat enum strings, "Coming soon" tiles
  (`settings.html:266-272`, `tools.html:189-196`), and roadmap text. `npsso` appears nowhere.
- **Frontend**: manual-paste UI in `frontend/templates/integrations_steam.html` (form at line
  54, cookie inputs at 78-91, sync buttons gated on cookie presence at 141/196/285). No Tauri
  detection exists yet (`app.js` has none). Vendored JS only — no Node project in the repo.
- **Toolchain**: Node v20 + Xcode present. **Rust missing** (user installs rustup themselves).
  No PyInstaller (not needed this phase).

## Design overview

- **`desktop/`** (new top-level dir, approved): a Tauri **v2** app, minimum `tauri = "2.4"`
  (first release with `WebviewWindow::cookies_for_url()`, which returns HttpOnly + Secure
  cookies — `document.cookie` cannot see `steamLoginSecure` or `npsso`).
- **No bundled frontend.** The main window's URL is `http://127.0.0.1:8000` — the FastAPI
  app serves the existing Jinja/HTMX UI. No JS framework, no build step for UI assets.
- **Backend lifecycle** (Rust `setup` hook):
  1. GET `http://127.0.0.1:8000/health`; if it answers, an external dev server is running —
     don't spawn, just open the window (keeps `uvicorn --reload` workflows working).
  2. Otherwise spawn `<repo>/.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1
     --port 8000` with cwd = repo root; poll `/health` until ready; open the window.
  3. On app exit, kill the child **only if we spawned it**.
  - Repo root resolution: `GAMES_TRACKER_ROOT` env var if set, else a compile-time default
    (`env!("CARGO_MANIFEST_DIR")/../..`) — fine for a dev shell built from this repo.
- **IPC from the served page**: `app.withGlobalTauri = true` in `tauri.conf.json` +a
  capability file granting the remote origin `http://127.0.0.1:8000` access to only our
  custom commands (`capabilities/main.json` with `"remote": {"urls": ["http://127.0.0.1:8000"]}`,
  plus `core:default` for the main window). Page JS feature-detects `window.__TAURI__`.
- **Capture pattern (both platforms)**: page JS invokes a Rust command → Rust opens a login
  `WebviewWindow` → polls `cookies_for_url(target)` every ~1.5 s (async — the API deadlocks in
  sync commands on Windows; keep it async from day one) until the marker cookie appears or the
  user closes the window → resolves the invoke promise with the cookie values → **page JS
  posts them to the existing backend endpoints** (so auth rides the page's session cookie and
  the existing flash/refresh UX is reused; Rust never needs to authenticate).
  All webviews share the app's cookie store, so a still-valid Steam/Sony login means
  re-capture completes without retyping credentials.

## Step 1 — Desktop shell (PR 1: `feature/tauri-shell`)

New files (all under `desktop/`):
- `desktop/package.json` — sole dep `@tauri-apps/cli` (dev), scripts `dev` / `build`.
  (npm CLI = prebuilt binary; avoids a 10-min `cargo install tauri-cli` compile.)
- `desktop/src-tauri/Cargo.toml` — `tauri = { version = "2.4", features = [] }`, `reqwest`
  (health polling) or plain `std::net` + tiny HTTP GET via `tauri`'s http… simplest:
  `ureq` (tiny, blocking, fine in setup thread). Keep deps minimal.
- `desktop/src-tauri/tauri.conf.json` — window url `http://127.0.0.1:8000`,
  `withGlobalTauri: true`, identifier e.g. `dev.frost.gamestracker`, `devtools` feature on.
- `desktop/src-tauri/src/main.rs` + `lib.rs` — backend spawn/health/kill logic per design.
- `desktop/src-tauri/capabilities/main.json` — remote-origin capability (used from Step 2 on;
  create it now so the wiring is proven).
- `desktop/.gitignore` — `node_modules/`, `src-tauri/target/`.
- `docs/tauri-desktop-plan.md` — this plan. `ROADMAP.md` update in the same PR.

Deliverable: `npm run dev` (and a `tauri build`-produced .app that does the same) opens a
window showing the login page / library, backend spawned automatically, no terminal needed.

## Step 2 — Steam cookie capture (PR 2: `feature/tauri-steam-capture`)

- **Rust** `#[tauri::command] async fn capture_steam_login(app: AppHandle) -> Result<SteamCookies, String>`:
  - Open `WebviewWindow` at `https://store.steampowered.com/login/` (label `steam-login`,
    ~900×760, guard against a second concurrent capture window).
  - Poll `cookies_for_url("https://store.steampowered.com")` until `steamLoginSecure` exists
    (it only exists when logged in; `sessionid` exists even anonymous). Return
    `{ sessionid, steam_login_secure }`, close the window. If the user closes the window
    first, resolve with a "cancelled" error.
- **Frontend** (`integrations_steam.html` + small addition to `app.js`):
  - A "Capture from Steam sign-in" button in the credentials card, hidden unless
    `window.__TAURI__` is present (JS toggles a `data-tauri-only` attribute/class).
  - Click → `window.__TAURI__.core.invoke('capture_steam_login')` → on success, fill the two
    existing form inputs and submit the **existing** form (`hx-post
    /integrations/steam/credentials`) so save + flash + refresh behave exactly like manual
    paste. Manual fields stay as the web fallback.
- **Stale-cookie re-capture** (user's step 2 requirement): the expiry path already lands as a
  failed-job toast and the test-cookies flash. In Tauri mode, show the same capture button as
  the remedy: add a short "Session expired? Re-capture" affordance next to the test-cookies
  button, and (cheap win) have the job-error toast link to `/integrations/steam`. Because the
  WebView's Steam login usually outlives the captured cookies, re-capture is typically
  zero-typing: window flashes open, cookies refresh, done.
- **No backend changes required** in this PR (endpoint already exists).

## Step 3 — PSN capture framework (PR 3: `feature/psn-npsso-capture`)

- **Model + migration**: add to `User` (`backend/models.py`): `psn_npsso` (String, nullable),
  `psn_npsso_captured_at` (DateTime, nullable), `psn_online_id` (String, nullable — filled by
  the test call when available). Alembic revision; plaintext like the Steam columns (matching
  existing pattern; encryption is a separate roadmap concern).
- **Backend** (`backend/integrations.py`, same router):
  - `GET /integrations/psn` — settings page, template `integrations_psn.html` modeled on
    `integrations_steam.html`: status card (captured date / not configured), manual NPSSO
    paste field as web fallback (mirrors psn-api's documented manual flow: log in at
    playstation.com, visit `https://ca.account.sony.com/api/v1/ssocookie`, copy the JSON
    value), clear button, Tauri-only capture button.
  - `POST /integrations/psn/credentials` — form field `psn_npsso`; save/clear, flash partial,
    `HX-Refresh` — clone of `save_steam_credentials`.
  - `POST /integrations/psn/test-token` — mirrors `test-cookies`: exchange NPSSO for an OAuth
    access code (`GET https://ca.account.sony.com/api/authz/v3/oauth/authorize` with the
    `npsso` cookie, the documented psnawp/psn-api first hop, using the public client id those
    libraries use); flash valid/expired. Store nothing beyond `psn_online_id` if trivially
    available; the full token exchange/refresh machinery belongs to the PSN integration phase.
  - Wire the settings/tools "Coming soon" PSN tiles (`settings.html:266`, `tools.html:189`) to
    link to `/integrations/psn`.
- **Rust** `capture_psn_login`: open window at `https://my.playstation.com/` (redirects into
  Sony SSO login). Poll `cookies_for_url("https://ca.account.sony.com")` for the `npsso`
  cookie; if login completes but the cookie hasn't materialized, navigate the window to
  `https://ca.account.sony.com/api/v1/ssocookie` once and keep polling. Return `{ npsso }`;
  page JS fills + submits the credentials form, same pattern as Steam.
- **Tests** (`backend/test_integrations.py` additions): PSN page renders for logged-in user;
  credentials POST saves/clears `psn_npsso` (+ sets/clears `captured_at`); test-token endpoint
  mocked via `httpx` monkeypatch for valid/invalid paths. Use `headers={"HX-Request": "true"}`
  where library-adjacent (per CLAUDE.md test-isolation rule).

## Prerequisites (user actions, before implementation)

1. **Rust via rustup** (user runs it — per-user install, `~/.rustup` + `~/.cargo`, no sudo):
   `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh` then restart shell /
   `source ~/.cargo/env`. Avoid `brew install rust` (Tauri expects rustup toolchains).
2. Nothing else: Node v20 + Xcode CLT already present; `@tauri-apps/cli` arrives via
   `npm install` in `desktop/`; no new Python deps this phase.

## Verification

- **Per PR**: `ruff format` then `.venv/bin/pytest backend/ -q` once (Steps 1–2 should be
  no-op for pytest; Step 3 adds tests). `cargo check` / `npm run dev` compile check for Rust.
- **User-tested (their lane)**: Step 1 — launch app, window opens to login/library, confirm
  external-uvicorn coexistence and that quitting the app stops the spawned backend. Step 2 —
  capture button → Steam login window → credentials card shows saved cookies → "Test cookies"
  passes → full sync runs; then clear + re-capture to prove the refresh path. Step 3 — PSN
  page → capture → NPSSO stored with date → test-token flashes valid.
- Windows support is out of scope for verification (Mac first per roadmap); the async-command
  requirement keeps the code Windows-compatible for later.

## Handoff notes (if a different model executes this)

- **HTML/template/CSS/JS files must be modified via Bash (heredoc/sed), never Write/Edit** — a
  desktop-app hook fires on Write/Edit of HTML and opens a useless preview panel.
- Read `DESIGN.md` before touching any template/CSS/JS. Catppuccin theme, no emoji in UI
  chrome, HTMX only — no JS frameworks.
- Git: never commit to `main`; `git pull origin main` before every `git checkout -b`; never
  force-push; stay on the feature branch after opening a PR; sequential PRs (land 1 → branch 2
  from fresh main), not stacked.
- `ruff format` first, then pytest once — never format→test→format→test.
- The user tests PRs themselves from the local checkout — do not use browser tools to
  self-verify UI.

## References

- Tauri cookies API: added in Tauri v2.4.0 (`Webview::cookies_for_url`) —
  https://v2.tauri.app/release/tauri/v2.4.0/ , PR https://github.com/tauri-apps/tauri/pull/12665
  (returns HttpOnly/Secure cookies; async-only on Windows).
- Tauri sidecar/external-binary docs (future packaging phase): https://v2.tauri.app/develop/sidecar/
- PSN NPSSO flow: https://psn-api.achievements.app/authentication/authenticating-manually and
  psnawp docs https://psnawp.readthedocs.io/en/latest/additional_resources/README.html
  (log in at playstation.com → `https://ca.account.sony.com/api/v1/ssocookie` → NPSSO;
  NPSSO → access code → tokens; refresh token ~2 months).
