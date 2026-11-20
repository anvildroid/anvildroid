//! Opening package files from the file manager: `anvildroid-gui FILE.apk...`.
//!
//! The first GUI process listens on a socket in the user's runtime directory.
//! A later `anvildroid-gui FILE...` hands its paths to that process and exits,
//! so the files open in the window the user already has. APKs, split APKs
//! and bundles (`.apks`, `.xapk`, `.apkm`) are accepted, several at once.

use std::ffi::OsStr;
use std::io::{Read, Write};
use std::os::unix::fs::FileTypeExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Emitter, Manager};

/// Frontend event: package files are waiting in [`PendingApk`].
pub const EVENT: &str = "open-apk";
const SOCKET_NAME: &str = "anvildroid-gui.sock";
const MAX_PATH: usize = 4096;
/// Files one launch may hand over, as many as one install accepts.
pub const MAX_FILES: usize = anvildroid_core::apk_set::MAX_FILES;
const MAX_TOKEN: usize = 256;
const TIMEOUT: Duration = Duration::from_secs(2);
const ACCEPTED: &[u8] = b"ok";

/// The files the Install dialog should open with, until the page takes them.
pub struct PendingApk(pub Mutex<Vec<String>>);

#[tauri::command]
pub fn take_pending_apk(state: tauri::State<'_, PendingApk>) -> Vec<String> {
    std::mem::take(&mut *state.0.lock().unwrap_or_else(|e| e.into_inner()))
}

fn has_apk_extension(path: &Path) -> bool {
    anvildroid_core::apk_set::accepted(path)
}

/// An argument that names a package file rather than an option.
pub fn is_apk_argument(argument: &OsStr) -> bool {
    !argument.as_encoded_bytes().starts_with(b"-") && has_apk_extension(Path::new(argument))
}

/// The absolute path of an existing APK, as the frontend and installer use it.
pub fn resolve(argument: &OsStr) -> Result<String, String> {
    let shown = Path::new(argument).display().to_string();
    let path = std::fs::canonicalize(argument).map_err(|_| format!("Package file not found: {shown}"))?;
    checked(&path).ok_or_else(|| format!("Not an Android package file: {shown}"))
}

fn checked(path: &Path) -> Option<String> {
    let text = path.to_str()?;
    (path.is_absolute() && text.len() <= MAX_PATH && !text.contains('\0')
        && has_apk_extension(path) && path.is_file())
        .then(|| text.to_owned())
}

fn own_uid() -> u32 {
    // getuid only reads this process's identity and cannot fail.
    unsafe { libc::getuid() }
}

/// The runtime directory is trusted only if it is what the desktop session
/// promises: a real directory of this user that nobody else can enter.
/// Anything else could hold another user's socket under our name.
fn private_directory(directory: &Path) -> bool {
    use std::os::unix::fs::MetadataExt;
    directory.is_absolute() && std::fs::symlink_metadata(directory)
        .is_ok_and(|meta| meta.is_dir() && meta.uid() == own_uid() && meta.mode() & 0o077 == 0)
}

fn socket_path() -> Option<PathBuf> {
    let directory = PathBuf::from(std::env::var_os("XDG_RUNTIME_DIR")?);
    private_directory(&directory).then(|| directory.join(SOCKET_NAME))
}

/// The user at the other end of the socket, as the kernel recorded it.
fn peer_uid(stream: &UnixStream) -> Option<u32> {
    use std::os::fd::AsRawFd;
    let mut credentials = libc::ucred { pid: 0, uid: u32::MAX, gid: u32::MAX };
    let mut length = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    // The buffer and its size stay valid for the whole getsockopt call.
    let result = unsafe {
        libc::getsockopt(stream.as_raw_fd(), libc::SOL_SOCKET, libc::SO_PEERCRED,
            (&mut credentials as *mut libc::ucred).cast(), &mut length)
    };
    (result == 0 && length as usize == std::mem::size_of::<libc::ucred>()).then_some(credentials.uid)
}

/// Files are handed over and accepted only between processes of this user.
fn same_user(peer: Option<u32>) -> bool {
    peer == Some(own_uid())
}

/// The launcher's activation token lets the running window take focus.
fn activation_token() -> String {
    ["XDG_ACTIVATION_TOKEN", "DESKTOP_STARTUP_ID"].iter()
        .filter_map(|name| std::env::var(name).ok())
        .find(|token| !token.is_empty() && token.len() <= MAX_TOKEN && !token.contains('\0'))
        .unwrap_or_default()
}

/// Hand the files to a running GUI. False means this process must open them.
pub fn forward(paths: &[String]) -> bool {
    socket_path().is_some_and(|socket| send(&socket, paths, &activation_token()))
}

/// One message: every path, then the activation token, separated by NUL.
fn send(socket: &Path, paths: &[String], token: &str) -> bool {
    let Ok(mut stream) = UnixStream::connect(socket) else { return false };
    if !same_user(peer_uid(&stream)) { return false; }
    let _ = stream.set_write_timeout(Some(TIMEOUT));
    let _ = stream.set_read_timeout(Some(TIMEOUT));
    let message = format!("{}\0{token}", paths.join("\0"));
    if stream.write_all(message.as_bytes()).is_err()
        || stream.shutdown(std::net::Shutdown::Write).is_err() {
        return false;
    }
    let mut reply = Vec::new();
    stream.take(ACCEPTED.len() as u64 + 1).read_to_end(&mut reply).is_ok() && reply == ACCEPTED
}

/// Read one request: the file paths and the sender's activation token.
fn receive(stream: &mut UnixStream) -> Option<(Vec<String>, String)> {
    let _ = stream.set_read_timeout(Some(TIMEOUT));
    let _ = stream.set_write_timeout(Some(TIMEOUT));
    let mut message = Vec::new();
    let limit = (MAX_FILES * (MAX_PATH + 1) + MAX_TOKEN) as u64;
    stream.take(limit + 1).read_to_end(&mut message).ok()?;
    if message.len() as u64 > limit { return None; }
    let message = String::from_utf8(message).ok()?;
    let (paths, token) = message.rsplit_once('\0')?;
    if token.len() > MAX_TOKEN { return None; }
    let paths: Vec<&str> = paths.split('\0').collect();
    if paths.len() > MAX_FILES { return None; }
    // One unacceptable path refuses the request; the sender then opens its own window.
    let paths = paths.into_iter().map(|path| checked(Path::new(path))).collect::<Option<Vec<_>>>()?;
    Some((paths, token.to_owned()))
}

/// Bind the socket unless another GUI process already answers on it.
fn bind(socket: &Path) -> Option<UnixListener> {
    match UnixListener::bind(socket) {
        Ok(listener) => Some(listener),
        Err(error) if error.kind() == std::io::ErrorKind::AddrInUse => {
            if UnixStream::connect(socket).is_ok() { return None; }
            // A socket left by a process that is gone. Never remove anything else.
            let stale = std::fs::symlink_metadata(socket).is_ok_and(|meta| meta.file_type().is_socket());
            if !stale || std::fs::remove_file(socket).is_err() { return None; }
            UnixListener::bind(socket).ok()
        }
        Err(_) => None,
    }
}

fn serve(listener: UnixListener, mut open: impl FnMut(Vec<String>, String)) {
    for stream in listener.incoming() {
        let Ok(mut stream) = stream else { continue };
        if !same_user(peer_uid(&stream)) { continue; }
        if let Some((paths, token)) = receive(&mut stream) {
            if stream.write_all(ACCEPTED).is_ok() { open(paths, token); }
        }
    }
}

/// Accept package files from later launches for as long as this GUI runs.
pub fn listen(app: tauri::AppHandle) {
    let Some(listener) = socket_path().and_then(|socket| bind(&socket)) else { return };
    let spawned = std::thread::Builder::new().name("open-apk".into()).spawn(move || {
        serve(listener, |paths, token| {
            *app.state::<PendingApk>().0.lock().unwrap_or_else(|e| e.into_inner()) = paths;
            let handle = app.clone();
            let _ = app.run_on_main_thread(move || {
                // A window released to the panel is recreated and its page
                // takes the pending file while loading; an open page needs the event.
                crate::preferences::present_main(&handle, Some(token.as_str()).filter(|token| !token.is_empty()));
                let _ = handle.emit(EVENT, ());
            });
        });
    });
    if let Err(error) = spawned { eprintln!("Cannot accept APK files from the file manager: {error}"); }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let directory = std::env::temp_dir().join(format!("anvildroid-open-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&directory);
        std::fs::create_dir_all(&directory).unwrap();
        directory.canonicalize().unwrap()
    }

    #[test]
    fn only_a_plain_apk_path_is_an_apk_argument() {
        assert!(is_apk_argument(OsStr::new("game.apk")));
        assert!(is_apk_argument(OsStr::new("/home/user/My Game.APK")));
        assert!(!is_apk_argument(OsStr::new("--launch-app")));
        assert!(!is_apk_argument(OsStr::new("--evil.apk")));
        assert!(!is_apk_argument(OsStr::new("-.apk")));
        for bundle in ["game.apks", "Game.XAPK", "/tmp/game.apkm"] { assert!(is_apk_argument(OsStr::new(bundle))); }
        assert!(!is_apk_argument(OsStr::new("--evil.xapk")));
        assert!(!is_apk_argument(OsStr::new("game.zip")));
        assert!(!is_apk_argument(OsStr::new("apk")));
    }

    #[test]
    fn resolve_requires_an_existing_regular_apk() {
        let directory = scratch("resolve");
        let apk = directory.join("My Game.apk");
        std::fs::write(&apk, b"x").unwrap();
        assert_eq!(resolve(apk.as_os_str()).unwrap(), apk.to_str().unwrap());
        assert!(resolve(directory.join("missing.apk").as_os_str()).unwrap_err().contains("not found"));
        std::fs::create_dir(directory.join("folder.apk")).unwrap();
        assert!(resolve(directory.join("folder.apk").as_os_str()).unwrap_err().contains("Not an Android package"));
        // A link is followed, so its target decides.
        std::os::unix::fs::symlink(directory.join("note.txt"), directory.join("link.apk")).unwrap();
        std::fs::write(directory.join("note.txt"), b"x").unwrap();
        assert!(resolve(directory.join("link.apk").as_os_str()).unwrap_err().contains("Not an Android package"));
        let bundle = directory.join("game.xapk");
        std::fs::write(&bundle, b"x").unwrap();
        assert_eq!(resolve(bundle.as_os_str()).unwrap(), bundle.to_str().unwrap());
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn a_running_instance_receives_the_paths_and_token() {
        let directory = scratch("forward");
        let apk = directory.join("game.apk");
        let split = directory.join("split_config.en.apk");
        let bundle = directory.join("other game.xapk");
        for file in [&apk, &split, &bundle] { std::fs::write(file, b"x").unwrap(); }
        let socket = directory.join(SOCKET_NAME);
        let listener = bind(&socket).unwrap();
        let (sender, receiver) = std::sync::mpsc::channel();
        std::thread::spawn(move || serve(listener, move |paths, token| { let _ = sender.send((paths, token)); }));
        let path = apk.to_str().unwrap().to_owned();
        let one = vec![path.clone()];
        assert!(send(&socket, &one, "token-1"));
        assert_eq!(receiver.recv_timeout(TIMEOUT).unwrap(), (one.clone(), "token-1".to_owned()));
        assert!(send(&socket, &one, ""));
        assert_eq!(receiver.recv_timeout(TIMEOUT).unwrap(), (one.clone(), String::new()));
        // Several files selected in the file manager arrive together, in order.
        let several = vec![path.clone(), split.to_str().unwrap().to_owned(), bundle.to_str().unwrap().to_owned()];
        assert!(send(&socket, &several, "token-2"));
        assert_eq!(receiver.recv_timeout(TIMEOUT).unwrap(), (several, "token-2".to_owned()));
        // Rejected requests are not acknowledged, so the sender opens its own window.
        let refused = |paths: &[String], token: &str| assert!(!send(&socket, paths, token), "{paths:?}");
        refused(&[directory.join("missing.apk").to_str().unwrap().to_owned()], "");
        refused(&[directory.join("../forward/game.txt").to_str().unwrap().to_owned()], "");
        refused(&["game.apk".to_owned()], "");
        refused(&[path.clone(), "/etc/hostname".to_owned()], "");
        refused(&[], "");
        refused(&one, &"t".repeat(MAX_TOKEN + 1));
        refused(&[format!("{}/{}.apk", directory.display(), "a".repeat(MAX_PATH))], "");
        refused(&vec![path.clone(); MAX_FILES + 1], "");
        assert!(receiver.try_recv().is_err());
        assert!(send(&socket, &vec![path; MAX_FILES], ""));
        assert_eq!(receiver.recv_timeout(TIMEOUT).unwrap().0.len(), MAX_FILES);
        // A second instance leaves the live socket alone.
        assert!(bind(&socket).is_none());
        assert!(send(&socket, &one, ""));
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn only_a_private_runtime_directory_and_the_same_user_are_trusted() {
        use std::os::unix::fs::PermissionsExt;
        let directory = scratch("private");
        let mode = |mode| std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(mode)).unwrap();
        mode(0o700);
        assert!(private_directory(&directory));
        for open in [0o750, 0o705, 0o701, 0o770, 0o777] {
            mode(open);
            assert!(!private_directory(&directory), "{open:o}");
        }
        mode(0o700);
        // A link, a file, a missing or relative path and another user's directory.
        let link = directory.join("link");
        std::os::unix::fs::symlink(&directory, &link).unwrap();
        assert!(!private_directory(&link));
        std::fs::write(directory.join("file"), b"x").unwrap();
        assert!(!private_directory(&directory.join("file")));
        assert!(!private_directory(&directory.join("missing")));
        assert!(!private_directory(Path::new("relative")));
        if own_uid() != 0 { assert!(!private_directory(Path::new("/"))); }
        // The kernel names the peer; anyone else is neither served nor sent to.
        let (left, right) = UnixStream::pair().unwrap();
        assert_eq!(peer_uid(&left), Some(own_uid()));
        assert!(same_user(peer_uid(&right)));
        assert!(!same_user(Some(own_uid().wrapping_add(1))));
        assert!(!same_user(None));
        // Both directions check it before any path is read or written.
        let source = include_str!("open_file.rs");
        let body = |start: &str| source[source.find(start).unwrap()..].split("\n}\n").next().unwrap().to_owned();
        let send = body("fn send(socket: &Path");
        assert!(send.find("same_user(peer_uid(&stream))").unwrap() < send.find("write_all").unwrap());
        let serve = body("fn serve(listener: UnixListener");
        assert!(serve.find("same_user(peer_uid(&stream))").unwrap() < serve.find("receive(&mut stream)").unwrap());
        assert!(body("fn socket_path()").contains("private_directory(&directory)"));
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn a_stale_socket_is_replaced_but_other_files_are_kept() {
        let directory = scratch("stale");
        let socket = directory.join(SOCKET_NAME);
        drop(UnixListener::bind(&socket).unwrap());
        assert!(socket.exists());
        assert!(!send(&socket, &["/x.apk".to_owned()], ""));
        assert!(bind(&socket).is_some());
        let file = directory.join("regular");
        std::fs::write(&file, b"keep").unwrap();
        assert!(bind(&file).is_none());
        assert_eq!(std::fs::read(&file).unwrap(), b"keep");
        assert!(!send(&directory.join("absent"), &["/x.apk".to_owned()], ""));
        std::fs::remove_dir_all(directory).unwrap();
    }
}
