// Desktop shell: ensure the FastAPI backend is running on 127.0.0.1:8000
// (spawning it from the repo's .venv if needed), then open the UI in a
// WebView window. Packaging/bundled-Python is a later roadmap phase — this
// build assumes the repo checkout and .venv exist on the machine.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

const BACKEND_URL: &str = "http://127.0.0.1:8000";
const BACKEND_ADDR: &str = "127.0.0.1:8000";

/// Backend process we spawned, if any. None when an external `uvicorn --reload`
/// was already answering on the port — in that case it is not ours to kill.
struct BackendChild(Mutex<Option<Child>>);

/// Minimal HTTP GET /health over a raw socket — avoids pulling an HTTP client
/// crate for a localhost liveness probe.
fn backend_healthy() -> bool {
    let addr = match BACKEND_ADDR.parse() {
        Ok(a) => a,
        Err(_) => return false,
    };
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(500)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(1500)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(1500)));
    let request =
        format!("GET /health HTTP/1.1\r\nHost: {BACKEND_ADDR}\r\nConnection: close\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut buf = [0u8; 64];
    match stream.read(&mut buf) {
        Ok(n) if n > 0 => String::from_utf8_lossy(&buf[..n]).contains(" 200 "),
        _ => false,
    }
}

/// Repo root: GAMES_TRACKER_ROOT env var if set, else the compile-time
/// location of this crate (desktop/src-tauri → two levels up). The compile-time
/// fallback is what makes this a dev shell — the built .app only works on the
/// machine it was built on unless the env var is set.
fn repo_root() -> Result<PathBuf, String> {
    if let Ok(root) = std::env::var("GAMES_TRACKER_ROOT") {
        let path = PathBuf::from(&root);
        if path.join("backend").is_dir() {
            return Ok(path);
        }
        return Err(format!(
            "GAMES_TRACKER_ROOT={root} does not look like the repo (no backend/ directory)"
        ));
    }
    let compiled = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    compiled.canonicalize().map_err(|e| {
        format!(
            "repo root not found at compile-time path {} ({e}); set GAMES_TRACKER_ROOT",
            compiled.display()
        )
    })
}

fn spawn_backend() -> Result<Child, String> {
    let root = repo_root()?;
    let python = root.join(".venv/bin/python");
    if !python.is_file() {
        return Err(format!("no venv python at {}", python.display()));
    }
    // cwd must be the repo root: DATABASE_URL defaults to the relative
    // sqlite:///backend/app.db, so the cwd decides which DB file is opened.
    Command::new(python)
        .args([
            "-m",
            "uvicorn",
            "backend.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ])
        .current_dir(&root)
        .spawn()
        .map_err(|e| format!("failed to spawn backend: {e}"))
}

/// SIGTERM first so uvicorn shuts down cleanly (WAL checkpoint, worker
/// cancellation), escalate to SIGKILL if it hasn't exited after 5s.
fn terminate(child: &mut Child) {
    unsafe {
        libc::kill(child.id() as libc::pid_t, libc::SIGTERM);
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if let Ok(Some(_)) = child.try_wait() {
            return;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    let _ = child.kill();
    let _ = child.wait();
}

#[derive(serde::Serialize)]
struct SteamCookies {
    sessionid: String,
    steam_login_secure: String,
}

/// Open a Steam sign-in window and poll the shared cookie store until the
/// logged-in marker cookie appears, then hand both cookies back to the page
/// JS (which submits them through the existing credentials form, so auth and
/// flash UX stay server-side). The store domain matters: the backend's one
/// cookie-authenticated call is store.steampowered.com/dynamicstore/userdata/,
/// and steamLoginSecure is per-domain — capturing on steamcommunity.com would
/// yield cookies the backend can't use.
///
/// Async command on purpose: cookies_for_url deadlocks in sync commands on
/// Windows, and the poll loop must not block the IPC thread. The blocking
/// loop lives on a spawn_blocking thread; WebviewWindow methods proxy to the
/// main thread internally.
#[tauri::command]
async fn capture_steam_login(app: AppHandle) -> Result<SteamCookies, String> {
    const LABEL: &str = "steam-login";
    const STORE_URL: &str = "https://store.steampowered.com";
    if let Some(existing) = app.get_webview_window(LABEL) {
        let _ = existing.set_focus();
        return Err("A Steam sign-in window is already open.".into());
    }
    let login_url = format!("{STORE_URL}/login/")
        .parse()
        .expect("static URL parses");
    let window = WebviewWindowBuilder::new(&app, LABEL, WebviewUrl::External(login_url))
        .title("Sign in to Steam")
        .inner_size(920.0, 800.0)
        .build()
        .map_err(|e| format!("Could not open the Steam sign-in window: {e}"))?;

    let result = tauri::async_runtime::spawn_blocking(move || {
        let store_url: tauri::Url = STORE_URL.parse().expect("static URL parses");
        let deadline = Instant::now() + Duration::from_secs(600);
        loop {
            std::thread::sleep(Duration::from_millis(1500));
            // Window gone = the user closed it without finishing sign-in.
            if window.app_handle().get_webview_window(LABEL).is_none() {
                return Err("Steam sign-in was cancelled.".to_string());
            }
            if Instant::now() > deadline {
                let _ = window.close();
                return Err("Timed out waiting for Steam sign-in.".to_string());
            }
            let cookies = match window.cookies_for_url(store_url.clone()) {
                Ok(c) => c,
                Err(e) => {
                    eprintln!("[games-tracker] cookie poll failed: {e}");
                    continue;
                }
            };
            // sessionid exists even for anonymous visitors; steamLoginSecure
            // only exists once signed in — it's the completion marker.
            let find = |name: &str| {
                cookies
                    .iter()
                    .find(|c| c.name() == name)
                    .map(|c| c.value().to_string())
            };
            if let (Some(secure), Some(sessionid)) =
                (find("steamLoginSecure"), find("sessionid"))
            {
                let _ = window.close();
                return Ok(SteamCookies {
                    sessionid,
                    steam_login_secure: secure,
                });
            }
        }
    })
    .await
    .map_err(|e| format!("Capture task failed: {e}"))?;
    result
}

#[derive(serde::Serialize)]
struct PsnToken {
    npsso: String,
}

/// Open a PlayStation sign-in window and poll the cookie store for the
/// `npsso` cookie on ca.account.sony.com — the token the backend stores and
/// later exchanges for PSN API access (the psn-api flow, validated by the
/// psn-library-generator prototype). The cookie is HttpOnly, which is why
/// the manual web flow needs the /api/v1/ssocookie JSON endpoint; reading
/// the store directly sees it as soon as sign-in completes. If it hasn't
/// materialized shortly after the user lands back on playstation.com, one
/// navigation to the ssocookie endpoint re-establishes it.
#[tauri::command]
async fn capture_psn_login(app: AppHandle) -> Result<PsnToken, String> {
    const LABEL: &str = "psn-login";
    const SONY_ACCOUNT_URL: &str = "https://ca.account.sony.com";
    const SSOCOOKIE_URL: &str = "https://ca.account.sony.com/api/v1/ssocookie";
    if let Some(existing) = app.get_webview_window(LABEL) {
        let _ = existing.set_focus();
        return Err("A PlayStation sign-in window is already open.".into());
    }
    let login_url = "https://my.playstation.com/"
        .parse()
        .expect("static URL parses");
    let window = WebviewWindowBuilder::new(&app, LABEL, WebviewUrl::External(login_url))
        .title("Sign in to PlayStation")
        .inner_size(920.0, 800.0)
        .build()
        .map_err(|e| format!("Could not open the PlayStation sign-in window: {e}"))?;

    let result = tauri::async_runtime::spawn_blocking(move || {
        let sony_url: tauri::Url = SONY_ACCOUNT_URL.parse().expect("static URL parses");
        let deadline = Instant::now() + Duration::from_secs(600);
        let mut saw_signin = false;
        let mut back_on_psn_since: Option<Instant> = None;
        let mut nudged_ssocookie = false;
        loop {
            std::thread::sleep(Duration::from_millis(1500));
            if window.app_handle().get_webview_window(LABEL).is_none() {
                return Err("PlayStation sign-in was cancelled.".to_string());
            }
            if Instant::now() > deadline {
                let _ = window.close();
                return Err("Timed out waiting for PlayStation sign-in.".to_string());
            }
            if let Ok(cookies) = window.cookies_for_url(sony_url.clone()) {
                if let Some(c) = cookies.iter().find(|c| c.name() == "npsso") {
                    let token = c.value().to_string();
                    let _ = window.close();
                    return Ok(PsnToken { npsso: token });
                }
            }
            // Fallback: sign-in bounces my.playstation.com → Sony SSO → back.
            // If we've seen the SSO page and been back on playstation.com for
            // a few seconds with no npsso cookie, visit the ssocookie endpoint
            // once — it re-establishes the cookie for a signed-in session.
            if let Ok(url) = window.url() {
                let host = url.host_str().unwrap_or("");
                if host.contains("account.sony.com") || host.contains("sonyentertainmentnetwork") {
                    saw_signin = true;
                    back_on_psn_since = None;
                } else if saw_signin && host.ends_with("playstation.com") {
                    let since = *back_on_psn_since.get_or_insert_with(Instant::now);
                    if !nudged_ssocookie && since.elapsed() > Duration::from_secs(5) {
                        nudged_ssocookie = true;
                        let sso = SSOCOOKIE_URL.parse().expect("static URL parses");
                        let _ = window.navigate(sso);
                    }
                }
            }
        }
    })
    .await
    .map_err(|e| format!("Capture task failed: {e}"))?;
    result
}

fn main() {
    tauri::Builder::default()
        .manage(BackendChild(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![
            capture_steam_login,
            capture_psn_login
        ])
        .setup(|app| {
            let handle = app.handle().clone();
            // Backend bring-up happens off the main thread so the event loop
            // starts immediately; the window is created once /health answers.
            std::thread::spawn(move || {
                if !backend_healthy() {
                    match spawn_backend() {
                        Ok(child) => {
                            handle
                                .state::<BackendChild>()
                                .0
                                .lock()
                                .unwrap()
                                .replace(child);
                            let deadline = Instant::now() + Duration::from_secs(30);
                            while !backend_healthy() && Instant::now() < deadline {
                                std::thread::sleep(Duration::from_millis(300));
                            }
                            if !backend_healthy() {
                                eprintln!(
                                    "[games-tracker] backend did not answer on {BACKEND_ADDR} within 30s"
                                );
                            }
                        }
                        Err(e) => eprintln!("[games-tracker] {e}"),
                    }
                }
                let url = BACKEND_URL.parse().expect("static URL parses");
                if let Err(e) =
                    WebviewWindowBuilder::new(&handle, "main", WebviewUrl::External(url))
                        .title("Games Tracker")
                        .inner_size(1440.0, 920.0)
                        .build()
                {
                    eprintln!("[games-tracker] failed to create window: {e}");
                    handle.exit(1);
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error building tauri app")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                if let Some(mut child) = app.state::<BackendChild>().0.lock().unwrap().take() {
                    terminate(&mut child);
                }
            }
        });
}
