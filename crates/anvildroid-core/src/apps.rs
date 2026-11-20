//! Narrow Android shell-UID companion; no privileged host process or shell text.
use crate::{apk_set::{self, PackageSet, Part}, backend, error::AnvilError, launch};
use serde::{Deserialize, Serialize};
use std::{
    fs,
    path::Path,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppDetails {
    pub package: String,
    pub installed: bool,
    pub version_code: Option<u64>,
    pub version_name: Option<String>,
    pub abi: Option<String>,
    pub stopped: Option<bool>,
    pub profile: Option<String>,
}

fn field<'a>(text: &'a str, key: &str) -> Option<&'a str> {
    text.lines()
        .map(str::trim)
        .find_map(|line| line.strip_prefix(key))
}

pub fn parse_details(package: &str, text: &str) -> AppDetails {
    let marker = format!("Package [{package}]");
    let section = text.find(&marker).map(|i| &text[i..]);
    let section = section
        .and_then(|s| s.split("\n  Package [").next())
        .unwrap_or("");
    let user = field(section, "User 0:").unwrap_or("");
    AppDetails {
        package: package.into(),
        installed: !section.is_empty() && user.split_whitespace().any(|s| s == "installed=true"),
        version_code: field(section, "versionCode=")
            .and_then(|v| v.split_whitespace().next()?.parse().ok()),
        version_name: field(section, "versionName=")
            .filter(|v| *v != "null")
            .map(String::from),
        abi: field(section, "primaryCpuAbi=")
            .filter(|v| *v != "null")
            .map(String::from),
        stopped: user
            .split_whitespace()
            .find_map(|s| s.strip_prefix("stopped="))
            .and_then(|s| s.parse().ok()),
        profile: None,
    }
}

/// Seconds to wait for an install: large packages take longer to copy and verify.
fn install_timeout(bytes: u64) -> u64 {
    (120 + bytes / (2 * 1024 * 1024)).min(900)
}

fn request(action: &str, package: &str, apks: &[Part]) -> Result<String, AnvilError> {
    let base = dirs::data_dir()
        .ok_or_else(|| AnvilError::Config("No user data directory".into()))?
        .join("waydroid/data/waydroid_tmp/anvildroid/apps");
    request_in(&base, action, package, apks)
}

/// Queue one job for the in-Android helper. One APK is `input.apk`; a split
/// package is `input-0.apk` (the base), `input-1.apk`, … for one session.
fn request_in(base: &Path, action: &str, package: &str, apks: &[Part]) -> Result<String, AnvilError> {
    if !backend::valid_package(package) {
        return Err(AnvilError::InvalidArgument("Invalid package".into()));
    }
    if !base.is_dir() {
        return Err(AnvilError::BackendFailed("App-management companion is unavailable. Install the P5.2 native release and restart Waydroid.".into()));
    }
    let id = format!(
        "job-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos()
    );
    let staging = base.join(format!("{id}.staging"));
    let ready = base.join(format!("{id}.ready"));
    let working = base.join(format!("{id}.working"));
    fs::create_dir(&staging)?;
    // The shell service needs group access to the host-created request.
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(&staging, fs::Permissions::from_mode(0o2770))?;
    let result = (|| {
        fs::write(staging.join("action"), format!("{action}\n"))?;
        fs::write(staging.join("package"), format!("{package}\n"))?;
        for (index, apk) in apks.iter().enumerate() {
            let name = if apks.len() == 1 { "input.apk".to_string() } else { format!("input-{index}.apk") };
            let mut file = fs::OpenOptions::new().write(true).create_new(true).open(staging.join(name))?;
            apk.copy_to(&mut file)?;
        }
        if apks.len() > 1 {
            // The helper installs exactly this many files, or nothing.
            fs::write(staging.join("count"), format!("{}\n", apks.len()))?;
        }
        for file in fs::read_dir(&staging)? {
            fs::set_permissions(file?.path(), fs::Permissions::from_mode(0o660))?;
        }
        fs::rename(&staging, &ready)?;
        let timeout = match action {
            "install" => install_timeout(apks.iter().map(|apk| apk.size).sum()),
            "uninstall" => 30,
            _ => 15,
        };
        let started = Instant::now();
        loop {
            match fs::read_to_string(working.join("status")) {
                Ok(status) => {
                    let text = fs::read_to_string(working.join("output"))?;
                    if status.trim() == "2" && text.trim().is_empty() && apks.len() > 1 {
                        // A helper from before split support stops without a word.
                        return Err(AnvilError::BackendFailed(
                            "System Waydroid cannot install split packages yet. Update its AnvilDroid runtime tools, restart System Waydroid and retry.".into(),
                        ));
                    }
                    if status.trim() != "0" {
                        return Err(AnvilError::BackendFailed(format!(
                            "Android {action} failed: {}",
                            text.trim()
                        )));
                    }
                    return Ok(text);
                }
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(e) => return Err(e.into()),
            }
            if started.elapsed() >= Duration::from_secs(timeout) {
                // Do not delete an in-flight install or claim that it was cancelled.
                return Err(AnvilError::Timeout(format!(
                    "Android {action} timed out; it may still complete. Refresh before retrying."
                )));
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    })();
    let _ = fs::remove_dir_all(staging);
    if working.join("status").is_file() {
        let _ = fs::remove_dir_all(working);
    }
    result
}

pub fn details(package: &str) -> Result<AppDetails, AnvilError> {
    let text = request("details", package, &[])?;
    if !text.contains(&format!("Package [{package}]"))
        && !text
            .lines()
            .any(|line| line.trim() == format!("Unable to find package: {package}"))
    {
        return Err(AnvilError::BackendFailed(
            "Android returned an unrecognized package query; installation state is unknown.".into(),
        ));
    }
    Ok(parse_details(package, &text))
}

pub fn force_stop(package: &str) -> Result<(), AnvilError> {
    if let Some(reason) = backend::uninstall_block_reason(package) {
        return Err(AnvilError::InvalidArgument(reason.into()));
    }
    launch::ensure_ready(launch::BOOT_TIMEOUT)?;
    if !details(package)?.installed {
        return Err(AnvilError::InvalidArgument("App is not installed".into()));
    }
    request("force-stop", package, &[])?;
    if details(package)?.stopped != Some(true) {
        return Err(AnvilError::BackendFailed(
            "Force-stop could not be confirmed".into(),
        ));
    }
    Ok(())
}

/// The GUI has already confirmed removal. Use the shell-UID companion so
/// Waydroid's Android 13 PendingIntent failure cannot strand an invisible UI.
pub fn uninstall(package: &str) -> Result<(), AnvilError> {
    if !backend::valid_package(package) {
        return Err(AnvilError::InvalidArgument("Invalid package".into()));
    }
    if let Some(reason) = backend::uninstall_block_reason(package) {
        return Err(AnvilError::InvalidArgument(reason.into()));
    }
    launch::ensure_ready(launch::BOOT_TIMEOUT)?;
    if !details(package)?.installed {
        return Err(AnvilError::InvalidArgument(
            "App is not installed. Refresh the app list.".into(),
        ));
    }
    let response = request("uninstall", package, &[])?;
    if !response.lines().any(|line| line.trim() == "Success") {
        return Err(AnvilError::BackendFailed(format!(
            "Android did not confirm uninstall: {}",
            response.trim()
        )));
    }
    if details(package)?.installed {
        return Err(AnvilError::BackendFailed(
            "Android still reports the app installed; refresh before retrying.".into(),
        ));
    }
    Ok(())
}

pub fn apk_identity(text: &str) -> Result<(String, u64), AnvilError> {
    let line = text
        .lines()
        .find(|s| s.starts_with("package: "))
        .ok_or_else(|| AnvilError::InvalidArgument("Cannot read APK manifest".into()))?;
    let attr = |key: &str| {
        line.split_once(&format!("{key}='"))
            .and_then(|(_, s)| s.split('\'').next())
    };
    let package = attr("name")
        .filter(|p| backend::valid_package(p))
        .ok_or_else(|| AnvilError::InvalidArgument("Invalid APK package".into()))?;
    let version: u64 = attr("versionCode")
        .and_then(|s| s.parse().ok())
        .ok_or_else(|| AnvilError::InvalidArgument("Invalid APK versionCode".into()))?;
    let major: u64 = attr("versionCodeMajor")
        .map(str::parse)
        .transpose()
        .map_err(|_| AnvilError::InvalidArgument("Invalid versionCodeMajor".into()))?
        .unwrap_or(0);
    if major > u32::MAX as u64 || version > u32::MAX as u64 {
        return Err(AnvilError::InvalidArgument(
            "APK version out of range".into(),
        ));
    }
    Ok((package.into(), (major << 32) | version))
}

/// How long an install waits for Android's package service to come back.
pub(crate) const RESTART_WAIT: Duration = Duration::from_secs(90);
pub(crate) const RESTART_PAUSE: Duration = Duration::from_secs(3);
/// How long an install waits for one Android is already committing.
const SETTLE_WAIT: Duration = Duration::from_secs(60);
const SETTLE_PAUSE: Duration = Duration::from_secs(2);

/// Android's system process restarted under the request: the package service
/// went away or is not registered yet. Nothing was installed.
fn android_restarting(error: &AnvilError) -> bool {
    let AnvilError::BackendFailed(message) = error else { return false };
    // The third one is what a managed device's worker says after its own check.
    ["Failure calling service package", "Can't find service: package",
        "package service is not ready", "DeadSystemException", "DeadObjectException"]
        .iter().any(|sign| message.contains(sign))
}

/// Android is committing an install session right now, as listed by
/// `dumpsys package installs`. Such a session holds Android's install lock.
/// Sessions still being downloaded or written do not count, and neither does
/// text this parser does not know. `services/runtime-linux.py` reads the same
/// text for managed devices.
fn install_in_flight(text: &str) -> bool {
    let now_ms = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis();
    install_in_flight_at(text, now_ms)
}

/// Android stamps a commit with the wall clock, which the container shares.
const INSTALL_SESSION_STALE_MS: u128 = 600_000;

fn install_in_flight_at(text: &str, now_ms: u128) -> bool {
    #[derive(Default)]
    struct Session { committed: Option<bool>, sealed: Option<bool>, destroyed: bool, committed_ms: Option<u128> }
    // A system process restart leaves committed sessions listed as active for
    // good; one committed long ago is a leftover, not an install in progress.
    let committing = |session: &Option<Session>| {
        session.as_ref().is_some_and(|session| {
            !session.destroyed
                && session.committed.or(session.sealed) == Some(true)
                && !session.committed_ms.is_some_and(|stamp| {
                    stamp > 0 && stamp + INSTALL_SESSION_STALE_MS <= now_ms
                })
        })
    };
    let mut session: Option<Session> = None;
    let mut active = false;
    for line in text.lines().map(str::trim) {
        if line == "Active install sessions:" {
            active = true;
        } else if !active {
            continue;
        } else if line.ends_with(" install sessions:") {
            break;
        } else if line.strip_prefix("Active ").unwrap_or(line)
            .strip_prefix("Session ").and_then(|rest| rest.strip_suffix(':'))
            .is_some_and(|id| !id.is_empty() && id.bytes().all(|byte| byte.is_ascii_digit()))
        {
            // Android 13 prints `Active Session N:`.
            if committing(&session) {
                return true;
            }
            session = Some(Session::default());
        } else if let Some(session) = session.as_mut() {
            for word in line.split_whitespace() {
                match word.split_once('=') {
                    Some(("mCommitted", value)) if session.committed.is_none() => session.committed = Some(value == "true"),
                    Some(("mSealed", value)) if session.sealed.is_none() => session.sealed = Some(value == "true"),
                    Some(("mDestroyed", "true")) => session.destroyed = true,
                    Some(("committedMillis", value)) if session.committed_ms.is_none() => session.committed_ms = value.parse().ok(),
                    _ => {}
                }
            }
        }
    }
    committing(&session)
}

/// Wait until the package service answers and no other install is being
/// committed. An install that was suspended half way keeps the install lock,
/// and Android's watchdog may restart the system process within a minute of
/// the resume; a request queued behind it would be lost in that restart.
/// `pause` waits and reports whether there is time for another look.
fn settle(mut probe: impl FnMut() -> Result<String, AnvilError>, mut pause: impl FnMut() -> bool) {
    loop {
        match probe() {
            Ok(text) if text.lines().any(|line| line.starts_with("package:")) && !install_in_flight(&text) => return,
            Ok(_) => {}
            Err(error) if android_restarting(&error) => {}
            // A helper from before this check cannot answer; install as before.
            Err(_) => return,
        }
        if !pause() {
            return;
        }
    }
}

/// Repeat a request that failed only because Android was restarting.
/// `pause` waits and reports whether another attempt is still allowed.
pub(crate) fn retry_after_restart<T>(
    mut attempt: impl FnMut() -> Result<T, AnvilError>,
    mut pause: impl FnMut() -> bool,
) -> Result<T, AnvilError> {
    loop {
        match attempt() {
            Err(error) if android_restarting(&error) => {
                if !pause() {
                    return Err(AnvilError::BackendFailed(format!(
                        "Android restarted during the installation and is not ready yet. Try again in a moment. ({error})"
                    )));
                }
            }
            other => return other,
        }
    }
}

/// Install one APK file or one bundle that holds a single package.
pub fn install(path: &Path) -> Result<AppDetails, AnvilError> {
    let prepared = apk_set::prepare(&[path.to_owned()])?;
    if let Some(rejected) = prepared.rejected.first() {
        return Err(AnvilError::InvalidArgument(format!("{}: {}", rejected.file, rejected.reason)));
    }
    match prepared.packages.as_slice() {
        [set] => install_set(set),
        _ => Err(AnvilError::InvalidArgument("The file holds more than one app. Install it from the app window.".into())),
    }
}

/// Install the APKs of one package as one Android session and verify it.
pub fn install_set(set: &PackageSet) -> Result<AppDetails, AnvilError> {
    if let Some(error) = &set.manifest_error {
        return Err(AnvilError::InvalidArgument(format!("Invalid APK: {error}")));
    }
    // `prepare` refuses these too; keep the rule where the install happens.
    if backend::uninstall_block_reason(&set.package).is_some() {
        return Err(AnvilError::InvalidArgument(
            "Runtime components must be updated through the runtime installer.".into(),
        ));
    }
    launch::ensure_ready(launch::BOOT_TIMEOUT)?;
    let deadline = Instant::now() + SETTLE_WAIT;
    settle(
        || request("installs", "android", &[]),
        || {
            std::thread::sleep(SETTLE_PAUSE);
            Instant::now() < deadline
        },
    );
    let deadline = Instant::now() + RESTART_WAIT;
    let response = retry_after_restart(
        || request("install", &set.package, &set.parts),
        || {
            std::thread::sleep(RESTART_PAUSE);
            Instant::now() < deadline
        },
    )?;
    if !response.lines().any(|s| s.trim() == "Success") {
        return Err(AnvilError::BackendFailed(format!(
            "Installation was not confirmed: {}",
            response.trim()
        )));
    }
    let info = details(&set.package)?;
    if !info.installed || info.version_code != Some(set.version_code) {
        return Err(AnvilError::BackendFailed(
            "Installed package/version does not match the APK. Refresh Android App Info."
                .into(),
        ));
    }
    Ok(info)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn install_is_repeated_only_while_android_restarts() {
        let failure = |text: &str| Err(AnvilError::BackendFailed(format!("Android install failed: {text}")));
        // The package service dies, is missing while Android restarts, then answers.
        let mut replies = vec![
            failure("cmd: Failure calling service package: Broken pipe (32)"),
            failure("cmd: Can't find service: package"),
            Ok("Success".to_string()),
        ].into_iter();
        let mut pauses = 0;
        let result = retry_after_restart(|| replies.next().unwrap(), || { pauses += 1; true });
        assert_eq!(result.unwrap(), "Success");
        assert_eq!(pauses, 2);
        // A real rejection is reported at once.
        let attempts = std::cell::Cell::new(0);
        let result = retry_after_restart(
            || { attempts.set(attempts.get() + 1); failure("Failure [INSTALL_FAILED_VERSION_DOWNGRADE]") },
            || panic!("must not wait"),
        );
        assert!(result.unwrap_err().to_string().contains("VERSION_DOWNGRADE"));
        assert_eq!(attempts.get(), 1);
        // A timeout may still complete in Android, so it is never repeated.
        let result = retry_after_restart::<String>(
            || Err(AnvilError::Timeout("Android install timed out".into())),
            || panic!("must not wait"),
        );
        assert!(matches!(result, Err(AnvilError::Timeout(_))));
        // Android never comes back: stop at the deadline and say what happened.
        let attempts = std::cell::Cell::new(0);
        let result = retry_after_restart(
            || { attempts.set(attempts.get() + 1); failure("cmd: Can't find service: package") },
            || attempts.get() < 3,
        );
        let message = result.unwrap_err().to_string();
        assert!(message.contains("Android restarted during the installation"), "{message}");
        assert!(message.contains("Can't find service: package"));
        assert_eq!(attempts.get(), 3);
    }
    /// `dumpsys package installs` as Android 13 prints it, cut to what is read.
    fn sessions(list: &[(u32, &str)]) -> String {
        let mut text = String::from("package:/system/framework/framework-res.apk\nActive install sessions:\n");
        for (id, state) in list {
            text.push_str(&format!("  Session {id}:\n    userId=0 mOriginalInstallerUid=10123 installerPackageName=com.android.vending \
                createdMillis=1763280000000 stageDir=/data/app/vmdl{id}.tmp stageCid=null\n    mode=1 installFlags=0x400002\n    \
                mClientProgress=1.0 mProgress=0.8 {state} mFds=0 mBridges=0 mFinalStatus=0 mFinalMessage=null\n\n"));
        }
        text.push_str("\nHistorical install sessions:\n  Session 7:\n    mCommitted=true mSealed=true mDestroyed=false\n\nLegacy install sessions:\n  {}\n");
        text
    }
    #[test]
    fn only_a_session_being_committed_counts_as_an_install_in_flight() {
        assert!(install_in_flight(&sessions(&[(11, "mCommitted=true mSealed=true mDestroyed=false")])));
        // A second session is found behind one that is only being downloaded.
        assert!(install_in_flight(&sessions(&[(11, "mCommitted=false mSealed=false mDestroyed=false"),
            (12, "mCommitted=true mSealed=true mDestroyed=false")])));
        // Android releases without mCommitted mark the same moment with mSealed.
        assert!(install_in_flight(&sessions(&[(11, "mSealed=true mDestroyed=false")])));
        for idle in [vec![], vec![(11, "mCommitted=false mSealed=false mDestroyed=false")],
            vec![(11, "mCommitted=false mSealed=true mDestroyed=false")],
            vec![(11, "mCommitted=true mSealed=true mDestroyed=true")], vec![(11, "mFds=0")]] {
            assert!(!install_in_flight(&sessions(&idle)), "{idle:?}");
        }
        // Finished sessions, other dumps and an Android that is restarting say nothing.
        assert!(!install_in_flight("Historical install sessions:\n  Session 7:\n    mCommitted=true\n"));
        assert!(!install_in_flight("Session 7:\n    mCommitted=true\n"));
        assert!(!install_in_flight("cmd: Can't find service: package\nCan't find service: package\n"));
        assert!(!install_in_flight(""));
    }
    /// Taken from Android 13 after a system process restart interrupted an install.
    const REAL_DUMP: &str = "Active install sessions:\n  Active Session 1002949385:\n    userId=0 mOriginalInstallerUid=2000 mInstallerUid=2000 \n    createdMillis=1794828979946 updatedMillis=1794828979946 committedMillis=1794828980147 \n    stageDir=/data/app/vmdl1002949385.tmp stageCid=null \n    mClientProgress=0.0 mProgress=0.0 mCommitted=true mSealed=true mPermissionsManuallyAccepted=false \n    mStageDirInUse=false mDestroyed=false mFds=0 mBridges=0 mFinalStatus=0 mFinalMessage=null \n  \nFinalized install sessions:\n\nHistorical install sessions:\n  Session 896581799:\n      mClientProgress=1.0 mProgress=0.9 mCommitted=true mSealed=true\n";

    #[test]
    fn android_13_session_headers_are_read_and_leftover_sessions_are_ignored() {
        let committed = 1_794_828_980_147u128;
        // Seconds after the commit the install is in flight.
        assert!(install_in_flight_at(REAL_DUMP, committed + 5_000));
        assert!(install_in_flight_at(REAL_DUMP, committed + INSTALL_SESSION_STALE_MS - 1));
        // Hours later the same listing is a leftover of the restart.
        assert!(!install_in_flight_at(REAL_DUMP, committed + INSTALL_SESSION_STALE_MS));
        assert!(!install_in_flight_at(REAL_DUMP, committed + 19_000_000));
        // A clock that reads earlier than the commit never hides real work.
        assert!(install_in_flight_at(REAL_DUMP, committed - 60_000));
        // Without a commit time the session still counts.
        assert!(install_in_flight_at("Active install sessions:\n  Active Session 5:\n    mCommitted=true mDestroyed=false\n", committed));
        assert!(install_in_flight_at("Active install sessions:\n  Active Session 5:\n    committedMillis=0 mCommitted=true\n", committed));
    }

    #[test]
    fn install_waits_for_the_package_service_and_for_an_install_in_flight() {
        let busy = sessions(&[(11, "mCommitted=true mSealed=true mDestroyed=false")]);
        let failure = |text: &str| Err(AnvilError::BackendFailed(format!("Android installs failed: {text}")));
        // Busy, then gone with the system process, then back with nothing in hand.
        let mut replies = vec![Ok(busy.clone()), Ok("cmd: Can't find service: package\n".to_string()),
            failure("cmd: Failure calling service package: Broken pipe (32)"), Ok(sessions(&[]))].into_iter();
        let mut pauses = 0;
        settle(|| replies.next().unwrap(), || { pauses += 1; true });
        assert_eq!((pauses, replies.next().is_none()), (3, true));
        // Nothing to wait for: one look, no pause.
        let mut looks = 0;
        settle(|| { looks += 1; Ok(sessions(&[])) }, || panic!("must not wait"));
        assert_eq!(looks, 1);
        // An older helper, or one that does not answer in time, never delays the install.
        settle(|| failure("Unsupported operation"), || panic!("must not wait"));
        settle(|| Err(AnvilError::Timeout("Android installs timed out".into())), || panic!("must not wait"));
        // A long install of Android's own is waited for only as long as allowed.
        let looks = std::cell::Cell::new(0);
        settle(|| { looks.set(looks.get() + 1); Ok(busy.clone()) }, || looks.get() < 4);
        assert_eq!(looks.get(), 4);
    }
    #[test]
    fn install_jobs_stage_one_apk_or_a_numbered_split_set() {
        let directory = apk_set::tests::scratch("apps-queue");
        let base = directory.join("apps");
        fs::create_dir(&base).unwrap();
        fs::write(directory.join("pack"), b"BASE-SPLIT-ONE-SPLIT-TWO").unwrap();
        let pack = std::sync::Arc::new(fs::File::open(directory.join("pack")).unwrap());
        let part = |offset, size| Part::new(pack.clone(), offset, size, None).unwrap();
        // Play the in-Android helper: take the job, record its files, answer.
        let helper = |reply: &'static str, status: &'static str| {
            let base = base.clone();
            std::thread::spawn(move || loop {
                let ready = fs::read_dir(&base).unwrap().flatten()
                    .find(|entry| entry.file_name().to_string_lossy().ends_with(".ready"));
                let Some(ready) = ready else { std::thread::sleep(Duration::from_millis(5)); continue };
                let working = ready.path().with_extension("working");
                fs::rename(ready.path(), &working).unwrap();
                let mut files: Vec<(String, String)> = fs::read_dir(&working).unwrap().flatten()
                    .map(|entry| (entry.file_name().to_string_lossy().into_owned(), fs::read_to_string(entry.path()).unwrap()))
                    .collect();
                files.sort();
                fs::write(working.join("output"), reply).unwrap();
                fs::write(working.join("status"), status).unwrap();
                return files;
            })
        };
        let files = |list: &[(&str, &str)]| list.iter().map(|(name, text)| (name.to_string(), text.to_string())).collect::<Vec<_>>();
        let job = helper("Success\n", "0\n");
        assert_eq!(request_in(&base, "install", "com.example.game", &[part(5, 9)]).unwrap(), "Success\n");
        assert_eq!(job.join().unwrap(), files(&[("action", "install\n"), ("input.apk", "SPLIT-ONE"), ("package", "com.example.game\n")]));
        let job = helper("Success\n", "0\n");
        request_in(&base, "install", "com.example.game", &[part(0, 4), part(5, 9), part(15, 9)]).unwrap();
        assert_eq!(job.join().unwrap(), files(&[("action", "install\n"), ("count", "3\n"), ("input-0.apk", "BASE"), ("input-1.apk", "SPLIT-ONE"),
            ("input-2.apk", "SPLIT-TWO"), ("package", "com.example.game\n")]));
        // The job folder is removed once the answer is read.
        assert_eq!(fs::read_dir(&base).unwrap().count(), 0);
        let job = helper("Failure [INSTALL_FAILED_MISSING_SPLIT]\n", "1\n");
        let error = request_in(&base, "install", "com.example.game", &[part(0, 4), part(5, 9)]).unwrap_err();
        assert!(error.to_string().contains("INSTALL_FAILED_MISSING_SPLIT"));
        job.join().unwrap();
        // A helper from before split support exits 2 without output.
        let job = helper("", "2\n");
        let error = request_in(&base, "install", "com.example.game", &[part(0, 4), part(5, 9)]).unwrap_err();
        assert!(error.to_string().contains("cannot install split packages yet"), "{error}");
        job.join().unwrap();
        // A part that is shorter than announced never reaches Android.
        assert!(request_in(&base, "install", "com.example.game", &[part(0, 4), part(20, 9)]).is_err());
        assert_eq!(fs::read_dir(&base).unwrap().count(), 0);
        assert!(request_in(&base, "install", "bad;name", &[]).is_err());
        assert_eq!(install_timeout(1024), 120);
        assert_eq!(install_timeout(1024 * 1024 * 1024), 632);
        assert_eq!(install_timeout(apk_set::MAX_SET), 900);
        fs::remove_dir_all(directory).unwrap();
    }
    #[test]
    fn metadata_and_user_state() {
        let text = "Packages:\n  Package [com.example.test] (abc):\n    versionCode=23 minSdk=23\n    versionName=2.3\n    primaryCpuAbi=null\n    User 0: installed=true hidden=false stopped=true\n";
        let d = parse_details("com.example.test", text);
        assert!(d.installed);
        assert_eq!(d.version_code, Some(23));
        assert_eq!(d.abi, None);
        assert_eq!(d.stopped, Some(true));
        assert!(!parse_details("com.other.app", text).installed);
        assert!(
            !parse_details(
                "com.example.test",
                &text.replace("installed=true", "installed=false")
            )
            .installed
        );
    }
    #[test]
    fn manifest_identity() {
        assert_eq!(
            apk_identity("package: name='com.example.test' versionCode='12' versionName='x'")
                .unwrap(),
            ("com.example.test".into(), 12)
        );
        assert!(apk_identity("package: name='bad;cmd' versionCode='12'").is_err());
        assert!(apk_identity("garbage").is_err());
    }
}
