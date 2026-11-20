use std::env;
use std::ffi::OsString;
use std::io::Write;
use std::process::{Command, ExitCode};

use anvildroid_core::backend;
use anvildroid_core::runtime;

mod managed_cli;

const HELP: &str = concat!("AnvilDroid ", env!("CARGO_PKG_VERSION"), " — experimental Waydroid backend
Usage: anvildroid COMMAND [--json]
  doctor                 Read-only preliminary host checks
  status                 Backend runtime status
  health                 Check backend state and desktop session D-Bus
  recover                Recover missing session (foreground; may close Android windows)
  start                  Start user session (foreground; use another terminal)
  stop                   Stop user session, keep app data
  multi-window           Enable multi-window; then stop/start session
  window-mode MODE       Set desktop or immersive Android window mode
  runtimes [--json]      Show registered runtime and configured settings
  apps                   List installed apps
  install FILE...        Install APKs, split APKs or bundles (.apks, .xapk, .apkm)
  run PACKAGE            Launch an installed package
  screenshot FILE.png    Save a picture of the running Android display to a new file
  log                    Show/follow backend logs
  gpu [vulkan|gles]      Show or set container GPU mode (restart to apply)

Options:
  --json                 Output machine-readable JSON (status, health, apps, gpu, runtimes)
  --runtime ID           Target a managed runtime (see below)

{MANAGED}

Setup: see README.md. Container must be started separately.
No ARM translation or native-integration guarantees in this prototype.");

fn report(result: Result<u8, managed_cli::Failure>) -> Result<u8, String> {
    match result {
        Ok(code) => Ok(code),
        Err(failure) => {
            eprintln!("anvildroid: {}", failure.message);
            Ok(failure.code)
        }
    }
}

fn wants_json(args: &[OsString]) -> bool {
    args.iter().any(|a| a == "--json")
}

fn strip_json_flag(args: &[OsString]) -> Vec<OsString> {
    args.iter().filter(|a| *a != "--json").cloned().collect()
}

fn run() -> Result<u8, String> {
    let raw_args: Vec<_> = env::args_os().skip(1).collect();
    if raw_args.is_empty()
        || (raw_args.len() == 1 && matches!(raw_args[0].to_str(), Some("--help" | "-h")))
    {
        println!("{}", HELP.replace("{MANAGED}", managed_cli::HELP));
        return Ok(0);
    }

    let (runtime_id, raw_args) =
        anvildroid_core::scope::parse_selector(&raw_args).map_err(|e| e.to_string())?;
    let json = wants_json(&raw_args);
    let args = strip_json_flag(&raw_args);

    let command = args.first().and_then(|s| s.to_str()).unwrap_or("");

    if anvildroid_core::library::valid_managed_runtime(&runtime_id) {
        // Managed runtimes are served only by the controller, never the singleton backend.
        match command {
            "run" => {
                if args.len() != 2 || json {
                    return report(Err(managed_cli::Failure { code: managed_cli::USAGE,
                        message: "Usage: anvildroid --runtime ID run PACKAGE".into() }));
                }
                let package = args[1].to_str().ok_or("Package must be UTF-8")?;
                anvildroid_core::managed::launch(&runtime_id, package).map_err(|e| e.to_string())?;
                println!("Opened {package} in {runtime_id}");
                return Ok(0);
            }
            "install" => {
                if args.len() < 2 || json {
                    return report(Err(managed_cli::Failure { code: managed_cli::USAGE,
                        message: "Usage: anvildroid --runtime ID install FILE.apk [FILE ...]".into() }));
                }
                return install_files(&args[1..], |set| {
                    anvildroid_core::managed::install_set(&runtime_id, set)?;
                    Ok(format!("Installed {} into {runtime_id}; Android confirmed installation.",
                        anvildroid_core::apk_set::describe(set)))
                });
            }
            "screenshot" => return screenshot(&runtime_id, &args, json, "anvildroid --runtime ID screenshot FILE.png"),
            _ => return report(managed_cli::run(&mut managed_cli::Live, &runtime_id, &raw_args)),
        }
    }
    if command == "managed-runtimes" {
        if runtime_id != anvildroid_core::scope::LEGACY_ID {
            return Err("managed-runtimes lists every runtime; do not pass --runtime".into());
        }
        return report(managed_cli::list(&mut managed_cli::Live, &raw_args));
    }

    // Even global queries reject unsupported explicit selectors. Never forward
    // a request aimed at another runtime to the singleton backend.
    if command != "doctor" || runtime_id != anvildroid_core::scope::LEGACY_ID {
        anvildroid_core::scope::ensure_legacy(&runtime_id).map_err(|e| e.to_string())?;
    }

    match command {
        "runtimes" => {
            if args.len() != 1 {
                return Err("runtimes does not accept extra arguments".into());
            }
            let catalog = anvildroid_core::registry::catalog().map_err(|e| e.to_string())?;
            if json {
                println!(
                    "{}",
                    serde_json::to_string_pretty(&catalog).map_err(|e| e.to_string())?
                );
            } else {
                for r in &catalog.registry.runtimes {
                    println!("{}: {} — {}", r.id, r.name, r.data_dir.display());
                }
                println!(
                    "Configured image: {:?}; renderer: {:?} (not measured)",
                    catalog.configured.settings.image_kind, catalog.configured.settings.renderer
                );
                println!("{}", catalog.management_note);
            }
            Ok(0)
        }
        "doctor" => {
            if args.len() != 1 {
                return Err("doctor does not accept extra arguments".into());
            }
            backend::doctor().map_err(|e| e.to_string())
        }
        "gpu" => {
            if json && args.len() == 1 {
                // --json with no argument: output current mode as JSON.
                match backend::gpu_mode() {
                    Ok(mode) => {
                        println!("{}", serde_json::to_string(&mode).unwrap_or_default());
                        Ok(0)
                    }
                    Err(e) => Err(e.to_string()),
                }
            } else {
                backend::gpu_command(&args).map_err(|e| e.to_string())
            }
        }
        "health" => {
            if args.len() != 1 {
                return Err("health does not accept extra arguments".into());
            }
            if json {
                let report = runtime::health_report();
                println!(
                    "{}",
                    serde_json::to_string_pretty(&report).unwrap_or_default()
                );
                Ok(if report.healthy { 0 } else { 1 })
            } else {
                runtime::health().map_err(|e| e.to_string())
            }
        }
        "recover" => {
            if args.len() != 1 {
                return Err("recover does not accept extra arguments".into());
            }
            runtime::recover().map_err(|e| e.to_string())
        }
        "window-mode" => backend::window_mode_command(&args).map_err(|e| e.to_string()),
        "status" => {
            if json {
                let status = runtime::runtime_status();
                println!(
                    "{}",
                    serde_json::to_string_pretty(&status).unwrap_or_default()
                );
                Ok(0)
            } else {
                // Fall through to forwarded backend command.
                forward_to_backend(&args)
            }
        }
        "apps" => {
            if json {
                match backend::list_apps() {
                    Ok(apps) => {
                        println!(
                            "{}",
                            serde_json::to_string_pretty(&apps).unwrap_or_default()
                        );
                        Ok(0)
                    }
                    Err(e) => Err(e.to_string()),
                }
            } else {
                forward_to_backend(&args)
            }
        }
        "install" => {
            if args.len() < 2 {
                return Err("Usage: anvildroid install FILE.apk [FILE ...]".into());
            }
            install_files(&args[1..], |set| {
                let info = anvildroid_core::apps::install_set(set)?;
                let version = info.version_code.map(|code| format!(" version {code}")).unwrap_or_default();
                Ok(format!("Installed {}{version}; verified in Android.", info.package))
            })
        }
        "screenshot" => screenshot(&runtime_id, &args, json, "anvildroid screenshot FILE.png"),
        "run" => {
            let forwarded = backend::backend_args(&args).map_err(|e| e.to_string())?;
            runtime::launch_preflight().map_err(|e| e.to_string())?;
            let output = Command::new("waydroid")
                .args(&forwarded)
                .output()
                .map_err(|e| format!("Cannot execute Waydroid: {e}. See README.md for setup."))?;
            std::io::stdout()
                .write_all(&output.stdout)
                .map_err(|e| e.to_string())?;
            std::io::stderr()
                .write_all(&output.stderr)
                .map_err(|e| e.to_string())?;
            if !output.status.success() {
                return Ok(output
                    .status
                    .code()
                    .and_then(|c| u8::try_from(c).ok())
                    .unwrap_or(1));
            }
            let combined = format!(
                "{}\n{}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            );
            if let Some(error) = runtime::launch_error(&combined) {
                return Err(error);
            }
            Ok(0)
        }
        _ => forward_to_backend(&args),
    }
}

/// Install APKs, split APKs and bundles: the files of one package go to
/// Android as one install session, packages one after another.
fn install_files(
    files: &[OsString],
    mut install: impl FnMut(&anvildroid_core::apk_set::PackageSet) -> Result<String, anvildroid_core::error::AnvilError>,
) -> Result<u8, String> {
    use anvildroid_core::apk_set;
    let paths: Vec<std::path::PathBuf> = files.iter().map(std::path::PathBuf::from).collect();
    let prepared = apk_set::prepare(&paths).map_err(|e| e.to_string())?;
    let mut failed = false;
    for rejected in &prepared.rejected {
        eprintln!("anvildroid: not installed: {}: {}", rejected.file, rejected.reason);
        failed = true;
    }
    for set in &prepared.packages {
        match install(set) {
            Ok(message) => {
                println!("{message}");
                if let Some(notice) = apk_set::obb_notice(set) {
                    eprintln!("anvildroid: note: {notice}");
                }
            }
            Err(error) => {
                eprintln!("anvildroid: not installed: {}: {error}", apk_set::describe(set));
                failed = true;
            }
        }
    }
    Ok(u8::from(failed))
}

/// Save a PNG of the whole Android display. An existing file is never replaced.
fn screenshot(id: &str, args: &[OsString], json: bool, usage: &str) -> Result<u8, String> {
    if args.len() != 2 || json {
        return report(Err(managed_cli::Failure { code: managed_cli::USAGE, message: format!("Usage: {usage}") }));
    }
    let path = env::current_dir().map_err(|e| e.to_string())?.join(&args[1]);
    let saved = anvildroid_core::controller::screenshot_to(id, &path, false)?;
    println!("Saved {} x {} screenshot to {}", saved["width"], saved["height"], path.display());
    Ok(0)
}

fn forward_to_backend(args: &[OsString]) -> Result<u8, String> {
    let forwarded = backend::backend_args(args).map_err(|e| e.to_string())?;
    let command = args.first().and_then(|s| s.to_str()).unwrap_or("");
    if command == "start" {
        println!(
            "Starting Waydroid user session. Wait for Android ready; use another terminal for app commands."
        );
    }
    let mut backend_command = if command == "start" {
        anvildroid_core::launch::session_start_command()
    } else {
        let mut backend = Command::new("waydroid");
        backend.args(&forwarded);
        backend
    };
    let status = backend_command.status()
        .map_err(|e| format!("Cannot execute Waydroid: {e}. See README.md for setup."))?;
    if status.success() && command == "multi-window" {
        println!(
            "Property set. Run anvildroid stop, then anvildroid start to apply (interrupts the current session)."
        );
    }
    Ok(status
        .code()
        .and_then(|c| u8::try_from(c).ok())
        .unwrap_or(1))
}

fn main() -> ExitCode {
    match run() {
        Ok(code) => ExitCode::from(code),
        Err(error) => {
            eprintln!("anvildroid: {error}");
            ExitCode::FAILURE
        }
    }
}
