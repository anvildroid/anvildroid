//! Saves the log lines shown in Device settings > Logs to a file the user chose.
//! The file is written by this process, as the desktop user, never by the root controller.
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;

/// The panel keeps a few thousand lines; this bounds what one save can write.
const MAX_BYTES: usize = 8 * 1024 * 1024;

fn write_log(path: &Path, content: &str) -> Result<usize, String> {
    if !path.is_absolute() || path.file_name().is_none() {
        return Err("Choose a file to save the logs to.".into());
    }
    if content.is_empty() {
        return Err("There are no log lines to save.".into());
    }
    if content.len() > MAX_BYTES {
        return Err("These logs are too large to save. Clear the view or filter by level, then retry.".into());
    }
    // Same rule as the diagnostic report: a new private file, never an
    // overwrite, a symlink, a FIFO or a device node.
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .map_err(|e| format!("Could not create the log file. Choose a new file name: {e}"))?;
    if let Err(error) = file.write_all(content.as_bytes()).and_then(|_| file.sync_all()) {
        // Remove only the inode created here, even if its name was replaced meanwhile.
        use std::os::unix::fs::MetadataExt;
        if let (Ok(opened), Ok(named)) = (file.metadata(), path.symlink_metadata()) {
            if (opened.dev(), opened.ino()) == (named.dev(), named.ino()) {
                let _ = std::fs::remove_file(path);
            }
        }
        return Err(format!("Could not write the log file: {error}"));
    }
    Ok(content.len())
}

#[tauri::command]
pub async fn save_device_log(path: String, content: String) -> Result<serde_json::Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let bytes = write_log(Path::new(&path), &content)?;
        Ok(serde_json::json!({"path": path, "bytes": bytes}))
    }).await.map_err(|e| e.to_string())?
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{symlink, PermissionsExt};

    #[test]
    fn writes_a_new_private_file_and_never_replaces_or_follows() {
        let folder = std::env::temp_dir().join(format!("anvildroid-device-log-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&folder);
        std::fs::create_dir(&folder).unwrap();
        let target = folder.join("device.txt");
        assert_eq!(write_log(&target, "11-19 15:31:29.892 I Tag: line\n").unwrap(), 31);
        assert_eq!(std::fs::read_to_string(&target).unwrap(), "11-19 15:31:29.892 I Tag: line\n");
        assert_eq!(std::fs::metadata(&target).unwrap().permissions().mode() & 0o777, 0o600);
        assert!(write_log(&target, "again").unwrap_err().contains("Choose a new file name"));
        assert_eq!(std::fs::read_to_string(&target).unwrap(), "11-19 15:31:29.892 I Tag: line\n");
        let link = folder.join("link.txt");
        symlink(folder.join("elsewhere.txt"), &link).unwrap();
        assert!(write_log(&link, "text").is_err());
        assert!(!folder.join("elsewhere.txt").exists());
        assert!(write_log(Path::new("relative.txt"), "text").is_err());
        assert!(write_log(Path::new("/"), "text").is_err());
        assert!(write_log(&folder.join("empty.txt"), "").unwrap_err().contains("no log lines"));
        assert!(write_log(&folder.join("big.txt"), &"x".repeat(MAX_BYTES + 1)).unwrap_err().contains("too large"));
        assert!(!folder.join("empty.txt").exists() && !folder.join("big.txt").exists());
        std::fs::remove_dir_all(&folder).unwrap();
    }
}
