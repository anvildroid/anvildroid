//! Narrow client for the root-owned controller. No caller-selected socket or UID.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::io::{BufRead, BufReader, Write};
use std::os::fd::AsRawFd;
use std::os::unix::net::UnixStream;
use std::time::Duration;

#[derive(Debug, Deserialize, Serialize)]
#[serde(tag = "op", rename_all = "lowercase", deny_unknown_fields)]
pub enum Request {
    List {},
    #[serde(rename = "device_info")]
    DeviceInfo { id: String },
    #[serde(rename = "device_set")]
    DeviceSet { id: String, profile: String, custom: Option<DeviceIdentity>, revision: String },
    #[serde(rename = "compatibility_get")]
    CompatibilityGet { id: String, package: String },
    #[serde(rename = "compatibility_set")]
    CompatibilitySet { id: String, package: String, settings: CompatibilitySettings, revision: String },
    #[serde(rename = "compatibility_reset")]
    CompatibilityReset { id: String, package: String, revision: String },
    #[serde(rename = "shares_get")]
    SharesGet { id: String },
    #[serde(rename = "shares_set")]
    SharesSet { id: String, name: String, source: String, read_only: bool },
    #[serde(rename = "shares_remove")]
    SharesRemove { id: String, name: String },
    #[serde(rename = "components_get")]
    ComponentsGet { id: String },
    #[serde(rename = "components_set")]
    ComponentsSet { id: String, component_id: String, enabled: bool, archive_sha256: Option<String>, revision: String },
    #[serde(rename = "diagnostics_collect")]
    DiagnosticsCollect { id: String, include_logs: bool },
    /// Start the caller's start-on-login runtimes in the caller's desktop session.
    #[serde(rename = "login_start")]
    LoginStart {},
    /// Long-poll until the caller's runtime view changes (or `timeout` seconds).
    Watch { since: Option<String>, timeout: u32 },
    #[serde(rename = "notifications_get")]
    NotificationsGet { id: String },
    #[serde(rename = "notifications_set")]
    NotificationsSet { id: String, enabled: bool },
    #[serde(rename = "notifications_app_set")]
    NotificationsAppSet { id: String, package: String, allowed: bool },
    #[serde(rename = "camera_get")]
    CameraGet { id: String },
    #[serde(rename = "camera_set")]
    CameraSet { id: String, enabled: bool, devices: Vec<String> },
    /// Backups of a device's Android data. Archives are named by ID only:
    /// the controller chooses every path.
    #[serde(rename = "backups_get")]
    BackupsGet { id: String },
    #[serde(rename = "backup_create")]
    BackupCreate { id: String },
    #[serde(rename = "backup_restore")]
    BackupRestore { id: String, backup_id: String, allow_image_mismatch: bool },
    #[serde(rename = "backup_delete")]
    BackupDelete { id: String, backup_id: String },
    #[serde(rename = "backup_cancel")]
    BackupCancel { id: String },
    /// Raw process counters, the app in front and its frame counters of a
    /// running device. Takes no arguments: Android names the app itself.
    #[serde(rename = "performance_get")]
    PerformanceGet { id: String },
    /// HTTP proxy and Private DNS of a running device, kept in Android's own
    /// settings. An empty `proxy_host` with port 0 and no bypass means no proxy.
    #[serde(rename = "network_get")]
    NetworkGet { id: String },
    #[serde(rename = "network_set")]
    NetworkSet {
        id: String,
        proxy_host: String,
        proxy_port: u16,
        proxy_bypass: Vec<String>,
        dns_mode: String,
        dns_hostname: String,
    },
    /// Recent Android log lines. `since` is the cursor of the previous answer.
    #[serde(rename = "logs_get")]
    LogsGet { id: String, lines: u32, level: String, package: Option<String>, since: Option<String> },
    /// PNG of the whole Android display, written to the one descriptor sent
    /// with the request (see `screenshot`). Never carries a path.
    #[serde(rename = "screenshot_save")]
    ScreenshotSave { id: String },
    #[serde(rename = "adb_info")]
    AdbInfo { id: String },
    #[serde(rename = "adb_set")]
    AdbSet { id: String, enabled: bool },
    #[serde(rename = "existing_claim")]
    ExistingClaim {
        id: String,
    },
    #[serde(rename = "system_integration")]
    SystemIntegration {
        id: String,
    },
    #[serde(rename = "existing_import")]
    ExistingImport {
        id: String,
        name: String,
    },
    #[serde(rename = "start_on_boot")]
    StartOnBoot {
        id: String,
        enabled: bool,
    },
    Delete {
        id: String,
        confirmed_name: String,
    },
    Install {
        id: String,
        /// Sizes of the APKs of one split package, laid end to end in the file.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        parts: Option<Vec<u64>>,
    },
    #[serde(rename = "display_info")]
    DisplayInfo {
        id: String,
    },
    #[serde(rename = "display_set")]
    DisplaySet {
        id: String,
        mode: String,
    },
    #[serde(rename = "backend_set")]
    BackendSet {
        id: String,
        backend: String,
        presenter: String,
    },
    #[serde(rename = "arm_info")]
    ArmInfo {
        id: String,
    },
    #[serde(rename = "arm_sources")]
    ArmSources {},
    #[serde(rename = "arm_set")]
    ArmSet {
        id: String,
        enabled: bool,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        archive_sha256: Option<String>,
    },
    #[serde(rename = "gpu_info")]
    GpuInfo {
        id: String,
    },
    #[serde(rename = "gpu_set")]
    GpuSet {
        id: String,
        node: String,
    },
    #[serde(rename = "resources_info")]
    ResourcesInfo {
        id: String,
    },
    #[serde(rename = "resources_set")]
    ResourcesSet {
        id: String,
        memory_mib: u64,
        cpu_count: u64,
    },
    Requirements {},
    #[serde(rename = "image_catalog")]
    ImageCatalog {
        refresh: bool,
    },
    #[serde(rename = "image_download")]
    ImageDownload {
        flavor: String,
        system_sha256: String,
        vendor_sha256: String,
        connections: u8,
    },
    #[serde(rename = "image_download_status")]
    ImageDownloadStatus {},
    #[serde(rename = "data_location")]
    DataLocation {},
    /// `path: None` returns new devices to the controller folder.
    #[serde(rename = "data_location_set")]
    DataLocationSet {
        path: Option<String>,
    },
    #[serde(rename = "image_cache_move")]
    ImageCacheMove {},
    #[serde(rename = "image_select")]
    ImageSelect {
        image_id: String,
    },
    #[serde(rename = "image_delete")]
    ImageDelete {
        image_id: String,
    },
    #[serde(rename = "image_cache_clear")]
    ImageCacheClear {},
    #[serde(rename = "image_rename")]
    ImageRename {
        image_id: String,
        name: String,
    },
    #[serde(rename = "image_download_cancel")]
    ImageDownloadCancel {
        job_id: String,
    },
    #[serde(rename = "google_services")]
    GoogleServices {
        id: String,
    },
    #[serde(rename = "app_action")]
    AppAction {
        id: String,
        action: String,
        package: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        activation_token: Option<String>,
        /// Android user of an app clone profile; absent for the original app.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        user: Option<u32>,
        /// Clone display name for `clone`/`clone_rename`; empty picks "Label (n)".
        #[serde(default, skip_serializing_if = "Option::is_none")]
        name: Option<String>,
    },
    #[serde(rename = "app_job")]
    AppJob {
        id: String,
    },
    Apps {
        id: String,
    },
    #[serde(rename = "app_states")]
    AppStates {
        id: String,
    },
    Keymaps {
        id: String,
    },
    #[serde(rename = "keymap_scheme")]
    KeymapScheme {
        id: String,
        package: String,
        action: String,
        scheme_id: String,
        name: String,
        revision: String,
        content: String,
    },
    Health {
        id: String,
    },
    #[serde(rename = "storage_info")]
    StorageInfo {
        id: String,
    },
    Move {
        id: String,
        destination: String,
    },
    #[serde(rename = "discard_previous")]
    DiscardPrevious {
        id: String,
        confirmed_name: String,
    },
    Create {
        name: String,
    },
    #[serde(rename = "create_image")]
    CreateImage {
        name: String,
        flavor: String,
        system_sha256: String,
        vendor_sha256: String,
    },
    #[serde(rename = "image_import")]
    ImageImport { name: String },
    #[serde(rename = "image_import_folder")]
    ImageImportFolder { name: String },
    #[serde(rename = "arm_import")]
    ArmImport {},
    Rename {
        id: String,
        name: String,
    },
    Prepare {
        id: String,
    },
    Reinstall {
        id: String,
        confirmed_name: String,
        flavor: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        system_sha256: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        vendor_sha256: Option<String>,
    },
    Start {
        id: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        display: Option<String>,
    },
    Stop {
        id: String,
    },
    Refresh {
        id: String,
    },
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DeviceIdentity {
    pub brand: String,
    pub manufacturer: String,
    pub model: String,
    pub device: String,
    pub product: String,
    pub fingerprint: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CompatibilitySettings {
    pub fake_wifi: bool,
    pub mouse_as_touch: bool,
}

fn display_basename(value: &str, uid: u32) -> Option<String> {
    let path = std::path::Path::new(value);
    if path.is_absolute() {
        let expected = std::path::PathBuf::from(format!("/run/user/{uid}"));
        if path.parent() != Some(expected.as_path()) {
            return None;
        }
    } else if path.components().count() != 1 {
        return None;
    }
    let name = path.file_name()?.to_str()?;
    if !name.starts_with("wayland-")
        || !name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_.".contains(&b))
    {
        return None;
    }
    Some(name.to_owned())
}

pub(crate) fn x11_display(value: &str) -> Option<String> {
    if value.len() > 64 {
        return None;
    }
    let path = std::path::Path::new(value);
    if path.is_absolute() {
        let name = path.file_name()?.to_str()?;
        let number = name.strip_prefix('X')?;
        return (path.parent() == Some(std::path::Path::new("/tmp/.X11-unix"))
            && !number.is_empty()
            && number.bytes().all(|b| b.is_ascii_digit()))
        .then(|| value.to_owned());
    }
    let rest = value
        .strip_prefix("localhost:")
        .or_else(|| value.strip_prefix("unix:"))
        .or_else(|| value.strip_prefix(':'))?;
    let mut parts = rest.split('.');
    let display_number = parts.next()?;
    if display_number.is_empty() || !display_number.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    match parts.next() {
        None => Some(value.to_owned()),
        Some(screen) if !screen.is_empty()
            && screen.bytes().all(|b| b.is_ascii_digit())
            && parts.next().is_none() =>
        {
            Some(value.to_owned())
        }
        _ => None,
    }
}

fn session_display(wayland: Option<&str>, display: Option<&str>, uid: u32) -> Option<String> {
    if let Some(value) = wayland {
        if let Some(name) = display_basename(value, uid) {
            return Some(name);
        }
    }
    if let Some(value) = display {
        if x11_display(value).is_some() {
            return Some(format!("x11:{value}"));
        }
    }
    None
}

pub fn desktop_session_display() -> Option<String> {
    // getuid reads the GUI caller's identity; it does not change credentials.
    session_display(
        std::env::var("WAYLAND_DISPLAY").ok().as_deref(),
        std::env::var("DISPLAY").ok().as_deref(),
        unsafe { libc::getuid() },
    )
}

thread_local! {
    static LAST_ERROR_CODE: std::cell::RefCell<Option<String>> = const { std::cell::RefCell::new(None) };
}

/// Controller error code (NOT_FOUND, BUSY, INVALID_STATE, ...) of the last
/// failed request on this thread; `None` for success or transport errors.
pub fn last_error_code() -> Option<String> {
    LAST_ERROR_CODE.with(|code| code.borrow().clone())
}

pub fn request(request: Request) -> Result<Value, String> {
    LAST_ERROR_CODE.with(|code| *code.borrow_mut() = None);
    let socket = UnixStream::connect("/run/anvildroid/control.sock")
        .map_err(|e| format!("Runtime controller unavailable: {e}"))?;
    exchange(socket, request).map_err(|e| format!("Runtime controller: {e}"))
}

fn exchange(socket: UnixStream, request: Request) -> Result<Value, String> {
    exchange_file(socket, request, None)
}

pub fn install(id: &str, path: &std::path::Path) -> Result<Value, String> {
    if !crate::library::valid_managed_runtime(id) {
        return Err("Invalid runtime ID".into());
    }
    use std::os::unix::fs::OpenOptionsExt;
    let file = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NONBLOCK)
        .open(path)
        .map_err(|e| format!("Cannot read APK: {e}"))?;
    let metadata = file.metadata().map_err(|e| e.to_string())?;
    if !metadata.is_file() || metadata.len() == 0 || metadata.len() > 2 * 1024 * 1024 * 1024 {
        return Err("APK must be a regular file between 1 byte and 2 GiB".into());
    }
    let socket = UnixStream::connect("/run/anvildroid/control.sock")
        .map_err(|e| format!("Runtime controller unavailable: {e}"))?;
    exchange_file(socket, Request::Install { id: id.into(), parts: None }, Some(&file))
}

/// Install one package from a caller-opened file that holds its APKs end to
/// end. `sizes` marks where each APK ends; several APKs form one session.
pub fn install_parts(id: &str, file: &std::fs::File, sizes: &[u64]) -> Result<Value, String> {
    if !crate::library::valid_managed_runtime(id) {
        return Err("Invalid runtime ID".into());
    }
    let metadata = file.metadata().map_err(|e| e.to_string())?;
    let total = sizes.iter().try_fold(0u64, |total, size| total.checked_add(*size));
    if !metadata.is_file() || sizes.is_empty() || sizes.len() > crate::apk_set::MAX_PARTS
        || sizes.iter().any(|size| *size == 0 || *size > crate::apk_set::MAX_APK)
        || total != Some(metadata.len()) || metadata.len() > crate::apk_set::MAX_SET
    {
        return Err("A package may hold up to 512 APKs of at most 2 GiB each and 4 GiB in total".into());
    }
    let socket = UnixStream::connect("/run/anvildroid/control.sock")
        .map_err(|e| format!("Runtime controller unavailable: {e}"))?;
    // One APK keeps the request every controller and worker version accepts.
    let parts = (sizes.len() > 1).then(|| sizes.to_vec());
    exchange_file(socket, Request::Install { id: id.into(), parts }, Some(file))
}

pub fn import_image(path: &std::path::Path, name: &str) -> Result<Value, String> {
    use std::os::unix::fs::OpenOptionsExt;
    let file = std::fs::OpenOptions::new().read(true).custom_flags(libc::O_NONBLOCK)
        .open(path).map_err(|e| format!("Cannot read custom image ZIP: {e}"))?;
    let metadata = file.metadata().map_err(|e| e.to_string())?;
    if !metadata.is_file() || metadata.len() == 0 || metadata.len() > 32 * 1024 * 1024 * 1024 {
        return Err("Custom image ZIP must be a regular file up to 32 GiB".into());
    }
    let socket = UnixStream::connect("/run/anvildroid/control.sock")
        .map_err(|e| format!("Runtime controller unavailable: {e}"))?;
    exchange_file(socket, Request::ImageImport { name: name.to_owned() }, Some(&file))
}

pub fn import_image_folder(path: &std::path::Path, name: &str) -> Result<Value, String> {
    use std::os::unix::fs::OpenOptionsExt;
    let mut files = Vec::new();
    for name in ["system.img", "vendor.img"] {
        let file = std::fs::OpenOptions::new().read(true)
            .custom_flags(libc::O_NONBLOCK | libc::O_NOFOLLOW).open(path.join(name))
            .map_err(|e| format!("Cannot read {name} in selected folder: {e}"))?;
        let metadata = file.metadata().map_err(|e| e.to_string())?;
        if !metadata.is_file() || metadata.len() == 0 || metadata.len() > 16 * 1024 * 1024 * 1024 {
            return Err(format!("{name} must be a regular raw image up to 16 GiB"));
        }
        files.push(file);
    }
    let socket = UnixStream::connect("/run/anvildroid/control.sock")
        .map_err(|e| format!("Runtime controller unavailable: {e}"))?;
    exchange_files(socket, Request::ImageImportFolder { name: name.to_owned() }, &[&files[0], &files[1]])
}

pub fn import_arm_source(kind: &str, source: &str, expected_sha256: &str) -> Result<Value, String> {
    use std::os::fd::FromRawFd;
    use std::process::{Command, Stdio};
    use std::io::{Seek, SeekFrom};
    if !matches!(kind, "builtin" | "url" | "folder" | "archive") {
        return Err("Unsupported ARM translation source".into());
    }
    // Anonymous, user-owned output; no temporary path crosses the root boundary.
    let descriptor = unsafe { libc::memfd_create(c"anvildroid-arm-source".as_ptr(), libc::MFD_CLOEXEC) };
    if descriptor < 0 { return Err(std::io::Error::last_os_error().to_string()); }
    let mut file = unsafe { std::fs::File::from_raw_fd(descriptor) };
    let output = Command::new("/usr/bin/python3")
        .args(["-I", "-c", include_str!("../../../services/runtime-arm-source.py"), kind, source, expected_sha256])
        .stdin(Stdio::null()).stdout(Stdio::from(file.try_clone().map_err(|e| e.to_string())?))
        .stderr(Stdio::piped()).output().map_err(|e| format!("Cannot collect ARM source: {e}"))?;
    if !output.status.success() {
        return Err(format!("Cannot collect ARM source: {}", String::from_utf8_lossy(&output.stderr).trim()));
    }
    file.seek(SeekFrom::Start(0)).map_err(|e| format!("Cannot rewind ARM source: {e}"))?;
    let metadata = file.metadata().map_err(|e| e.to_string())?;
    if !metadata.is_file() || metadata.len() == 0 || metadata.len() > 256 * 1024 * 1024 {
        return Err("ARM translation archive must be a regular file up to 256 MiB".into());
    }
    let socket = UnixStream::connect("/run/anvildroid/control.sock")
        .map_err(|e| format!("Runtime controller unavailable: {e}"))?;
    exchange_file(socket, Request::ArmImport {}, Some(&file))
}

/// Capture the whole Android display of a running device as PNG into a file
/// the caller opened for writing. Only the descriptor reaches the root
/// controller, which checks the image before it writes anything.
pub fn screenshot(id: &str, file: &std::fs::File) -> Result<Value, String> {
    if id != crate::scope::LEGACY_ID && !crate::library::valid_managed_runtime(id) {
        return Err("Invalid runtime ID".into());
    }
    if !file.metadata().map_err(|e| e.to_string())?.is_file() {
        return Err("The screenshot destination must be a regular file".into());
    }
    let socket = UnixStream::connect("/run/anvildroid/control.sock")
        .map_err(|e| format!("Runtime controller unavailable: {e}"))?;
    exchange_file(socket, Request::ScreenshotSave { id: id.into() }, Some(file))
}

/// Save a screenshot of `id` at `path`. With `overwrite` an existing file is
/// replaced (the caller already asked the user); without it the file must be new.
pub fn screenshot_to(id: &str, path: &std::path::Path, overwrite: bool) -> Result<Value, String> {
    let mut result = save_screenshot(path, overwrite, |file| screenshot(id, file))?;
    if result["runtime_id"].as_str() != Some(id) {
        // Already published under the chosen name: do not leave another device's picture there.
        let _ = std::fs::remove_file(path);
        return Err("The screenshot came from another device; nothing was saved. Try again.".into());
    }
    result["path"] = path.to_string_lossy().into();
    Ok(result)
}

/// The picture is written to a new hidden file next to the target and only a
/// complete one gets the chosen name, so a failed capture leaves nothing behind
/// and never damages an existing file.
fn save_screenshot(
    path: &std::path::Path,
    overwrite: bool,
    capture: impl FnOnce(&std::fs::File) -> Result<Value, String>,
) -> Result<Value, String> {
    use std::os::unix::fs::OpenOptionsExt;
    struct Remove(std::path::PathBuf);
    impl Drop for Remove {
        fn drop(&mut self) { let _ = std::fs::remove_file(&self.0); }
    }
    let name = path.file_name().and_then(|name| name.to_str()).ok_or("Choose a file name for the screenshot")?;
    let folder = path.parent().filter(|folder| !folder.as_os_str().is_empty()).unwrap_or(std::path::Path::new("."));
    let exists = || format!("{} already exists. Choose another file name.", path.display());
    match path.symlink_metadata() {
        Ok(found) if found.is_dir() => return Err(format!("{} is a folder. Choose a file name.", path.display())),
        Ok(_) if !overwrite => return Err(exists()),
        _ => {}
    }
    let (file, temporary) = (0..16).find_map(|attempt| {
        let temporary = folder.join(format!(".{name}.{}-{attempt}.part", std::process::id()));
        match std::fs::OpenOptions::new().write(true).create_new(true).custom_flags(libc::O_NOFOLLOW).open(&temporary) {
            Ok(file) => Some(Ok((file, temporary))),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => None,
            Err(error) => Some(Err(format!("Cannot create a file in {}: {error}", folder.display()))),
        }
    }).unwrap_or_else(|| Err(format!("Cannot create a temporary file in {}", folder.display())))?;
    let temporary = Remove(temporary);
    let result = capture(&file)?;
    let written = file.metadata().map_err(|e| e.to_string())?.len();
    if written == 0 || result["bytes"].as_u64() != Some(written) {
        return Err("The screenshot was not written completely; nothing was saved. Try again.".into());
    }
    if overwrite {
        std::fs::rename(&temporary.0, path).map_err(|e| format!("Cannot save {}: {e}", path.display()))?;
    } else if let Err(error) = std::fs::hard_link(&temporary.0, path) {
        // A link never replaces a file. Filesystems without links fall back to a rename.
        if error.kind() == std::io::ErrorKind::AlreadyExists || path.symlink_metadata().is_ok() {
            return Err(exists());
        }
        std::fs::rename(&temporary.0, path).map_err(|e| format!("Cannot save {}: {e}", path.display()))?;
    }
    Ok(result)
}

fn exchange_file(socket: UnixStream, request: Request, file: Option<&std::fs::File>) -> Result<Value, String> {
    let files: Vec<_> = file.into_iter().collect();
    exchange_files(socket, request, &files)
}

fn exchange_files(
    mut socket: UnixStream,
    request: Request,
    files: &[&std::fs::File],
) -> Result<Value, String> {
    let mut credentials = libc::ucred {
        pid: 0,
        uid: u32::MAX,
        gid: u32::MAX,
    };
    let mut length = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    // The buffer and its size remain valid for the entire getsockopt call.
    let result = unsafe {
        libc::getsockopt(
            socket.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            (&mut credentials as *mut libc::ucred).cast(),
            &mut length,
        )
    };
    if result != 0 || length as usize != std::mem::size_of::<libc::ucred>() || credentials.uid != 0
    {
        return Err("Refusing endpoint not owned by root".into());
    }
    // ARM staging fsyncs hundreds of library files before publishing a version.
    let reply_timeout = if let Request::Watch { timeout, .. } = &request {
        u64::from((*timeout).min(25)) + 10
    } else if matches!(&request, Request::ArmImport {} | Request::ArmSet { .. } | Request::ComponentsSet { .. }) {
        180
    } else if matches!(&request, Request::CompatibilityGet { .. } | Request::CompatibilitySet { .. }
        | Request::CompatibilityReset { .. } | Request::DiagnosticsCollect { .. } | Request::ComponentsGet { .. }) {
        120
    } else if matches!(&request, Request::KeymapScheme { .. }) {
        25
    } else if matches!(&request, Request::SharesSet { .. }) {
        15
    } else if matches!(&request, Request::SystemIntegration { .. } | Request::BackendSet { .. }) {
        60
    } else if matches!(&request, Request::Reinstall { id, .. } if id == "default") {
        // System Waydroid reinstalls synchronously: image extraction and the
        // verified ARM package must finish before reporting completion.
        600
    } else if matches!(&request, Request::ScreenshotSave { .. }) {
        // Capture (15 s) plus the bounded copy to a possibly slow destination.
        45
    } else if matches!(&request, Request::NetworkSet { .. } | Request::LogsGet { .. }) {
        // Several `settings` calls, or `ps` plus `logcat`, on a busy device.
        20
    } else if matches!(&request, Request::PerformanceGet { .. } | Request::NetworkGet { .. }) {
        10
    } else {
        5
    };
    socket
        .set_read_timeout(Some(Duration::from_secs(reply_timeout)))
        .map_err(|e| e.to_string())?;
    socket
        .set_write_timeout(Some(Duration::from_secs(5)))
        .map_err(|e| e.to_string())?;
    let mut payload = serde_json::to_vec(&request).map_err(|e| e.to_string())?;
    if payload.len() >= 16384 {
        return Err("Request too large".into());
    }
    payload.push(b'\n');
    if !files.is_empty() {
        send_files(&mut socket, &payload, files)?;
    } else {
        socket.write_all(&payload).map_err(|e| e.to_string())?;
    }
    decode(BufReader::new(socket))
}

fn send_files(socket: &mut UnixStream, payload: &[u8], files: &[&std::fs::File]) -> Result<(), String> {
    let descriptor_bytes = std::mem::size_of::<libc::c_int>() * files.len();
    // cmsghdr-aligned storage; descriptors are sent only after root peer validation.
    let space = unsafe { libc::CMSG_SPACE(descriptor_bytes as _) } as usize;
    let mut control = vec![0usize; space.div_ceil(std::mem::size_of::<usize>())];
    let mut iov = libc::iovec {
        iov_base: payload.as_ptr() as *mut _,
        iov_len: payload.len(),
    };
    let mut message: libc::msghdr = unsafe { std::mem::zeroed() };
    message.msg_iov = &mut iov;
    message.msg_iovlen = 1;
    message.msg_control = control.as_mut_ptr().cast();
    message.msg_controllen = space;
    let sent = unsafe {
        let header = libc::CMSG_FIRSTHDR(&message);
        (*header).cmsg_level = libc::SOL_SOCKET;
        (*header).cmsg_type = libc::SCM_RIGHTS;
        (*header).cmsg_len = libc::CMSG_LEN(descriptor_bytes as _) as usize;
        for (index, file) in files.iter().enumerate() {
            std::ptr::write_unaligned(libc::CMSG_DATA(header).cast::<libc::c_int>().add(index), file.as_raw_fd());
        }
        libc::sendmsg(socket.as_raw_fd(), &message, libc::MSG_NOSIGNAL)
    };
    if sent < 0 {
        return Err(std::io::Error::last_os_error().to_string());
    }
    socket
        .write_all(&payload[sent as usize..])
        .map_err(|e| e.to_string())
}

fn decode(reader: impl BufRead) -> Result<Value, String> {
    let mut data = Vec::new();
    reader
        .take(4 * 1024 * 1024 + 1)
        .read_until(b'\n', &mut data)
        .map_err(|e| e.to_string())?;
    if data.len() > 4 * 1024 * 1024 || data.last() != Some(&b'\n') {
        return Err("Invalid or oversized response".into());
    }
    let response: Value = serde_json::from_slice(&data).map_err(|e| e.to_string())?;
    LAST_ERROR_CODE.with(|code| {
        *code.borrow_mut() = response.pointer("/error/code").and_then(Value::as_str).map(str::to_owned)
    });
    match response.get("ok").and_then(Value::as_bool) {
        Some(true) => response
            .get("result")
            .cloned()
            .ok_or_else(|| "Missing result".into()),
        Some(false) => Err(response
            .pointer("/error/message")
            .and_then(Value::as_str)
            .unwrap_or("Request failed")
            .to_owned()),
        None => Err("Invalid response envelope".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn feature_requests_roundtrip_and_reject_caller_privileged_fields() {
        let revision = "a".repeat(64);
        for value in [
            serde_json::json!({"op":"device_info","id":"default"}),
            serde_json::json!({"op":"device_set","id":"default","profile":"pixel-7","custom":null,"revision":revision}),
            serde_json::json!({"op":"device_set","id":"default","profile":"custom","custom":{"brand":"Test","manufacturer":"Test","model":"Test phone","device":"test","product":"test","fingerprint":""},"revision":revision}),
            serde_json::json!({"op":"compatibility_get","id":"default","package":"com.example.app"}),
            serde_json::json!({"op":"compatibility_set","id":"default","package":"com.example.app","settings":{"fake_wifi":true,"mouse_as_touch":false},"revision":revision}),
            serde_json::json!({"op":"compatibility_reset","id":"default","package":"com.example.app","revision":revision}),
            serde_json::json!({"op":"shares_get","id":"r-11111111111111111111111111111111"}),
            serde_json::json!({"op":"shares_set","id":"r-11111111111111111111111111111111","name":"docs","source":"/home/user/docs","read_only":true}),
            serde_json::json!({"op":"shares_remove","id":"r-11111111111111111111111111111111","name":"docs"}),
            serde_json::json!({"op":"components_get","id":"default"}),
            serde_json::json!({"op":"components_set","id":"default","component_id":"libndk","enabled":false,"archive_sha256":null,"revision":revision}),
            serde_json::json!({"op":"diagnostics_collect","id":"default","include_logs":false}),
        ] {
            let request: Request = serde_json::from_value(value.clone()).unwrap();
            assert_eq!(serde_json::to_value(request).unwrap(), value);
            for (key, privileged) in [("uid", serde_json::json!(0)), ("socket", serde_json::json!("/tmp/fake")), ("command", serde_json::json!("id"))] {
                let mut invalid = value.clone();
                invalid[key] = privileged;
                assert!(serde_json::from_value::<Request>(invalid).is_err());
            }
        }
    }
    #[test]
    fn feature_boolean_and_compatibility_settings_types_are_strict() {
        for value in [
            serde_json::json!({"op":"compatibility_set","id":"default","package":"com.example.app","settings":{"fake_wifi":1,"mouse_as_touch":false},"revision":"revision"}),
            serde_json::json!({"op":"compatibility_set","id":"default","package":"com.example.app","settings":{"fake_wifi":true},"revision":"revision"}),
            serde_json::json!({"op":"compatibility_set","id":"default","package":"com.example.app","settings":{"fake_wifi":true,"mouse_as_touch":false,"property":"ro.foo"},"revision":"revision"}),
            serde_json::json!({"op":"shares_set","id":"default","name":"docs","source":"/home/user/docs","read_only":"true"}),
            serde_json::json!({"op":"components_set","id":"default","component_id":"libndk","enabled":"false","archive_sha256":null,"revision":"revision"}),
            serde_json::json!({"op":"diagnostics_collect","id":"default","include_logs":"false"}),
        ] {
            assert!(serde_json::from_value::<Request>(value).is_err());
        }
    }

    #[test]
    fn scheme_request_is_typed_and_cannot_select_a_privileged_file_or_command() {
        let value = serde_json::json!({"op":"keymap_scheme","id":"default","package":"com.example.game",
            "action":"import","scheme_id":"","name":"Imported","revision":"a".repeat(64),"content":"{\"format\":\"anvildroid-scheme\"}"});
        let parsed: Request = serde_json::from_value(value.clone()).unwrap();
        assert_eq!(serde_json::to_value(parsed).unwrap(), value);
        for field in ["path", "socket", "uid", "command", "generation"] {
            let mut invalid = value.clone();
            invalid[field] = serde_json::json!("/etc/shadow");
            assert!(serde_json::from_value::<Request>(invalid).is_err());
        }
        let mut missing = value.clone();
        missing.as_object_mut().unwrap().remove("revision");
        assert!(serde_json::from_value::<Request>(missing).is_err());
        let mut wrong_type = value;
        wrong_type["content"] = serde_json::json!({"path":"/etc/shadow"});
        assert!(serde_json::from_value::<Request>(wrong_type).is_err());
    }

    #[test]
    fn window_activation_is_optional_and_preserved() {
        let base = serde_json::json!({"op":"app_action","id":"r-test","action":"full_ui","package":"org.anvildroid.desktop"});
        let request: Request = serde_json::from_value(base.clone()).unwrap();
        assert_eq!(serde_json::to_value(request).unwrap(), base);
        let mut token_request = base;
        token_request["activation_token"] = serde_json::json!("source-token");
        let request: Request = serde_json::from_value(token_request.clone()).unwrap();
        assert_eq!(serde_json::to_value(request).unwrap(), token_request);
    }
    #[test]
    fn app_clone_fields_round_trip_exactly() {
        for value in [
            serde_json::json!({"op":"app_action","id":"r-test","action":"clone","package":"com.example.game","name":""}),
            serde_json::json!({"op":"app_action","id":"r-test","action":"launch","package":"com.example.game","user":10}),
            serde_json::json!({"op":"app_action","id":"r-test","action":"clone_rename","package":"com.example.game","user":10,"name":"Alt"}),
        ] {
            let request: Request = serde_json::from_value(value.clone()).unwrap();
            assert_eq!(serde_json::to_value(request).unwrap(), value);
        }
        let negative = serde_json::json!({"op":"app_action","id":"r-test","action":"launch","package":"com.example.game","user":-1});
        assert!(serde_json::from_value::<Request>(negative).is_err());
    }
    #[test]
    fn image_download_carries_the_connection_count() {
        let wire = serde_json::json!({"op":"image_download","flavor":"VANILLA","system_sha256":"a","vendor_sha256":"b","connections":8});
        let request: Request = serde_json::from_value(wire.clone()).unwrap();
        assert_eq!(serde_json::to_value(request).unwrap(), wire);
        assert!(serde_json::from_str::<Request>(r#"{"op":"image_download","flavor":"VANILLA","system_sha256":"a","vendor_sha256":"b"}"#).is_err());
    }

    #[test]
    fn backup_requests_round_trip_and_never_carry_a_path() {
        let backup = "0123456789abcdef0123456789abcdef";
        for value in [
            serde_json::json!({"op":"backups_get","id":"default"}),
            serde_json::json!({"op":"backup_create","id":"r-11111111111111111111111111111111"}),
            serde_json::json!({"op":"backup_restore","id":"default","backup_id":backup,"allow_image_mismatch":false}),
            serde_json::json!({"op":"backup_delete","id":"r-11111111111111111111111111111111","backup_id":backup}),
            serde_json::json!({"op":"backup_cancel","id":"default"}),
        ] {
            let request: Request = serde_json::from_value(value.clone()).unwrap();
            assert_eq!(serde_json::to_value(request).unwrap(), value);
            for (key, privileged) in [("path", serde_json::json!("/etc")), ("destination", serde_json::json!("/tmp/x")),
                                      ("uid", serde_json::json!(0)), ("owner_uid", serde_json::json!(0))] {
                let mut invalid = value.clone();
                invalid[key] = privileged;
                assert!(serde_json::from_value::<Request>(invalid).is_err());
            }
        }
        for invalid in [
            serde_json::json!({"op":"backup_restore","id":"default","backup_id":backup}),
            serde_json::json!({"op":"backup_restore","id":"default","backup_id":backup,"allow_image_mismatch":"yes"}),
            serde_json::json!({"op":"backup_restore","id":"default","allow_image_mismatch":true}),
            serde_json::json!({"op":"backup_delete","id":"default"}),
            serde_json::json!({"op":"backup_create"}),
        ] {
            assert!(serde_json::from_value::<Request>(invalid).is_err());
        }
    }

    #[test]
    fn data_location_requests_round_trip_exactly() {
        for value in [
            serde_json::json!({"op":"data_location"}),
            serde_json::json!({"op":"data_location_set","path":"/mnt/data/AnvilDroid"}),
            serde_json::json!({"op":"data_location_set","path":null}),
            serde_json::json!({"op":"image_cache_move"}),
            serde_json::json!({"op":"image_cache_clear"}),
            serde_json::json!({"op":"image_delete","image_id":"abc"}),
        ] {
            let request: Request = serde_json::from_value(value.clone()).unwrap();
            assert_eq!(serde_json::to_value(request).unwrap(), value);
        }
        assert!(serde_json::from_str::<Request>(r#"{"op":"image_cache_move","destination":"/etc"}"#).is_err());
    }
    #[test]
    fn custom_import_uses_descriptor_not_privileged_path() {
        assert!(serde_json::from_str::<Request>(r#"{"op":"image_import","name":"Custom"}"#).is_ok());
        assert!(serde_json::from_str::<Request>(r#"{"op":"image_import","path":"/etc/shadow"}"#).is_err());
        assert_eq!(serde_json::to_value(Request::ImageImport { name: "Custom".into() }).unwrap(), serde_json::json!({"op":"image_import","name":"Custom"}));
        assert_eq!(serde_json::to_value(Request::ArmImport {}).unwrap(), serde_json::json!({"op":"arm_import"}));
    }
    #[test]
    fn existing_management_requests_reject_privileged_fields() {
        for request in [
            r#"{"op":"existing_claim","id":"default"}"#,
            r#"{"op":"system_integration","id":"default"}"#,
            r#"{"op":"existing_import","id":"default","name":"Managed copy"}"#,
        ] {
            assert!(serde_json::from_str::<Request>(request).is_ok());
        }
        for request in [
            r#"{"op":"existing_claim","id":"default","uid":0}"#,
            r#"{"op":"system_integration","id":"default","unit":"sshd.service"}"#,
            r#"{"op":"existing_import","id":"default","name":"Managed copy","source":"/etc"}"#,
        ] {
            assert!(serde_json::from_str::<Request>(request).is_err());
        }
    }
    #[test]
    fn adb_requests_are_scoped_and_cannot_choose_bind_addresses() {
        let request: Request = serde_json::from_str(r#"{"op":"adb_set","id":"default","enabled":true}"#).unwrap();
        assert!(matches!(request, Request::AdbSet { ref id, enabled: true } if id == "default"));
        for value in [
            r#"{"op":"adb_set","id":"default","enabled":"true"}"#,
            r#"{"op":"adb_set","id":"default","enabled":true,"host":"0.0.0.0"}"#,
            r#"{"op":"adb_info","id":"default","uid":0}"#,
        ] { assert!(serde_json::from_str::<Request>(value).is_err()); }
    }
    #[test]
    fn app_states_is_typed_and_scoped() {
        let request: Request = serde_json::from_str(r#"{"op":"app_states","id":"r-test"}"#).unwrap();
        assert!(matches!(request, Request::AppStates { ref id } if id == "r-test"));
        assert!(serde_json::from_str::<Request>(r#"{"op":"app_states","id":"r-test","uid":0}"#).is_err());
    }
    #[test]
    fn install_request_carries_split_sizes_and_never_a_path() {
        let single = Request::Install { id: "r-test".into(), parts: None };
        assert_eq!(serde_json::to_string(&single).unwrap(), r#"{"op":"install","id":"r-test"}"#);
        let split = Request::Install { id: "r-test".into(), parts: Some(vec![11, 7]) };
        assert_eq!(serde_json::to_string(&split).unwrap(), r#"{"op":"install","id":"r-test","parts":[11,7]}"#);
        assert!(serde_json::from_str::<Request>(r#"{"op":"install","id":"r-test","parts":[1],"path":"/x.apk"}"#).is_err());
        assert!(serde_json::from_str::<Request>(r#"{"op":"install","id":"r-test","parts":["1"]}"#).is_err());
        // Sizes that do not describe the open file are refused before any request.
        let path = std::env::temp_dir().join(format!("anvil-parts-{}.bin", std::process::id()));
        std::fs::write(&path, b"PK\x03\x04onePK\x03\x04two").unwrap();
        let file = std::fs::File::open(&path).unwrap();
        std::fs::remove_file(path).unwrap();
        let id = "r-11111111111111111111111111111111";
        for sizes in [&[][..], &[7, 6], &[7, 0, 7], &[15]] {
            assert!(install_parts(id, &file, sizes).unwrap_err().contains("up to 512 APKs"), "{sizes:?}");
        }
        assert_eq!(install_parts("default", &file, &[7, 7]).unwrap_err(), "Invalid runtime ID");
    }
    #[test]
    fn apk_descriptor_transfer_preserves_bytes_without_sending_a_path() {
        use std::os::fd::FromRawFd;
        use std::os::unix::fs::FileExt;
        let path = std::env::temp_dir().join(format!("anvil-fd-{}.apk", std::process::id()));
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
            .unwrap();
        std::fs::remove_file(path).unwrap();
        file.write_all_at(b"PK\x03\x04test", 0).unwrap();
        let (mut client, server) = UnixStream::pair().unwrap();
        send_files(&mut client, b"{\"op\":\"install\"}\n", &[&file]).unwrap();
        let mut bytes = [0u8; 100];
        let mut control = [0usize; 16];
        let mut iov = libc::iovec {
            iov_base: bytes.as_mut_ptr().cast(),
            iov_len: bytes.len(),
        };
        let mut message: libc::msghdr = unsafe { std::mem::zeroed() };
        message.msg_iov = &mut iov;
        message.msg_iovlen = 1;
        message.msg_control = control.as_mut_ptr().cast();
        message.msg_controllen = std::mem::size_of_val(&control);
        let received =
            unsafe { libc::recvmsg(server.as_raw_fd(), &mut message, libc::MSG_CMSG_CLOEXEC) };
        assert_eq!(&bytes[..received as usize], b"{\"op\":\"install\"}\n");
        let fd = unsafe {
            let header = libc::CMSG_FIRSTHDR(&message);
            assert!(!header.is_null());
            assert_eq!((*header).cmsg_type, libc::SCM_RIGHTS);
            std::ptr::read_unaligned(libc::CMSG_DATA(header).cast::<libc::c_int>())
        };
        let received_file = unsafe { std::fs::File::from_raw_fd(fd) };
        let mut content = [0u8; 8];
        received_file.read_exact_at(&mut content, 0).unwrap();
        assert_eq!(&content, b"PK\x03\x04test");
    }
    #[test]
    fn display_names_stay_within_the_callers_session() {
        assert_eq!(
            display_basename("wayland-0", 1000).as_deref(),
            Some("wayland-0")
        );
        assert_eq!(
            display_basename("/run/user/1000/wayland-0", 1000).as_deref(),
            Some("wayland-0")
        );
        for value in [
            "/run/user/2000/wayland-0",
            "../wayland-0",
            "/tmp/wayland-0",
            "folder/wayland-0",
            "x11:0",
        ] {
            assert!(display_basename(value, 1000).is_none());
        }
    }
    #[test]
    fn x11_display_accepts_only_local_forms() {
        for value in [
            ":0",
            ":0.1",
            "unix:0",
            "localhost:10.0",
            "/tmp/.X11-unix/X0",
        ] {
            assert_eq!(x11_display(value).as_deref(), Some(value), "accept {value}");
        }
        for value in [
            "host.example:0",
            "/tmp/X11-unix/X0",
            "/tmp/.X11-unix/X",
            "/tmp/.X11-unix/X0/..",
            "x11:0",
            ":0.1.2",
            ":",
            "",
            "wayland-0",
        ] {
            assert!(x11_display(value).is_none(), "reject {value}");
        }
    }
    #[test]
    fn session_display_prefers_wayland_over_x11() {
        let uid = 1000;
        assert_eq!(session_display(Some("wayland-0"), Some(":0"), uid).as_deref(), Some("wayland-0"));
        assert_eq!(session_display(None, Some(":0"), uid).as_deref(), Some("x11::0"));
        assert_eq!(session_display(Some("wayland-0"), None, uid).as_deref(), Some("wayland-0"));
        assert_eq!(session_display(None, None, uid), None);
        assert_eq!(session_display(None, Some("bogus"), uid), None);
        assert_eq!(
            session_display(Some("/run/user/2000/wayland-0"), Some(":0"), uid).as_deref(),
            Some("x11::0")
        );
    }
    #[test]
    fn rejects_unframed_or_invalid_responses() {
        for data in [
            b"{\"ok\":true,\"result\":[]}".as_slice(),
            b"{}\n",
            b"{\"ok\":true}\n",
        ] {
            assert!(decode(data).is_err());
        }
        assert!(decode(vec![b'x'; 4 * 1024 * 1024 + 1].as_slice()).is_err());
        assert_eq!(
            decode(b"{\"ok\":true,\"result\":[]}\n".as_slice()).unwrap(),
            serde_json::json!([])
        );
        assert_eq!(
            decode(b"{\"ok\":false,\"error\":{\"message\":\"Busy\"}}\n".as_slice()).unwrap_err(),
            "Busy"
        );
    }
    #[test]
    fn caller_cannot_supply_privileged_fields() {
        for data in [
            r#"{"op":"list","uid":0}"#,
            r#"{"op":"shell"}"#,
            r#"{"op":"start","id":"r-1","socket":"/tmp/fake"}"#,
        ] {
            assert!(serde_json::from_str::<Request>(data).is_err());
        }
    }
    #[test]
    fn screenshot_request_carries_only_the_device_and_one_descriptor() {
        for id in ["default", "r-11111111111111111111111111111111"] {
            let wire = serde_json::json!({"op":"screenshot_save","id":id});
            let request: Request = serde_json::from_value(wire.clone()).unwrap();
            assert!(matches!(request, Request::ScreenshotSave { id: ref sent } if sent == id));
            assert_eq!(serde_json::to_value(request).unwrap(), wire);
            for (key, privileged) in [("path", serde_json::json!("/etc/x.png")), ("destination", serde_json::json!("/tmp/x")),
                                      ("uid", serde_json::json!(0)), ("display", serde_json::json!(1))] {
                let mut invalid = wire.clone();
                invalid[key] = privileged;
                assert!(serde_json::from_value::<Request>(invalid).is_err());
            }
        }
        assert!(serde_json::from_str::<Request>(r#"{"op":"screenshot_save"}"#).is_err());
        let folder = std::env::temp_dir().join(format!("anvil-shot-{}", std::process::id()));
        std::fs::create_dir(&folder).unwrap();
        let directory = std::fs::File::open(&folder).unwrap();
        assert_eq!(screenshot("r-1", &directory).unwrap_err(), "Invalid runtime ID");
        assert!(screenshot("default", &directory).unwrap_err().contains("regular file"));

        // The descriptor travels with the request; the receiver writes through it.
        use std::os::fd::FromRawFd;
        use std::os::unix::fs::FileExt;
        let sent_path = folder.join("sent.png");
        let file = std::fs::OpenOptions::new().write(true).create_new(true).open(&sent_path).unwrap();
        let mut payload = serde_json::to_vec(&Request::ScreenshotSave { id: "default".into() }).unwrap();
        payload.push(b'\n');
        let (mut client, server) = UnixStream::pair().unwrap();
        send_files(&mut client, &payload, &[&file]).unwrap();
        let mut bytes = [0u8; 100];
        let mut control = [0usize; 16];
        let mut iov = libc::iovec { iov_base: bytes.as_mut_ptr().cast(), iov_len: bytes.len() };
        let mut message: libc::msghdr = unsafe { std::mem::zeroed() };
        message.msg_iov = &mut iov;
        message.msg_iovlen = 1;
        message.msg_control = control.as_mut_ptr().cast();
        message.msg_controllen = std::mem::size_of_val(&control);
        let received = unsafe { libc::recvmsg(server.as_raw_fd(), &mut message, libc::MSG_CMSG_CLOEXEC) };
        assert_eq!(&bytes[..received as usize], b"{\"op\":\"screenshot_save\",\"id\":\"default\"}\n");
        let fd = unsafe {
            let header = libc::CMSG_FIRSTHDR(&message);
            assert!(!header.is_null());
            assert_eq!((*header).cmsg_len, libc::CMSG_LEN(std::mem::size_of::<libc::c_int>() as _) as usize);
            std::ptr::read_unaligned(libc::CMSG_DATA(header).cast::<libc::c_int>())
        };
        let remote = unsafe { std::fs::File::from_raw_fd(fd) };
        assert_eq!(unsafe { libc::fcntl(remote.as_raw_fd(), libc::F_GETFL) } & libc::O_ACCMODE, libc::O_WRONLY);
        remote.write_all_at(b"\x89PNG\r\n\x1a\n", 0).unwrap();
        assert_eq!(std::fs::read(&sent_path).unwrap(), b"\x89PNG\r\n\x1a\n");
        std::fs::remove_dir_all(&folder).unwrap();
    }
    #[test]
    fn screenshot_file_appears_only_when_complete_and_respects_existing_files() {
        use std::os::unix::fs::FileExt;
        let folder = std::env::temp_dir().join(format!("anvil-shot-save-{}", std::process::id()));
        std::fs::create_dir(&folder).unwrap();
        let names = || { let mut names: Vec<_> = std::fs::read_dir(&folder).unwrap().map(|e| e.unwrap().file_name().into_string().unwrap()).collect(); names.sort(); names };
        let png = |bytes: &'static [u8]| move |file: &std::fs::File| {
            file.write_all_at(bytes, 0).unwrap();
            Ok(serde_json::json!({"runtime_id":"default","bytes":bytes.len(),"width":2,"height":1}))
        };
        let target = folder.join("shot.png");
        let saved = save_screenshot(&target, false, png(b"\x89PNG first")).unwrap();
        assert_eq!(saved["width"], 2);
        assert_eq!(std::fs::read(&target).unwrap(), b"\x89PNG first");
        assert_eq!(names(), ["shot.png"]);
        // Without overwrite an existing file is refused before any capture.
        let error = save_screenshot(&target, false, |_| panic!("must not capture")).unwrap_err();
        assert!(error.ends_with("already exists. Choose another file name."), "{error}");
        // A file that appears during the capture is not replaced either.
        let racing = folder.join("race.png");
        let error = save_screenshot(&racing, false, |file| { std::fs::write(&racing, b"theirs").unwrap(); png(b"\x89PNG mine")(file) }).unwrap_err();
        assert!(error.contains("already exists"), "{error}");
        assert_eq!(std::fs::read(&racing).unwrap(), b"theirs");
        std::fs::remove_file(&racing).unwrap();
        // A failed, empty or short capture leaves the old picture and no stray file.
        assert_eq!(save_screenshot(&target, true, |file| { file.write_all_at(b"\x89PNG par", 0).unwrap(); Err("Start this device first.".into()) }).unwrap_err(), "Start this device first.");
        assert!(save_screenshot(&target, true, |_| Ok(serde_json::json!({"bytes":0}))).unwrap_err().contains("not written completely"));
        assert!(save_screenshot(&target, true, |file| { file.write_all_at(b"abc", 0).unwrap(); Ok(serde_json::json!({"bytes":9})) }).unwrap_err().contains("not written completely"));
        assert_eq!(std::fs::read(&target).unwrap(), b"\x89PNG first");
        assert_eq!(names(), ["shot.png"]);
        save_screenshot(&target, true, png(b"\x89PNG second")).unwrap();
        assert_eq!(std::fs::read(&target).unwrap(), b"\x89PNG second");
        assert_eq!(names(), ["shot.png"]);
        assert!(save_screenshot(&folder, true, |_| panic!("must not capture")).unwrap_err().contains("is a folder"));
        assert!(save_screenshot(&folder.join("missing/shot.png"), false, |_| panic!("must not capture")).unwrap_err().contains("Cannot create a file"));
        assert!(save_screenshot(std::path::Path::new("/"), false, |_| panic!("must not capture")).is_err());
        std::fs::remove_dir_all(&folder).unwrap();
    }
    #[test]
    fn rejects_unprivileged_server_before_sending() {
        if unsafe { libc::geteuid() } == 0 {
            return;
        }
        let (client, mut server) = UnixStream::pair().unwrap();
        assert!(exchange(client, Request::List {})
            .unwrap_err()
            .contains("not owned by root"));
        let mut bytes = Vec::new();
        std::io::Read::read_to_end(&mut server, &mut bytes).unwrap();
        assert!(bytes.is_empty());
    }
    #[test]
    fn logs_request_round_trips_with_every_field_present() {
        for value in [
            serde_json::json!({"op":"logs_get","id":"default","lines":500,"level":"I","package":null,"since":null}),
            serde_json::json!({"op":"logs_get","id":"r-11111111111111111111111111111111","lines":2000,"level":"W",
                               "package":"com.example.app","since":"1795077058189000000:0123abcd,89abcdef"}),
        ] {
            let request: Request = serde_json::from_value(value.clone()).unwrap();
            assert_eq!(serde_json::to_value(request).unwrap(), value);
            for (key, extra) in [("path", serde_json::json!("/etc")), ("uid", serde_json::json!(0)), ("follow", serde_json::json!(true))] {
                let mut invalid = value.clone();
                invalid[key] = extra;
                assert!(serde_json::from_value::<Request>(invalid).is_err());
            }
        }
        for invalid in [
            serde_json::json!({"op":"logs_get","lines":500,"level":"I","package":null,"since":null}),
            serde_json::json!({"op":"logs_get","id":"default","lines":"500","level":"I","package":null,"since":null}),
            serde_json::json!({"op":"logs_get","id":"default","lines":-1,"level":"I","package":null,"since":null}),
            serde_json::json!({"op":"logs_get","id":"default","lines":500,"package":null,"since":null}),
            serde_json::json!({"op":"logs_get","id":"default","lines":500,"level":"I","package":["a"],"since":null}),
        ] {
            assert!(serde_json::from_value::<Request>(invalid).is_err());
        }
    }
    #[test]
    fn network_requests_round_trip_with_every_field_present() {
        let set = serde_json::json!({"op":"network_set","id":"r-11111111111111111111111111111111","proxy_host":"192.168.1.10",
                                     "proxy_port":3128,"proxy_bypass":["localhost","*.lan"],"dns_mode":"hostname",
                                     "dns_hostname":"dns.adguard.com"});
        for value in [
            serde_json::json!({"op":"network_get","id":"default"}),
            set.clone(),
            serde_json::json!({"op":"network_set","id":"default","proxy_host":"","proxy_port":0,"proxy_bypass":[],
                               "dns_mode":"automatic","dns_hostname":""}),
        ] {
            let request: Request = serde_json::from_value(value.clone()).unwrap();
            assert_eq!(serde_json::to_value(request).unwrap(), value);
            // Android's global proxy has no credentials, and no caller field is privileged.
            for (key, extra) in [("username", serde_json::json!("u")), ("password", serde_json::json!("p")),
                                 ("uid", serde_json::json!(0)), ("path", serde_json::json!("/etc"))] {
                let mut invalid = value.clone();
                invalid[key] = extra;
                assert!(serde_json::from_value::<Request>(invalid).is_err());
            }
        }
        for key in ["id", "proxy_host", "proxy_port", "proxy_bypass", "dns_mode", "dns_hostname"] {
            let mut missing = set.clone();
            missing.as_object_mut().unwrap().remove(key);
            assert!(serde_json::from_value::<Request>(missing).is_err(), "missing {key}");
        }
        for (key, wrong) in [("proxy_port", serde_json::json!("3128")), ("proxy_port", serde_json::json!(65536)),
                             ("proxy_port", serde_json::json!(-1)), ("proxy_port", serde_json::json!(true)),
                             ("proxy_bypass", serde_json::json!("localhost")), ("proxy_bypass", serde_json::json!(null)),
                             ("proxy_host", serde_json::json!(null)), ("dns_hostname", serde_json::json!(null))] {
            let mut invalid = set.clone();
            invalid[key] = wrong;
            assert!(serde_json::from_value::<Request>(invalid).is_err(), "wrong {key}");
        }
    }
    #[test]
    fn performance_request_round_trips_and_takes_no_arguments() {
        for id in ["default", "r-11111111111111111111111111111111"] {
            let value = serde_json::json!({"op":"performance_get","id":id});
            let request: Request = serde_json::from_value(value.clone()).unwrap();
            assert_eq!(serde_json::to_value(request).unwrap(), value);
            for (key, extra) in [("package", serde_json::json!("com.example.app")), ("layer", serde_json::json!("SurfaceView[a/b]#1")),
                                 ("uid", serde_json::json!(0)), ("command", serde_json::json!("id"))] {
                let mut invalid = value.clone();
                invalid[key] = extra;
                assert!(serde_json::from_value::<Request>(invalid).is_err(), "extra {key}");
            }
        }
        assert!(serde_json::from_value::<Request>(serde_json::json!({"op":"performance_get"})).is_err());
        assert!(serde_json::from_value::<Request>(serde_json::json!({"op":"performance_get","id":7})).is_err());
    }
}
