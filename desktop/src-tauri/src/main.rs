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

use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

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

fn main() {
    tauri::Builder::default()
        .manage(BackendChild(Mutex::new(None)))
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
