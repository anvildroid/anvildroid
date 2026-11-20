use std::os::unix::fs::PermissionsExt;
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};

static NEXT_FIXTURE: AtomicUsize = AtomicUsize::new(0);

struct RuntimeFixture(std::path::PathBuf);
impl RuntimeFixture {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "anvildroid-runtime-{}-{}",
            std::process::id(),
            NEXT_FIXTURE.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&path).unwrap();
        for (name, body) in [
            ("dbus-send", "#!/bin/sh\nprintf 'boolean %s\\n' \"$TEST_SESSION_OWNER\"\n"),
            ("waydroid", "#!/bin/sh\nif [ \"$1\" = status ]; then\n printf 'Session: RUNNING\\nContainer: %s\\n' \"$TEST_CONTAINER\"\nelse\n printf 'LAUNCH_CALLED\\n'\n printf '%s\\n' \"$TEST_LAUNCH_MESSAGE\" >&2\n exit \"${TEST_LAUNCH_EXIT:-0}\"\nfi\n"),
        ] {
            let script = path.join(name);
            std::fs::write(&script, body).unwrap();
            std::fs::set_permissions(script, std::fs::Permissions::from_mode(0o700)).unwrap();
        }
        Self(path)
    }
    fn command(&self, args: &[&str]) -> Command {
        let mut command = Command::new(env!("CARGO_BIN_EXE_anvildroid"));
        command
            .args(args)
            .env("PATH", &self.0)
            .env("XDG_CONFIG_HOME", self.0.join("config"))
            .env("XDG_RUNTIME_DIR", &self.0)
            .env_remove("WAYLAND_DISPLAY")
            .env_remove("DISPLAY")
            .env("TEST_SESSION_OWNER", "true")
            .env("TEST_CONTAINER", "RUNNING")
            .env("TEST_LAUNCH_MESSAGE", "")
            .env("TEST_LAUNCH_EXIT", "0");
        command
    }
}
impl Drop for RuntimeFixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

#[test]
fn explicit_runtime_never_falls_back_to_live_singleton() {
    let fixture = RuntimeFixture::new();
    for args in [
        vec!["run", "com.example.game", "--runtime", "second"],
        vec!["stop", "--runtime", "second"],
        vec!["recover", "--runtime", "second"],
        vec!["apps", "--runtime", "second", "--json"],
        vec!["apps", "--runtime=second", "--json"],
        vec!["--runtime", "default", "stop", "--runtime", "second"],
    ] {
        let output = fixture.command(&args).output().unwrap();
        assert!(!output.status.success());
        assert!(!String::from_utf8_lossy(&output.stdout).contains("LAUNCH_CALLED"));
    }
    let output = fixture
        .command(&["run", "com.example.game", "--runtime", "default"])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(String::from_utf8_lossy(&output.stdout).contains("LAUNCH_CALLED"));
}

#[test]
fn missing_session_blocks_launch_without_calling_backend() {
    let fixture = RuntimeFixture::new();
    let output = fixture
        .command(&["run", "com.android.settings"])
        .env("TEST_SESSION_OWNER", "false")
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(!String::from_utf8_lossy(&output.stdout).contains("LAUNCH_CALLED"));
    assert!(String::from_utf8_lossy(&output.stderr).contains("anvildroid stop"));
}

#[test]
fn recover_preserves_present_session_and_refuses_missing_display() {
    let fixture = RuntimeFixture::new();
    let output = fixture.command(&["recover"]).output().unwrap();
    assert!(output.status.success());
    assert!(!String::from_utf8_lossy(&output.stdout).contains("LAUNCH_CALLED"));
    let output = fixture
        .command(&["recover"])
        .env("TEST_SESSION_OWNER", "false")
        .env_remove("WAYLAND_DISPLAY")
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(!String::from_utf8_lossy(&output.stdout).contains("LAUNCH_CALLED"));
}

#[test]
fn recover_starts_only_after_verified_stop() {
    let fixture = RuntimeFixture::new();
    let socket = fixture.0.join("wayland-test");
    let _listener = std::os::unix::net::UnixListener::bind(&socket).unwrap();
    let backend = fixture.0.join("waydroid");
    std::fs::write(&backend, "#!/bin/sh\ncase \"$1 $2\" in\n 'session stop') echo STOP; exit \"$TEST_STOP_EXIT\";;\n 'status ') printf 'Session: STOPPED\\n';;\n 'session start') echo START;;\n *) exit 9;;\nesac\n").unwrap();
    for (stop_exit, success) in [("0", true), ("7", false)] {
        let output = fixture
            .command(&["recover"])
            .env("TEST_SESSION_OWNER", "false")
            .env("WAYLAND_DISPLAY", &socket)
            .env("TEST_STOP_EXIT", stop_exit)
            .output()
            .unwrap();
        assert_eq!(output.status.success(), success);
        assert_eq!(
            String::from_utf8_lossy(&output.stdout)
                .lines()
                .any(|l| l == "START"),
            success
        );
    }
}

#[test]
fn launch_detects_zero_exit_backend_errors_and_preserves_nonzero() {
    let fixture = RuntimeFixture::new();
    for message in [
        "RuntimeError: Already tracking a session",
        "Failed to access IPlatform service",
        "WayDroid is not initialized",
    ] {
        let output = fixture
            .command(&["run", "com.android.settings"])
            .env("TEST_LAUNCH_MESSAGE", message)
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(1));
        assert!(String::from_utf8_lossy(&output.stderr).contains(message));
    }
    let output = fixture
        .command(&["run", "com.android.settings"])
        .env("TEST_LAUNCH_EXIT", "7")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(7));
    assert!(fixture
        .command(&["run", "com.android.settings"])
        .output()
        .unwrap()
        .status
        .success());
}

#[test]
fn health_distinguishes_missing_session_from_suspension() {
    let fixture = RuntimeFixture::new();
    assert!(!fixture
        .command(&["health"])
        .env("TEST_SESSION_OWNER", "false")
        .output()
        .unwrap()
        .status
        .success());
    for state in ["RUNNING", "FROZEN"] {
        assert!(fixture
            .command(&["health"])
            .env("TEST_CONTAINER", state)
            .output()
            .unwrap()
            .status
            .success());
    }
    assert!(!fixture
        .command(&["health"])
        .env("TEST_CONTAINER", "STOPPED")
        .output()
        .unwrap()
        .status
        .success());
    assert!(!fixture
        .command(&["health"])
        .env("TEST_SESSION_OWNER", "unexpected")
        .output()
        .unwrap()
        .status
        .success());
}

#[test]
fn missing_backend_is_an_actionable_failure() {
    let fixture = RuntimeFixture::new();
    let output = fixture
        .command(&["status"])
        .env("PATH", "")
        .output()
        .unwrap();
    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("Cannot execute Waydroid"));
    assert!(stderr.contains("README.md"));
}

#[test]
fn backend_exit_failure_is_preserved() {
    // A failing executable substitutes only the process boundary; this is not
    // an Android/runtime integration test.
    let directory =
        std::env::temp_dir().join(format!("anvildroid-cli-test-{}", std::process::id()));
    std::fs::create_dir(&directory).unwrap();
    std::os::unix::fs::symlink("/bin/false", directory.join("waydroid")).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_anvildroid"))
        .arg("status")
        .env("PATH", &directory)
        .env("XDG_CONFIG_HOME", directory.join("config"))
        .output();
    std::fs::remove_file(directory.join("waydroid")).unwrap();
    std::fs::remove_dir(&directory).unwrap();
    assert_eq!(output.unwrap().status.code(), Some(1));
}

#[test]
fn install_reports_every_file_it_will_not_install() {
    // Nothing here reaches Android: every file is refused while it is inspected.
    let fixture = RuntimeFixture::new();
    let split = fixture.0.join("notes.txt");
    let fake = fixture.0.join("fake.apk");
    let bundle = fixture.0.join("empty.xapk");
    std::fs::write(&split, b"text").unwrap();
    std::fs::write(&fake, b"not a zip").unwrap();
    std::fs::write(&bundle, b"PK\x05\x06\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0").unwrap();
    let missing = fixture.0.join("missing.apks");
    for selector in [&[][..], &["--runtime", "r-11111111111111111111111111111111"][..]] {
        let files = [&split, &fake, &bundle, &missing].map(|path| path.to_str().unwrap());
        let output = fixture.command(&[selector, &["install"], &files].concat())
            .env("XDG_CACHE_HOME", fixture.0.join("cache"))
            .env("XDG_DATA_HOME", fixture.0.join("data"))
            .output().unwrap();
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(stderr.contains("not installed: notes.txt: Not an Android package"), "{stderr}");
        assert!(stderr.contains("not installed: fake.apk: Not an APK file"), "{stderr}");
        assert!(stderr.contains("not installed: empty.xapk: Not a ZIP-based package bundle"), "{stderr}");
        assert!(stderr.contains("not installed: missing.apks: Cannot read the file"), "{stderr}");
    }
    let usage = fixture.command(&["install"]).output().unwrap();
    assert_eq!(usage.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&usage.stderr).contains("install FILE.apk [FILE ...]"));
}

#[test]
fn screenshot_checks_usage_and_never_replaces_an_existing_file() {
    let fixture = RuntimeFixture::new();
    let managed = ["--runtime", "r-11111111111111111111111111111111"];
    let help = fixture.command(&["--help"]).output().unwrap();
    let help = String::from_utf8_lossy(&help.stdout).into_owned();
    assert!(help.contains("  screenshot FILE.png "), "{help}");
    assert!(help.contains("  --runtime ID screenshot FILE.png "), "{help}");
    for (selector, usage) in [(&[][..], "Usage: anvildroid screenshot FILE.png"),
                              (&managed[..], "Usage: anvildroid --runtime ID screenshot FILE.png")] {
        for extra in [&[][..], &["a.png", "b.png"][..], &["a.png", "--json"][..]] {
            let output = fixture.command(&[selector, &["screenshot"], extra].concat()).current_dir(&fixture.0).output().unwrap();
            assert_eq!(output.status.code(), Some(2), "{extra:?}");
            assert!(String::from_utf8_lossy(&output.stderr).contains(usage));
        }
        // Refused before the controller is contacted; relative names resolve in the current folder.
        std::fs::write(fixture.0.join("old.png"), b"keep").unwrap();
        for name in ["old.png".to_owned(), fixture.0.join("old.png").to_str().unwrap().to_owned()] {
            let output = fixture.command(&[selector, &["screenshot", &name]].concat()).current_dir(&fixture.0).output().unwrap();
            assert_eq!(output.status.code(), Some(1));
            assert!(output.stdout.is_empty());
            let stderr = String::from_utf8_lossy(&output.stderr);
            assert!(stderr.contains("old.png already exists. Choose another file name."), "{stderr}");
        }
        assert_eq!(std::fs::read(fixture.0.join("old.png")).unwrap(), b"keep");
        assert!(!std::fs::read_dir(&fixture.0).unwrap().any(|entry| entry.unwrap().file_name().to_string_lossy().ends_with(".part")));
    }
    let foreign = fixture.command(&["--runtime", "second", "screenshot", "x.png"]).current_dir(&fixture.0).output().unwrap();
    assert!(!foreign.status.success());
    assert!(!fixture.0.join("x.png").exists());
}
