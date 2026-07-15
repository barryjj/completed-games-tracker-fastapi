# Games Tracker — desktop shell (Tauri)

Dev-shell phase: the app launches the FastAPI backend from this repo's `.venv` and opens the
UI in a WebView. No bundled Python — the repo checkout and `.venv` must exist on the machine.
Full plan: [`docs/tauri-desktop-plan.md`](../docs/tauri-desktop-plan.md).

## Prerequisites

- Rust via rustup (`rustc`/`cargo` on PATH)
- Node (for `@tauri-apps/cli`) — `npm install` in this directory once
- The repo `.venv` set up as usual

## Run

```sh
npm run dev      # debug build + launch
npm run build    # produces the .app under src-tauri/target/release/bundle/macos/
```

Behavior on launch:

1. If something already answers on `http://127.0.0.1:8000/health` (e.g. your own
   `uvicorn --reload`), the app just opens the window against it — nothing is spawned or
   killed on quit.
2. Otherwise it spawns `.venv/bin/python -m uvicorn backend.main:app` with the repo root as
   cwd (so the default `sqlite:///backend/app.db` resolves to the same DB as always), waits
   for `/health`, then opens the window. Quitting the app SIGTERMs the spawned backend.

The repo root is baked in at compile time (this crate's location, two levels up). To point a
built .app at a different checkout, set `GAMES_TRACKER_ROOT=/path/to/repo`.

## App icon

Two icon sources live in `src-tauri/icons/`:

- `icon-source.svg` — **current default.** Controller silhouette adapted from Bootstrap
  Icons "controller" (MIT); credit comment in the file is the required attribution.
- `icon-source-noun.svg` — alternate DualSense-style design adapted from "gamer controller"
  by Visual Glow / Noun Project (**CC BY 3.0**). If this ever becomes the shipped icon, add a
  visible credit line (about/settings page), e.g. "Controller icon by Visual Glow (Noun
  Project), CC BY 3.0" — the license requires attribution even for modified versions.

To switch: rasterize the SVG with an alpha-preserving renderer and regenerate the set —
**not** `qlmanage`, which flattens transparent corners to opaque white (that bug shipped
once already):

```sh
npx -y @resvg/resvg-js-cli src-tauri/icons/icon-source-noun.svg /tmp/icon.png
npx tauri icon /tmp/icon.png -o src-tauri/icons
rm -rf src-tauri/icons/{android,ios,Square*.png,StoreLogo.png,64x64.png}
```
