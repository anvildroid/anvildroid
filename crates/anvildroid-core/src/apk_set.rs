//! Package files chosen for installation: APKs, split APKs and bundles.
//!
//! Everything here runs as the desktop user. Bundles (`.apks`, `.xapk`,
//! `.apkm`) are untrusted ZIP files: entry names only classify an entry and
//! are never used as paths, every size is capped, and the APKs are unpacked
//! end to end into one private file. Each APK's own manifest decides which
//! package it belongs to, so one install session never mixes packages.
use crate::{backend, config, error::AnvilError};
use std::{
    fs::{self, File},
    io::{self, Read, Write},
    os::unix::fs::{DirBuilderExt, FileExt, MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};

/// File extensions the installer accepts, lowercase.
pub const EXTENSIONS: [&str; 4] = ["apk", "apks", "xapk", "apkm"];
/// Files accepted in one request.
pub const MAX_FILES: usize = 64;
/// APKs in one install session. The controller enforces the same limits.
pub const MAX_PARTS: usize = 512;
pub const MAX_APK: u64 = 2 * 1024 * 1024 * 1024;
pub const MAX_SET: u64 = 4 * 1024 * 1024 * 1024;
/// Bundle data one request may unpack in all, and the free space it leaves.
pub const MAX_UNPACKED: u64 = 8 * 1024 * 1024 * 1024;
const FREE_RESERVE: u64 = 512 * 1024 * 1024;
const MAX_BUNDLE_ENTRIES: u64 = 10_000;
const MAX_APK_ENTRIES: u64 = 500_000;
const MAX_DIRECTORY: u64 = 64 * 1024 * 1024;
const MAX_MANIFEST: u64 = 8 * 1024 * 1024;
const MAX_NAME: usize = 256;

/// True for a path the installer accepts by extension.
pub fn accepted(path: &Path) -> bool {
    path.extension()
        .and_then(|extension| extension.to_str())
        .is_some_and(|extension| EXTENSIONS.iter().any(|known| extension.eq_ignore_ascii_case(known)))
}

/// One APK: a byte range of a file that stays open from inspection to
/// installation, so the bytes sent are the bytes that were inspected even if
/// the path is renamed or replaced in between.
#[derive(Debug, Clone)]
pub struct Part {
    file: Arc<File>,
    /// Size and change times of the file when it was inspected.
    stamp: (u64, i64, i64, i64, i64),
    pub offset: u64,
    pub size: u64,
    /// `None` for the base APK.
    pub split: Option<String>,
}

fn stamp(file: &File) -> io::Result<(u64, i64, i64, i64, i64)> {
    let meta = file.metadata()?;
    Ok((meta.len(), meta.mtime(), meta.mtime_nsec(), meta.ctime(), meta.ctime_nsec()))
}

impl Part {
    /// A byte range of an already opened regular file.
    pub fn new(file: Arc<File>, offset: u64, size: u64, split: Option<String>) -> io::Result<Self> {
        let stamp = stamp(&file)?;
        Ok(Self { file, stamp, offset, size, split })
    }

    /// Fails if the file was written to since it was inspected.
    fn unchanged(&self) -> io::Result<()> {
        if stamp(&self.file)? != self.stamp {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "The file changed after it was selected; select it again"));
        }
        Ok(())
    }

    /// Append exactly this APK to `out`, from the descriptor that was inspected.
    pub fn copy_to(&self, out: &mut impl Write) -> io::Result<()> {
        self.unchanged()?;
        let mut cursor = Cursor {
            window: Window { file: &self.file, start: self.offset, len: self.size },
            position: 0,
            end: self.size,
        };
        let copied = io::copy(&mut cursor, out)?;
        // A write during the copy shows in the change time afterwards.
        self.unchanged()?;
        if copied != self.size {
            return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "The file changed after it was selected; select it again"));
        }
        Ok(())
    }
}

/// The APKs of one package, installed as one Android session. Base first.
#[derive(Debug, Clone)]
pub struct PackageSet {
    /// Empty when the manifest could not be read (see `manifest_error`).
    pub package: String,
    pub version_code: u64,
    /// What the user selected, for messages: a file name.
    pub label: String,
    pub parts: Vec<Part>,
    /// Expansion files found in the bundle. They are not installed.
    pub obb_files: u64,
    pub obb_bytes: u64,
    /// A lone APK whose manifest this reader cannot parse. Android decides.
    pub manifest_error: Option<String>,
}

impl PackageSet {
    pub fn bytes(&self) -> u64 {
        self.parts.iter().map(|part| part.size).sum()
    }

    /// One readable file holding every APK end to end, and each APK's size.
    /// A bundle's unpacked file is used as it is; loose files are joined.
    pub fn transfer_file(&self) -> Result<(File, Vec<u64>), AnvilError> {
        let sizes: Vec<u64> = self.parts.iter().map(|part| part.size).collect();
        let first = self.parts.first()
            .ok_or_else(|| AnvilError::InvalidArgument("No APK to install".into()))?;
        let mut next = 0;
        let contiguous = self.parts.iter().all(|part| {
            let fits = Arc::ptr_eq(&part.file, &first.file) && part.offset == next;
            next += part.size;
            fits
        });
        if contiguous && first.stamp.0 == next {
            // The very descriptor that was inspected, checked for writes since.
            first.unchanged()?;
            return Ok((first.file.try_clone()?, sizes));
        }
        let directory = config::cache_dir();
        fs::create_dir_all(&directory)?;
        let path = directory.join(format!("install-join-{}", unique()));
        let mut file = fs::OpenOptions::new().read(true).write(true).create_new(true).mode(0o600).open(&path)?;
        // Only the open descriptor is needed; nothing is left behind on failure.
        fs::remove_file(&path)?;
        for part in &self.parts {
            part.copy_to(&mut file)?;
        }
        file.flush()?;
        Ok((file, sizes))
    }
}

/// A selected file that will not be installed, and why.
#[derive(Debug, Clone)]
pub struct Rejected {
    pub file: String,
    pub reason: String,
}

/// The result of inspecting the selected files. Dropping it removes the
/// unpacked bundle data.
#[derive(Debug)]
pub struct Prepared {
    pub packages: Vec<PackageSet>,
    pub rejected: Vec<Rejected>,
    directory: Option<PathBuf>,
}

impl Drop for Prepared {
    fn drop(&mut self) {
        if let Some(directory) = self.directory.take() {
            let _ = fs::remove_dir_all(directory);
        }
    }
}

fn unique() -> String {
    format!(
        "{}-{}",
        std::process::id(),
        SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos()
    )
}

/// Open without blocking on a FIFO, and refuse anything but a regular file.
fn open_regular(path: &Path) -> io::Result<File> {
    let file = fs::OpenOptions::new().read(true).custom_flags(libc::O_NONBLOCK).open(path)?;
    if !file.metadata()?.is_file() {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "not a regular file"));
    }
    Ok(file)
}

// --- ZIP reading -----------------------------------------------------------

/// A byte range of an open file that holds one ZIP archive.
#[derive(Clone, Copy)]
struct Window<'a> {
    file: &'a File,
    start: u64,
    len: u64,
}

impl Window<'_> {
    fn read(&self, offset: u64, buffer: &mut [u8]) -> Result<(), String> {
        offset.checked_add(buffer.len() as u64).filter(|end| *end <= self.len)
            .ok_or("Archive is truncated")?;
        self.file.read_exact_at(buffer, self.start + offset)
            .map_err(|error| format!("Cannot read archive: {error}"))
    }
}

struct Cursor<'a> {
    window: Window<'a>,
    position: u64,
    end: u64,
}

impl Read for Cursor<'_> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        let wanted = (self.end - self.position).min(buffer.len() as u64) as usize;
        if wanted == 0 {
            return Ok(0);
        }
        let count = self.window.file.read_at(&mut buffer[..wanted], self.window.start + self.position)?;
        self.position += count as u64;
        Ok(count)
    }
}

fn le(bytes: &[u8], at: usize, width: usize) -> Option<u64> {
    let field = bytes.get(at..at.checked_add(width)?)?;
    Some(field.iter().rev().fold(0u64, |value, byte| (value << 8) | u64::from(*byte)))
}

#[derive(Debug, Clone)]
struct Entry {
    name: String,
    flags: u16,
    method: u16,
    crc: u32,
    packed: u64,
    size: u64,
    header: u64,
}

/// Visit the central directory; `visit` returns false to stop early.
fn scan(window: Window, max_entries: u64, mut visit: impl FnMut(Entry) -> bool) -> Result<(), String> {
    const DAMAGED: &str = "Not a valid ZIP archive";
    let mut magic = [0u8; 4];
    window.read(0, &mut magic).map_err(|_| DAMAGED)?;
    if &magic != b"PK\x03\x04" {
        return Err(DAMAGED.into());
    }
    let tail_len = window.len.min(22 + 65_535) as usize;
    let tail_start = window.len - tail_len as u64;
    let mut tail = vec![0u8; tail_len];
    window.read(tail_start, &mut tail)?;
    let end = (0..=tail_len.saturating_sub(22)).rev()
        .find(|at| tail[*at..].starts_with(b"PK\x05\x06")
            && le(&tail, at + 20, 2).is_some_and(|comment| at + 22 + comment as usize <= tail_len))
        .ok_or(DAMAGED)?;
    let mut count = le(&tail, end + 10, 2).ok_or(DAMAGED)?;
    let mut size = le(&tail, end + 12, 4).ok_or(DAMAGED)?;
    let mut offset = le(&tail, end + 16, 4).ok_or(DAMAGED)?;
    if count == 0xFFFF || size == 0xFFFF_FFFF || offset == 0xFFFF_FFFF {
        // ZIP64: a locator before the end record points at the real totals.
        let mut locator = [0u8; 20];
        let at = (tail_start + end as u64).checked_sub(20).ok_or(DAMAGED)?;
        window.read(at, &mut locator)?;
        if !locator.starts_with(b"PK\x06\x07") {
            return Err(DAMAGED.into());
        }
        let mut record = [0u8; 56];
        window.read(le(&locator, 8, 8).ok_or(DAMAGED)?, &mut record)?;
        if !record.starts_with(b"PK\x06\x06") {
            return Err(DAMAGED.into());
        }
        count = le(&record, 32, 8).ok_or(DAMAGED)?;
        size = le(&record, 40, 8).ok_or(DAMAGED)?;
        offset = le(&record, 48, 8).ok_or(DAMAGED)?;
    }
    if count > max_entries || size > MAX_DIRECTORY {
        return Err("Archive has too many entries".into());
    }
    let mut directory = vec![0u8; size as usize];
    window.read(offset, &mut directory)?;
    let mut at = 0usize;
    for _ in 0..count {
        if !directory.get(at..).is_some_and(|rest| rest.starts_with(b"PK\x01\x02")) {
            return Err(DAMAGED.into());
        }
        let field = |position: usize, width: usize| le(&directory, at + position, width).ok_or(DAMAGED);
        let name_len = field(28, 2)? as usize;
        let extra_len = field(30, 2)? as usize;
        let comment_len = field(32, 2)? as usize;
        let mut packed = field(20, 4)?;
        let mut unpacked = field(24, 4)?;
        let mut header = field(42, 4)?;
        let name_at = at + 46;
        let extra_at = name_at + name_len;
        let next = extra_at + extra_len + comment_len;
        let name = directory.get(name_at..extra_at).ok_or(DAMAGED)?;
        let extra = directory.get(extra_at..extra_at + extra_len).ok_or(DAMAGED)?;
        if next > directory.len() {
            return Err(DAMAGED.into());
        }
        // ZIP64 extra field: values that did not fit, in this fixed order.
        let mut position = 0;
        while let (Some(id), Some(length)) = (le(extra, position, 2), le(extra, position + 2, 2)) {
            let mut value = position + 4;
            if id == 1 {
                for target in [&mut unpacked, &mut packed, &mut header] {
                    if *target == 0xFFFF_FFFF {
                        *target = le(extra, value, 8).ok_or(DAMAGED)?;
                        value += 8;
                    }
                }
                break;
            }
            position = value + length as usize;
        }
        let entry = Entry {
            name: String::from_utf8_lossy(name).into_owned(),
            flags: field(8, 2)? as u16,
            method: field(10, 2)? as u16,
            crc: field(16, 4)? as u32,
            packed,
            size: unpacked,
            header,
        };
        if !visit(entry) {
            return Ok(());
        }
        at = next;
    }
    Ok(())
}

/// Unpack one entry. At most `entry.size` bytes are ever written, and the
/// caller has already capped that size.
fn extract(window: Window, entry: &Entry, limit: u64, out: &mut impl Write) -> Result<(), String> {
    const DAMAGED: &str = "Archive entry is damaged";
    if entry.flags & 1 != 0 {
        return Err("Archive is password protected".into());
    }
    if entry.size > limit {
        return Err("Archive entry is too large".into());
    }
    let mut local = [0u8; 30];
    window.read(entry.header, &mut local)?;
    if !local.starts_with(b"PK\x03\x04") {
        return Err(DAMAGED.into());
    }
    let skip = 30 + le(&local, 26, 2).ok_or(DAMAGED)? + le(&local, 28, 2).ok_or(DAMAGED)?;
    let data = entry.header.checked_add(skip).ok_or(DAMAGED)?;
    let end = data.checked_add(entry.packed).filter(|end| *end <= window.len).ok_or(DAMAGED)?;
    let cursor = Cursor { window, position: data, end };
    let mut reader: Box<dyn Read + '_> = match entry.method {
        0 if entry.packed == entry.size => Box::new(cursor),
        8 => Box::new(flate2::read::DeflateDecoder::new(io::BufReader::with_capacity(65_536, cursor))),
        0 => return Err(DAMAGED.into()),
        _ => return Err("Archive uses an unsupported compression method".into()),
    };
    let mut crc = flate2::Crc::new();
    let mut buffer = vec![0u8; 65_536];
    let mut total = 0u64;
    loop {
        let count = reader.read(&mut buffer).map_err(|_| DAMAGED)?;
        if count == 0 {
            break;
        }
        total += count as u64;
        if total > entry.size {
            return Err(DAMAGED.into());
        }
        crc.update(&buffer[..count]);
        out.write_all(&buffer[..count]).map_err(|error| format!("Cannot unpack: {error}"))?;
    }
    if total != entry.size || crc.sum() != entry.crc {
        return Err(DAMAGED.into());
    }
    Ok(())
}

// --- APK manifest ----------------------------------------------------------

/// What Android uses to decide which APKs belong together.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Identity {
    pub package: String,
    pub version_code: u64,
    pub split: Option<String>,
}

fn apk_identity(window: Window) -> Result<Identity, String> {
    let mut manifest = None;
    scan(window, MAX_APK_ENTRIES, |entry| {
        if entry.name == "AndroidManifest.xml" {
            manifest = Some(entry);
        }
        manifest.is_none()
    }).map_err(|_| "Not an APK file")?;
    let manifest = manifest.ok_or("APK has no manifest")?;
    let mut xml = Vec::new();
    extract(window, &manifest, MAX_MANIFEST, &mut xml).map_err(|_| "APK manifest is damaged")?;
    manifest_identity(&xml)
}

const AMBIGUOUS: &str = "The APK manifest is ambiguous";

/// One string of a compiled-XML string pool chunk.
fn pool_string(pool: &[u8], index: u64) -> Option<String> {
    let count = le(pool, 8, 4)?;
    let utf8 = le(pool, 16, 4)? & 0x100 != 0;
    let header = le(pool, 2, 2)? as usize;
    if index >= count {
        return None;
    }
    let offset = le(pool, header.checked_add(index as usize * 4)?, 4)? as usize;
    let mut at = (le(pool, 20, 4)? as usize).checked_add(offset)?;
    if utf8 {
        // Character count, then byte count; each one or two bytes.
        if le(pool, at, 1)? & 0x80 != 0 { at += 1; }
        at += 1;
        let mut length = le(pool, at, 1)? as usize;
        at += 1;
        if length & 0x80 != 0 {
            length = ((length & 0x7F) << 8) | le(pool, at, 1)? as usize;
            at += 1;
        }
        if length > MAX_NAME { return None; }
        String::from_utf8(pool.get(at..at + length)?.to_vec()).ok()
    } else {
        let mut length = le(pool, at, 2)? as usize;
        at += 2;
        if length & 0x8000 != 0 {
            length = ((length & 0x7FFF) << 16) | le(pool, at, 2)? as usize;
            at += 2;
        }
        if length > MAX_NAME { return None; }
        let units: Vec<u16> = pool.get(at..at + length * 2)?
            .chunks_exact(2).map(|pair| u16::from_le_bytes([pair[0], pair[1]])).collect();
        String::from_utf16(&units).ok()
    }
}

/// Read package, version and split name from a compiled AndroidManifest.xml.
pub fn manifest_identity(xml: &[u8]) -> Result<Identity, String> {
    const UNREADABLE: &str = "Cannot read the APK manifest";
    const NO_NAMESPACE: u64 = 0xFFFF_FFFF;
    const VERSION_CODE: u64 = 0x0101_021b;
    const VERSION_CODE_MAJOR: u64 = 0x0101_0576;
    const ANDROID: &str = "http://schemas.android.com/apk/res/android";
    if le(xml, 0, 2) != Some(0x0003) {
        return Err(UNREADABLE.into());
    }
    let mut at = le(xml, 2, 2).ok_or(UNREADABLE)? as usize;
    let mut pool: Option<&[u8]> = None;
    let mut resource_ids: &[u8] = &[];
    while let (Some(kind), Some(header), Some(size)) = (le(xml, at, 2), le(xml, at + 2, 2), le(xml, at + 4, 4)) {
        let (header, size) = (header as usize, size as usize);
        let chunk = at.checked_add(size).and_then(|end| xml.get(at..end))
            .filter(|_| header >= 8 && header <= size).ok_or(UNREADABLE)?;
        match kind {
            0x0001 if pool.is_none() => pool = Some(chunk),
            0x0180 => resource_ids = &chunk[header..],
            0x0102 => {
                // The first element is <manifest>.
                let pool = pool.ok_or(UNREADABLE)?;
                let text = |index: u64| pool_string(pool, index);
                if text(le(chunk, header + 4, 4).ok_or(UNREADABLE)?).as_deref() != Some("manifest") {
                    return Err(UNREADABLE.into());
                }
                let start = header + le(chunk, header + 8, 2).ok_or(UNREADABLE)? as usize;
                let width = le(chunk, header + 10, 2).ok_or(UNREADABLE)? as usize;
                let count = le(chunk, header + 12, 2).ok_or(UNREADABLE)? as usize;
                if width < 20 {
                    return Err(UNREADABLE.into());
                }
                // Android reads these attributes in more than one way: its
                // install-time check finds `package` and `split` as the first
                // attribute of that name without a namespace, and the version
                // by name (android-13 ApkLiteParseUtils: android namespace,
                // first match; android-11: any namespace, last match), while
                // the full parse finds the version by resource ID. Accept only
                // a manifest every one of them reads the same way.
                let (mut package, mut split, mut code, mut major) = (None, None, None, None);
                let once = |slot: &mut Option<u64>, value: u64| slot.replace(value).map_or(Ok(()), |_| Err(AMBIGUOUS));
                for index in 0..count {
                    let attribute = start + index * width;
                    let field = |position: usize, size: usize| le(chunk, attribute + position, size).ok_or(UNREADABLE);
                    let (namespace, name, raw) = (field(0, 4)?, field(4, 4)?, field(8, 4)?);
                    let (kind, data) = (field(15, 1)?, field(16, 4)?);
                    let resource = le(resource_ids, name as usize * 4, 4);
                    let label = text(name);
                    let android = namespace != NO_NAMESPACE && text(namespace).as_deref() == Some(ANDROID);
                    let string = || text(if kind == 0x03 { data } else { raw }).ok_or(UNREADABLE);
                    let number = || (kind == 0x10 || kind == 0x11).then_some(data).ok_or(UNREADABLE);
                    for (wanted, id, slot) in [("versionCode", VERSION_CODE, &mut code), ("versionCodeMajor", VERSION_CODE_MAJOR, &mut major)] {
                        let (by_name, by_id) = (label.as_deref() == Some(wanted), resource == Some(id));
                        if by_name || by_id {
                            if !(by_name && by_id && android) {
                                return Err(AMBIGUOUS.into());
                            }
                            once(slot, number()?)?;
                        }
                    }
                    for (wanted, slot) in [("package", &mut package), ("split", &mut split)] {
                        if label.as_deref() == Some(wanted) && namespace == NO_NAMESPACE {
                            if slot.is_some() {
                                return Err(AMBIGUOUS.into());
                            }
                            *slot = Some(string()?);
                        }
                    }
                }
                let major = major.unwrap_or(0);
                let package = package.filter(|name| backend::valid_package(name))
                    .ok_or("APK has an invalid package name")?;
                let valid_split = |name: &String| !name.is_empty()
                    && name.bytes().all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'.');
                if split.as_ref().is_some_and(|name| !valid_split(name)) {
                    return Err("APK has an invalid split name".into());
                }
                // Android reads a missing version code as 0.
                return Ok(Identity { package, version_code: (major << 32) | code.unwrap_or(0), split });
            }
            _ => {}
        }
        at += size.max(8);
    }
    Err(UNREADABLE.into())
}

// --- Selection -------------------------------------------------------------

/// The last path component, shortened and stripped of control characters.
fn display_name(name: &str) -> String {
    let base = name.rsplit(['/', '\\']).next().unwrap_or(name);
    let clean: String = base.chars().filter(|c| !c.is_control()).take(120).collect();
    if clean.is_empty() { "file".into() } else { clean }
}

fn megabytes(bytes: u64) -> u64 {
    bytes.div_ceil(1024 * 1024)
}

fn free_space(directory: &Path) -> Option<u64> {
    use std::os::unix::ffi::OsStrExt;
    let path = std::ffi::CString::new(directory.as_os_str().as_bytes()).ok()?;
    let mut info: libc::statvfs = unsafe { std::mem::zeroed() };
    // statvfs only fills the caller's struct for an existing path.
    #[allow(clippy::unnecessary_cast)]
    (unsafe { libc::statvfs(path.as_ptr(), &mut info) } == 0)
        .then(|| (info.f_bavail as u64).saturating_mul(info.f_frsize as u64))
}

struct Candidate {
    part: Part,
    identity: Identity,
    name: String,
}

struct Bundle {
    candidates: Vec<Candidate>,
    obb_files: u64,
    obb_bytes: u64,
}

/// Choose the APK entries of a bundle. A bundletool archive keeps the split
/// set under `splits/` next to standalone variants that must not be mixed
/// in; every other format keeps the APKs at the top level.
fn bundle_entries(window: Window) -> Result<(Vec<Entry>, u64, u64), String> {
    let (mut top, mut splits) = (Vec::new(), Vec::new());
    let (mut obb_files, mut obb_bytes) = (0u64, 0u64);
    scan(window, MAX_BUNDLE_ENTRIES, |entry| {
        let name = entry.name.replace('\\', "/").to_ascii_lowercase();
        if name.ends_with('/') || name.starts_with("__macosx/") || entry.size == 0 {
        } else if name.ends_with(".obb") {
            obb_files += 1;
            obb_bytes = obb_bytes.saturating_add(entry.size);
        } else if name.ends_with(".apk") {
            match name.rsplit_once('/') {
                None => top.push(entry),
                Some(("splits", _)) => splits.push(entry),
                Some(_) => {}
            }
        }
        true
    }).map_err(|error| if error.starts_with("Not a valid ZIP") {
        "Not a ZIP-based package bundle (the file is damaged or encrypted)".to_string()
    } else { error })?;
    let chosen = if splits.is_empty() { top } else { splits };
    if chosen.is_empty() {
        return Err("The bundle has no APK files at its top level".into());
    }
    if chosen.len() > MAX_PARTS {
        return Err(format!("The bundle has more than {MAX_PARTS} APK files"));
    }
    if chosen.iter().any(|entry| entry.size > MAX_APK)
        || chosen.iter().fold(0u64, |total, entry| total.saturating_add(entry.size)) > MAX_SET {
        return Err("The bundle is too large: one APK may be up to 2 GiB and all of them up to 4 GiB".into());
    }
    Ok((chosen, obb_files, obb_bytes))
}

fn read_bundle(path: &Path, pack: &Path, budget: &mut u64) -> Result<Bundle, String> {
    let file = open_regular(path).map_err(|error| format!("Cannot read the file: {error}"))?;
    let len = file.metadata().map_err(|error| error.to_string())?.len();
    let window = Window { file: &file, start: 0, len };
    let (entries, obb_files, obb_bytes) = bundle_entries(window)?;
    let total: u64 = entries.iter().map(|entry| entry.size).sum();
    let directory = pack.parent().ok_or("Invalid cache folder")?;
    if total > *budget {
        return Err(format!(
            "The selected bundles unpack to more than {} GiB in all. Install them a few at a time.",
            MAX_UNPACKED >> 30));
    }
    if free_space(directory).is_some_and(|free| free < total.saturating_add(FREE_RESERVE)) {
        return Err(format!(
            "Not enough free disk space to unpack the bundle: {} MB and {} MB to spare are needed in {}",
            megabytes(total), megabytes(FREE_RESERVE), directory.display()));
    }
    *budget -= total;
    let mut out = fs::OpenOptions::new().read(true).write(true).create_new(true).mode(0o600).open(pack)
        .map_err(|error| format!("Cannot unpack: {error}"))?;
    let mut ranges = Vec::new();
    let mut offset = 0u64;
    {
        let mut writer = io::BufWriter::with_capacity(1 << 20, &mut out);
        for entry in &entries {
            extract(window, entry, MAX_APK, &mut writer)
                .map_err(|error| format!("{}: {error}", display_name(&entry.name)))?;
            ranges.push((offset, entry.size, display_name(&entry.name)));
            offset += entry.size;
        }
        writer.flush().map_err(|error| format!("Cannot unpack: {error}"))?;
    }
    let out = Arc::new(out);
    let mut candidates = Vec::new();
    for (offset, size, name) in ranges {
        let identity = apk_identity(Window { file: &out, start: offset, len: size })
            .map_err(|error| format!("{name}: {error}"))?;
        let part = Part::new(out.clone(), offset, size, identity.split.clone())
            .map_err(|error| format!("Cannot unpack: {error}"))?;
        candidates.push(Candidate { part, identity, name });
    }
    Ok(Bundle { candidates, obb_files, obb_bytes })
}

fn read_loose(path: &Path) -> Result<(Part, Result<Identity, String>), String> {
    let file = open_regular(path).map_err(|error| format!("Cannot read the file: {error}"))?;
    let size = file.metadata().map_err(|error| error.to_string())?.len();
    if size == 0 || size > MAX_APK {
        return Err("An APK must be between 1 byte and 2 GiB".into());
    }
    let mut magic = [0u8; 4];
    if file.read_exact_at(&mut magic, 0).is_err() || &magic != b"PK\x03\x04" {
        return Err("Not an APK file".into());
    }
    let identity = apk_identity(Window { file: &file, start: 0, len: size });
    let split = identity.as_ref().ok().and_then(|identity| identity.split.clone());
    let part = Part::new(Arc::new(file), 0, size, split).map_err(|error| format!("Cannot read the file: {error}"))?;
    Ok((part, identity))
}

/// Turn candidates that share one origin into install sets, one per package
/// and version. Android refuses a session unless it has exactly one base
/// APK and every split has the same package, version and a distinct name.
fn group(candidates: Vec<Candidate>, bundle: Option<&str>, packages: &mut Vec<PackageSet>, rejected: &mut Vec<Rejected>) {
    let mut groups: Vec<Vec<Candidate>> = Vec::new();
    for candidate in candidates {
        match groups.iter_mut().find(|group| group[0].identity.package == candidate.identity.package
            && group[0].identity.version_code == candidate.identity.version_code) {
            Some(group) => group.push(candidate),
            None => groups.push(vec![candidate]),
        }
    }
    for mut members in groups {
        let package = members[0].identity.package.clone();
        let version_code = members[0].identity.version_code;
        // Base first, then splits by name: a stable order for the session.
        members.sort_by(|a, b| a.identity.split.cmp(&b.identity.split));
        let bases = members.iter().filter(|member| member.identity.split.is_none()).count();
        let repeated = members.windows(2).any(|pair| pair[0].identity.split.is_some()
            && pair[0].identity.split == pair[1].identity.split);
        let reason = if backend::uninstall_block_reason(&package).is_some() {
            Some("Runtime components must be updated through the runtime installer.".to_string())
        } else if bases == 0 {
            Some(format!("This is a split APK of {package} and its base APK is not selected. Select the base APK and all of its splits together."))
        } else if bases > 1 {
            Some(format!("More than one base APK of {package} (version code {version_code}) is selected. Select each app once."))
        } else if repeated {
            Some(format!("The same split APK of {package} is selected twice."))
        } else if members.len() > MAX_PARTS || members.iter().map(|member| member.part.size).sum::<u64>() > MAX_SET {
            Some(format!("{package} is too large: up to {MAX_PARTS} APKs and 4 GiB in total."))
        } else {
            None
        };
        if let Some(reason) = reason {
            match bundle {
                Some(name) => rejected.push(Rejected { file: name.to_owned(), reason }),
                None => rejected.extend(members.iter().map(|member| Rejected { file: member.name.clone(), reason: reason.clone() })),
            }
            continue;
        }
        packages.push(PackageSet {
            package,
            version_code,
            label: bundle.map_or_else(|| members[0].name.clone(), str::to_owned),
            parts: members.into_iter().map(|member| member.part).collect(),
            obb_files: 0,
            obb_bytes: 0,
            manifest_error: None,
        });
    }
}

/// Remove unpacked data left by a process that no longer exists.
fn remove_stale(cache: &Path) {
    let Ok(entries) = fs::read_dir(cache) else { return };
    for entry in entries.flatten() {
        let name = entry.file_name();
        let Some(rest) = name.to_str().and_then(|name| name.strip_prefix("install-")) else { continue };
        let Some(pid) = rest.split('-').next().and_then(|pid| pid.parse::<u32>().ok()) else { continue };
        if pid != std::process::id() && !Path::new(&format!("/proc/{pid}")).exists()
            && entry.file_type().is_ok_and(|kind| kind.is_dir()) {
            let _ = fs::remove_dir_all(entry.path());
        }
    }
}

/// Inspect the selected files and decide what will be installed.
///
/// Split selection: every split of a package is installed. Android accepts
/// any consistent set and picks ABI, density and language resources itself,
/// so nothing a device needs can be left out by a wrong guess here.
pub fn prepare(paths: &[PathBuf]) -> Result<Prepared, AnvilError> {
    prepare_in(paths, &config::cache_dir())
}

fn prepare_in(paths: &[PathBuf], cache: &Path) -> Result<Prepared, AnvilError> {
    if paths.is_empty() || paths.len() > MAX_FILES {
        return Err(AnvilError::InvalidArgument(format!("Select between 1 and {MAX_FILES} files")));
    }
    remove_stale(cache);
    let mut prepared = Prepared { packages: Vec::new(), rejected: Vec::new(), directory: None };
    let mut loose = Vec::new();
    let mut budget = MAX_UNPACKED;
    let mut seen: Vec<PathBuf> = Vec::new();
    for (index, path) in paths.iter().enumerate() {
        let name = display_name(&path.to_string_lossy());
        let reject = |prepared: &mut Prepared, reason: String| prepared.rejected.push(Rejected { file: name.clone(), reason });
        let path = match path.canonicalize() {
            Ok(path) => path,
            Err(error) => { reject(&mut prepared, format!("Cannot read the file: {error}")); continue; }
        };
        if seen.contains(&path) { continue; }
        seen.push(path.clone());
        let extension = path.extension().and_then(|e| e.to_str()).map(str::to_ascii_lowercase);
        match extension.as_deref() {
            Some("apk") => match read_loose(&path) {
                Ok((part, Ok(identity))) => loose.push(Candidate { part, identity, name }),
                // A manifest that reads two ways is never passed on: which app it
                // is would be decided by Android after our checks were skipped.
                Ok((_, Err(error))) if error == AMBIGUOUS => reject(&mut prepared, format!("{error}; it is not installed.")),
                // Android may still accept a manifest this reader cannot parse.
                Ok((part, Err(error))) => prepared.packages.push(PackageSet {
                    package: String::new(), version_code: 0, label: name, parts: vec![part],
                    obb_files: 0, obb_bytes: 0, manifest_error: Some(error),
                }),
                Err(reason) => reject(&mut prepared, reason),
            },
            Some("apks" | "xapk" | "apkm") => {
                let directory = match &prepared.directory {
                    Some(directory) => directory.clone(),
                    None => {
                        fs::create_dir_all(cache)?;
                        let directory = cache.join(format!("install-{}", unique()));
                        fs::DirBuilder::new().mode(0o700).create(&directory)?;
                        prepared.directory = Some(directory.clone());
                        directory
                    }
                };
                let pack = directory.join(format!("bundle-{index}"));
                match read_bundle(&path, &pack, &mut budget) {
                    Ok(bundle) => {
                        let first = prepared.packages.len();
                        group(bundle.candidates, Some(&name), &mut prepared.packages, &mut prepared.rejected);
                        if let Some(set) = prepared.packages.get_mut(first) {
                            set.obb_files = bundle.obb_files;
                            set.obb_bytes = bundle.obb_bytes;
                        }
                    }
                    Err(reason) => { let _ = fs::remove_file(&pack); reject(&mut prepared, reason); }
                }
            }
            _ => reject(&mut prepared, "Not an Android package. Choose .apk, .apks, .xapk or .apkm files.".into()),
        }
    }
    let Prepared { packages, rejected, .. } = &mut prepared;
    group(loose, None, packages, rejected);
    Ok(prepared)
}

/// What a set is, for a result line: `com.game (game.xapk, 4 APKs)`.
pub fn describe(set: &PackageSet) -> String {
    let files = match set.parts.len() {
        1 => String::new(),
        count => format!(", {count} APKs"),
    };
    if set.package.is_empty() { set.label.clone() } else { format!("{} ({}{files})", set.package, set.label) }
}

/// The plain statement about expansion files that were left out, if any.
pub fn obb_notice(set: &PackageSet) -> Option<String> {
    (set.obb_files > 0).then(|| format!(
        "{} expansion file{} (OBB, {} MB) in {} {} not installed. AnvilDroid installs the APKs only; the app may download its data on first start, or may not run without it.",
        set.obb_files, if set.obb_files == 1 { "" } else { "s" }, megabytes(set.obb_bytes), set.label,
        if set.obb_files == 1 { "was" } else { "were" }))
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use std::io::{Seek, SeekFrom};

    const NONE: u32 = 0xFFFF_FFFF;
    const ANDROID: &str = "http://schemas.android.com/apk/res/android";

    /// A compiled manifest holding only what the installer reads.
    pub(crate) fn manifest(package: &str, version: u32, split: Option<&str>, utf8: bool) -> Vec<u8> {
        let mut strings = vec!["versionCode", "package", "manifest", package, ANDROID];
        let mut attributes = vec![(4, 0, 0x10, version), (NONE, 1, 0x03, 3)];
        if let Some(split) = split {
            strings.extend(["split", split]);
            attributes.push((NONE, 5, 0x03, 6));
        }
        compiled(&strings, &[0x0101_021b], &attributes, utf8)
    }

    /// A compiled <manifest> element. Strings: index 2 must be "manifest".
    /// Attributes: (namespace string or NONE, name string, value type, value).
    fn compiled(strings: &[&str], resource_ids: &[u32], attributes: &[(u32, u32, u8, u32)], utf8: bool) -> Vec<u8> {
        let mut data = Vec::new();
        let mut offsets = Vec::new();
        for text in strings {
            offsets.push(data.len() as u32);
            if utf8 {
                data.extend([text.chars().count() as u8, text.len() as u8]);
                data.extend(text.as_bytes());
                data.push(0);
            } else {
                let units: Vec<u16> = text.encode_utf16().collect();
                data.extend((units.len() as u16).to_le_bytes());
                for unit in units { data.extend(unit.to_le_bytes()); }
                data.extend([0, 0]);
            }
        }
        while data.len() % 4 != 0 { data.push(0); }
        let words = |values: &[u32]| values.iter().flat_map(|value| value.to_le_bytes()).collect::<Vec<u8>>();
        let chunk = |kind: u16, header: u16, body: Vec<u8>| {
            let mut chunk = Vec::new();
            chunk.extend(kind.to_le_bytes());
            chunk.extend(header.to_le_bytes());
            chunk.extend((8 + body.len() as u32).to_le_bytes());
            chunk.extend(body);
            chunk
        };
        let count = strings.len() as u32;
        let mut pool = words(&[count, 0, if utf8 { 0x100 } else { 0 }, 28 + count * 4, 0]);
        pool.extend(words(&offsets));
        pool.extend(data);
        let attributes: Vec<Vec<u8>> = attributes.iter().map(|&(namespace, name, kind, value)| {
            let mut bytes = words(&[namespace, name, if kind == 0x03 { value } else { NONE }]);
            bytes.extend([8, 0, 0, kind]);
            bytes.extend(value.to_le_bytes());
            bytes
        }).collect();
        let mut element = words(&[1, NONE, NONE, 2]);
        for value in [20u16, 20, attributes.len() as u16, 0, 0, 0] { element.extend(value.to_le_bytes()); }
        element.extend(attributes.concat());
        let mut body = chunk(0x0001, 28, pool);
        body.extend(chunk(0x0180, 8, words(resource_ids)));
        body.extend(chunk(0x0102, 16, element));
        chunk(0x0003, 8, body)
    }

    /// A ZIP archive; `deflate` compresses every entry.
    pub(crate) fn zip(files: &[(&str, &[u8])], deflate: bool) -> Vec<u8> {
        let mut out = Vec::new();
        let mut directory = Vec::new();
        for (name, content) in files {
            let mut crc = flate2::Crc::new();
            crc.update(content);
            let packed = if deflate {
                let mut encoder = flate2::write::DeflateEncoder::new(Vec::new(), flate2::Compression::fast());
                encoder.write_all(content).unwrap();
                encoder.finish().unwrap()
            } else { content.to_vec() };
            let offset = out.len() as u32;
            let mut common = Vec::new();
            for value in [20u16, 0, if deflate { 8 } else { 0 }, 0, 0] { common.extend(value.to_le_bytes()); }
            for value in [crc.sum(), packed.len() as u32, content.len() as u32] { common.extend(value.to_le_bytes()); }
            common.extend((name.len() as u16).to_le_bytes());
            out.extend(b"PK\x03\x04");
            out.extend(&common);
            out.extend([0, 0]);
            out.extend(name.as_bytes());
            out.extend(&packed);
            directory.extend(b"PK\x01\x02\x14\x00");
            directory.extend(&common);
            directory.extend([0u8; 12]);
            directory.extend(offset.to_le_bytes());
            directory.extend(name.as_bytes());
        }
        let start = out.len() as u32;
        out.extend(&directory);
        out.extend(b"PK\x05\x06\0\0\0\0");
        out.extend((files.len() as u16).to_le_bytes());
        out.extend((files.len() as u16).to_le_bytes());
        out.extend((directory.len() as u32).to_le_bytes());
        out.extend(start.to_le_bytes());
        out.extend([0, 0]);
        out
    }

    pub(crate) fn apk(package: &str, version: u32, split: Option<&str>) -> Vec<u8> {
        zip(&[("classes.dex", b"dex"), ("AndroidManifest.xml", &manifest(package, version, split, true))], true)
    }

    pub(crate) fn scratch(name: &str) -> PathBuf {
        let directory = std::env::temp_dir().join(format!("anvildroid-apkset-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&directory);
        fs::create_dir_all(&directory).unwrap();
        directory.canonicalize().unwrap()
    }

    fn write(directory: &Path, name: &str, content: &[u8]) -> PathBuf {
        let path = directory.join(name);
        fs::write(&path, content).unwrap();
        path
    }

    fn part_bytes(part: &Part) -> Vec<u8> {
        let mut bytes = Vec::new();
        part.copy_to(&mut bytes).unwrap();
        bytes
    }

    #[test]
    fn manifest_identity_reads_both_string_encodings() {
        for utf8 in [true, false] {
            assert_eq!(manifest_identity(&manifest("com.example.game", 42, None, utf8)).unwrap(),
                Identity { package: "com.example.game".into(), version_code: 42, split: None });
            assert_eq!(manifest_identity(&manifest("com.example.game", 42, Some("config.arm64_v8a"), utf8)).unwrap().split.as_deref(),
                Some("config.arm64_v8a"));
        }
        assert!(manifest_identity(&manifest("bad;name", 1, None, true)).unwrap_err().contains("package name"));
        assert!(manifest_identity(&manifest("com.example.game", 1, Some("../x"), true)).unwrap_err().contains("split name"));
        // Damaged or hostile input is an error, never a panic.
        let good = manifest("com.example.game", 42, Some("config.en"), false);
        for cut in 0..good.len() { let _ = manifest_identity(&good[..cut]); }
        for at in 0..good.len() {
            let mut bad = good.clone();
            bad[at] = 0xFF;
            let _ = manifest_identity(&bad);
        }
        assert!(manifest_identity(b"<manifest/>").is_err());
    }

    #[test]
    fn a_manifest_android_could_read_two_ways_is_refused() {
        // Strings: 0 versionCode, 1 package, 2 manifest, 3 the package, 4 android namespace,
        // 5 another package, 6 split, 7 a split name, 8 another split name, 9 versionCodeMajor,
        // 10 an attribute Android does not know.
        let strings = ["versionCode", "package", "manifest", "com.example.game", ANDROID,
            "org.anvildroid.ime", "split", "config.en", "config.de", "versionCodeMajor", "other"];
        let ids = [0x0101_021b, 0, 0, 0, 0, 0, 0, 0, 0, 0x0101_0576];
        let read = |attributes: &[(u32, u32, u8, u32)]| manifest_identity(&compiled(&strings, &ids, attributes, true));
        let (version, package) = ((4, 0, 0x10, 7), (NONE, 1, 0x03, 3));
        assert_eq!(read(&[version, package, (4, 9, 0x10, 2), (NONE, 6, 0x03, 7), (NONE, 10, 0x03, 5)]).unwrap(),
            Identity { package: "com.example.game".into(), version_code: (2 << 32) | 7, split: Some("config.en".into()) });
        // No version code at all is version 0, as Android reads it.
        assert_eq!(read(&[package]).unwrap().version_code, 0);
        let ambiguous = |attributes: &[(u32, u32, u8, u32)]| assert_eq!(read(attributes).unwrap_err(), "The APK manifest is ambiguous", "{attributes:?}");
        // A second `package` or `split`: Android takes the first, a careless reader the last.
        ambiguous(&[version, package, (NONE, 1, 0x03, 5)]);
        ambiguous(&[version, (NONE, 1, 0x03, 5), package]);
        ambiguous(&[version, package, (NONE, 6, 0x03, 7), (NONE, 6, 0x03, 8)]);
        // A version code Android's readers disagree on: twice, without the android
        // namespace (read by Android 11, not by 13), or by name without its resource ID.
        ambiguous(&[version, (4, 0, 0x10, 9), package]);
        ambiguous(&[(NONE, 0, 0x10, 7), package]);
        ambiguous(&[version, package, (4, 9, 0x10, 1), (4, 9, 0x10, 2)]);
        let unnamed = ["", "package", "manifest", "com.example.game", ANDROID];
        assert_eq!(manifest_identity(&compiled(&unnamed, &[0x0101_021b], &[version, package], true)).unwrap_err(), "The APK manifest is ambiguous");
        let renamed = ["other", "package", "manifest", "com.example.game", ANDROID, "versionCode"];
        assert_eq!(manifest_identity(&compiled(&renamed, &[0x0101_021b], &[(4, 5, 0x10, 7), package], true)).unwrap_err(), "The APK manifest is ambiguous");
        // Such a file is refused outright, on every kind of device; it is not left to Android.
        let directory = scratch("ambiguous");
        let twice = compiled(&strings, &ids, &[version, package, (NONE, 1, 0x03, 5)], true);
        let path = write(&directory, "twice.apk", &zip(&[("AndroidManifest.xml", &twice)], true));
        let prepared = prepare(&[path]).unwrap();
        assert!(prepared.packages.is_empty());
        assert_eq!(prepared.rejected[0].reason, "The APK manifest is ambiguous; it is not installed.");
        fs::remove_dir_all(directory).unwrap();
        // `package` inside a namespace is not the package name.
        assert!(read(&[version, (4, 1, 0x03, 5)]).unwrap_err().contains("invalid package name"));
        assert_eq!(read(&[version, package, (4, 1, 0x03, 5)]).unwrap().package, "com.example.game");
    }

    #[test]
    fn a_file_replaced_or_rewritten_after_inspection_is_not_installed_in_its_place() {
        let directory = scratch("swap");
        let path = write(&directory, "game.apk", &apk("com.example.game", 7, None));
        let split = write(&directory, "split.apk", &apk("com.example.game", 7, Some("config.en")));
        let original = fs::read(&path).unwrap();
        let prepared = prepare(&[path.clone(), split.clone()]).unwrap();
        let set = &prepared.packages[0];
        // The path now names another file. What is sent is the inspected file or
        // nothing (most filesystems mark the rename as a change): never the new one.
        fs::rename(&path, directory.join("moved.apk")).unwrap();
        write(&directory, "game.apk", &apk("org.anvildroid.ime", 1, None));
        let mut copied = Vec::new();
        match set.parts[0].copy_to(&mut copied) {
            Ok(()) => assert_eq!(copied, original),
            Err(error) => assert!(error.to_string().contains("changed after it was selected")),
        }
        match set.transfer_file() {
            Ok((mut file, sizes)) => {
                let mut joined = vec![0u8; sizes[0] as usize];
                file.seek(SeekFrom::Start(0)).unwrap();
                file.read_exact(&mut joined).unwrap();
                assert_eq!(joined, original);
            }
            Err(error) => assert!(error.to_string().contains("changed after it was selected")),
        }
        // Written in place after inspection: refused, for a copy and for the direct hand-over.
        std::thread::sleep(std::time::Duration::from_millis(20));
        fs::OpenOptions::new().write(true).open(directory.join("moved.apk")).unwrap()
            .write_all_at(&apk("org.anvildroid.ime", 1, None), 0).unwrap();
        let mut sink = Vec::new();
        assert!(set.parts[0].copy_to(&mut sink).unwrap_err().to_string().contains("changed after it was selected"));
        assert!(set.transfer_file().unwrap_err().to_string().contains("changed after it was selected"));
        let lone = write(&directory, "lone.apk", &apk("org.example.tool", 1, None));
        let prepared = prepare(std::slice::from_ref(&lone)).unwrap();
        assert!(prepared.packages[0].transfer_file().is_ok());
        std::thread::sleep(std::time::Duration::from_millis(20));
        fs::OpenOptions::new().append(true).open(&lone).unwrap().write_all(b"more").unwrap();
        assert!(prepared.packages[0].transfer_file().unwrap_err().to_string().contains("changed after it was selected"));
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn one_request_unpacks_a_bounded_amount() {
        let directory = scratch("budget");
        let cache = directory.join("cache");
        fs::create_dir_all(&cache).unwrap();
        let bundle = write(&directory, "game.xapk", &zip(&[("base.apk", &apk("com.example.game", 9, None))], true));
        let size = apk("com.example.game", 9, None).len() as u64;
        let pack = cache.join("bundle-0");
        let mut budget = size - 1;
        assert!(read_bundle(&bundle, &pack, &mut budget).err().unwrap().contains("unpack to more than 8 GiB in all"));
        assert!(!pack.exists());
        let mut budget = size + 5;
        assert_eq!(read_bundle(&bundle, &pack, &mut budget).unwrap().candidates.len(), 1);
        assert_eq!(budget, 5);
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn loose_files_are_grouped_into_one_session_per_package() {
        let directory = scratch("loose");
        let paths = vec![
            write(&directory, "split_config.en.apk", &apk("com.example.game", 7, Some("config.en"))),
            write(&directory, "other.apk", &apk("org.example.tool", 3, None)),
            write(&directory, "game.apk", &apk("com.example.game", 7, None)),
            write(&directory, "split_config.arm64_v8a.apk", &apk("com.example.game", 7, Some("config.arm64_v8a"))),
        ];
        let prepared = prepare(&paths).unwrap();
        assert!(prepared.rejected.is_empty(), "{:?}", prepared.rejected);
        assert_eq!(prepared.packages.len(), 2);
        let game = &prepared.packages[0];
        assert_eq!((game.package.as_str(), game.version_code, game.label.as_str()), ("com.example.game", 7, "game.apk"));
        // Base first, then every split: nothing is filtered out.
        assert_eq!(game.parts.iter().map(|part| part.split.as_deref()).collect::<Vec<_>>(),
            [None, Some("config.arm64_v8a"), Some("config.en")]);
        assert_eq!(part_bytes(&game.parts[0]), fs::read(&paths[2]).unwrap());
        assert_eq!(prepared.packages[1].package, "org.example.tool");
        assert_eq!(describe(game), "com.example.game (game.apk, 3 APKs)");
        // Loose files are joined for the controller; sizes mark the borders.
        let (mut file, sizes) = game.transfer_file().unwrap();
        let mut joined = Vec::new();
        file.seek(SeekFrom::Start(0)).unwrap();
        file.read_to_end(&mut joined).unwrap();
        assert_eq!(sizes, game.parts.iter().map(|part| part.size).collect::<Vec<_>>());
        assert_eq!(joined, game.parts.iter().flat_map(part_bytes).collect::<Vec<_>>());
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn sets_android_would_refuse_are_rejected_with_a_reason() {
        let directory = scratch("refused");
        let reasons = |paths: &[PathBuf]| {
            let prepared = prepare(paths).unwrap();
            assert!(prepared.packages.is_empty());
            prepared.rejected.iter().map(|rejected| format!("{}: {}", rejected.file, rejected.reason)).collect::<Vec<_>>().join("\n")
        };
        let split = write(&directory, "split.apk", &apk("com.example.game", 7, Some("config.en")));
        assert!(reasons(std::slice::from_ref(&split)).contains("split.apk: This is a split APK of com.example.game and its base APK is not selected"));
        // A split of another version is not part of this base's session.
        let base = write(&directory, "base.apk", &apk("com.example.game", 8, None));
        let prepared = prepare(&[base.clone(), split.clone()]).unwrap();
        assert_eq!((prepared.packages.len(), prepared.packages[0].parts.len(), prepared.rejected.len()), (1, 1, 1));
        let copy = write(&directory, "copy.apk", &apk("com.example.game", 8, None));
        assert!(reasons(&[base.clone(), copy]).contains("More than one base APK"));
        let twice = write(&directory, "again.apk", &apk("com.example.game", 7, Some("config.en")));
        let old = write(&directory, "old.apk", &apk("com.example.game", 7, None));
        assert!(reasons(&[old, split, twice]).contains("selected twice"));
        for package in ["org.anvildroid.ime", "com.android.settings", "cu.axel.smartdock"] {
            let component = write(&directory, "component.apk", &apk(package, 1, None));
            assert!(reasons(&[component]).contains("Runtime components must be updated through the runtime installer"));
        }
        let text = write(&directory, "notes.txt", b"PK\x03\x04");
        assert!(reasons(&[text]).contains("Not an Android package"));
        let fake = write(&directory, "fake.apk", b"not a zip");
        assert!(reasons(&[fake]).contains("Not an APK file"));
        assert!(reasons(&[directory.join("missing.apk")]).contains("Cannot read the file"));
        fs::create_dir(directory.join("folder.apk")).unwrap();
        assert!(reasons(&[directory.join("folder.apk")]).contains("Cannot read the file"));
        assert!(prepare(&[]).is_err());
        assert!(prepare(&vec![base.clone(); MAX_FILES + 1]).is_err());
        // The same file selected twice counts once.
        assert_eq!(prepare(&[base.clone(), base]).unwrap().packages.len(), 1);
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn an_apk_with_an_unreadable_manifest_is_left_to_android() {
        let directory = scratch("unreadable");
        let odd = write(&directory, "odd.apk", &zip(&[("AndroidManifest.xml", b"\x03\x00\x08\x00junk")], false));
        let prepared = prepare(&[odd]).unwrap();
        assert!(prepared.rejected.is_empty());
        let set = &prepared.packages[0];
        assert_eq!((set.package.as_str(), set.parts.len(), set.label.as_str()), ("", 1, "odd.apk"));
        assert!(set.manifest_error.is_some());
        assert_eq!(describe(set), "odd.apk");
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn bundles_are_unpacked_by_content_and_never_by_entry_path() {
        let directory = scratch("bundle");
        let cache = directory.join("cache");
        let base = apk("com.example.game", 9, None);
        let arm = apk("com.example.game", 9, Some("config.arm64_v8a"));
        let dpi = apk("com.example.game", 9, Some("config.xxhdpi"));
        let obb = vec![7u8; 3000];
        for deflate in [false, true] {
            // .xapk: APKs at the top level, expansion files and metadata beside them.
            let xapk = write(&directory, "Game.xapk", &zip(&[
                ("manifest.json", b"{\"package_name\":\"org.anvildroid.ime\"}"),
                ("config.arm64_v8a.apk", &arm), ("com.example.game.apk", &base), ("config.xxhdpi.apk", &dpi),
                ("Android/obb/com.example.game/main.9.com.example.game.obb", &obb),
                ("icon.png", b"png"), ("nested/other.apk", &apk("org.example.tool", 1, None)),
                ("../../escape.apk", &apk("org.example.escape", 1, None)), ("empty.apk", b""),
            ], deflate));
            let prepared = prepare_in(std::slice::from_ref(&xapk), &cache).unwrap();
            assert!(prepared.rejected.is_empty(), "{:?}", prepared.rejected);
            assert_eq!(prepared.packages.len(), 1);
            let set = &prepared.packages[0];
            assert_eq!((set.package.as_str(), set.version_code, set.label.as_str()), ("com.example.game", 9, "Game.xapk"));
            assert_eq!(set.parts.iter().map(|part| part.split.as_deref()).collect::<Vec<_>>(),
                [None, Some("config.arm64_v8a"), Some("config.xxhdpi")]);
            assert_eq!(part_bytes(&set.parts[0]), base);
            assert_eq!(part_bytes(&set.parts[2]), dpi);
            assert_eq!((set.obb_files, set.obb_bytes), (1, 3000));
            assert!(obb_notice(set).unwrap().contains("1 expansion file (OBB, 1 MB) in Game.xapk was not installed"));
            // Everything unpacked is one private file in the cache folder.
            let unpacked = prepared.directory.clone().unwrap();
            assert!(unpacked.starts_with(&cache));
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(fs::metadata(&unpacked).unwrap().permissions().mode() & 0o777, 0o700);
            assert_eq!(fs::read_dir(&unpacked).unwrap().count(), 1);
            // The unpacked file goes to the controller as it is.
            let (file, sizes) = set.transfer_file().unwrap();
            assert_eq!(sizes.len(), 3);
            assert_eq!(file.metadata().unwrap().len(), set.bytes());
            drop(prepared);
            assert!(!unpacked.exists());
            assert!(!directory.join("escape.apk").exists() && !directory.parent().unwrap().join("escape.apk").exists());
        }
        // bundletool .apks: only splits/ is one consistent set.
        let apks = write(&directory, "game.apks", &zip(&[
            ("toc.pb", b"toc"), ("splits/base-master.apk", &base), ("splits/base-arm64_v8a.apk", &arm),
            ("standalones/standalone-x86.apk", &apk("com.example.game", 9, None)),
        ], true));
        let prepared = prepare_in(&[apks], &cache).unwrap();
        assert_eq!((prepared.packages.len(), prepared.packages[0].parts.len(), prepared.rejected.len()), (1, 2, 0));
        assert!(obb_notice(&prepared.packages[0]).is_none());
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn hostile_bundles_are_refused_without_leaving_files() {
        let directory = scratch("hostile");
        let cache = directory.join("cache");
        let base = apk("com.example.game", 9, None);
        let reason = |name: &str, content: &[u8]| {
            let prepared = prepare_in(&[write(&directory, name, content)], &cache).unwrap();
            assert!(prepared.packages.is_empty());
            let reason = prepared.rejected[0].reason.clone();
            drop(prepared);
            assert_eq!(fs::read_dir(&cache).unwrap().count(), 0, "{name} left files behind");
            reason
        };
        assert!(reason("empty.xapk", &zip(&[("readme.txt", b"x"), ("deep/a.apk", &base)], false)).contains("no APK files"));
        assert!(reason("encrypted.apkm", b"\x00\x01\x02 not a zip at all").contains("damaged or encrypted"));
        assert!(reason("mixed.apks", &zip(&[("a.apk", &base), ("b.apk", &apk("com.example.game", 9, None))], false)).contains("More than one base APK"));
        assert!(reason("component.xapk", &zip(&[("a.apk", &apk("org.anvildroid.desktop", 1, None))], false)).contains("Runtime components"));
        assert!(reason("junk.xapk", &zip(&[("a.apk", b"not an apk")], false)).contains("a.apk: Not an APK file"));
        // A lying size, a wrong checksum and an entry pointing outside the file.
        let good = zip(&[("a.apk", &base)], true);
        let directory_at = good.windows(4).rposition(|window| window == b"PK\x01\x02").unwrap();
        let mut bomb = good.clone();
        bomb[directory_at + 24..directory_at + 28].copy_from_slice(&8u32.to_le_bytes());
        assert!(reason("bomb.xapk", &bomb).contains("damaged"));
        let mut checksum = good.clone();
        checksum[directory_at + 16] ^= 0xFF;
        assert!(reason("checksum.xapk", &checksum).contains("damaged"));
        let mut outside = good.clone();
        outside[directory_at + 42..directory_at + 46].copy_from_slice(&0x7FFF_FFFFu32.to_le_bytes());
        assert!(reason("outside.xapk", &outside).contains("truncated"));
        let mut huge = good.clone();
        huge[directory_at + 24..directory_at + 28].copy_from_slice(&0xFFFF_FFF0u32.to_le_bytes());
        assert!(reason("huge.xapk", &huge).contains("too large"));
        let mut locked = good.clone();
        locked[directory_at + 8] |= 1;
        assert!(reason("locked.xapk", &locked).contains("password"));
        // Truncation anywhere is an error, never a panic or a partial install.
        for cut in (0..good.len()).step_by(7) {
            let prepared = prepare_in(&[write(&directory, "cut.xapk", &good[..cut])], &cache).unwrap();
            assert!(prepared.packages.is_empty(), "cut at {cut}");
        }
        let many: Vec<(String, Vec<u8>)> = (0..MAX_PARTS + 1).map(|index| (format!("{index}.apk"), b"x".to_vec())).collect();
        let many: Vec<(&str, &[u8])> = many.iter().map(|(name, content)| (name.as_str(), content.as_slice())).collect();
        assert!(reason("many.xapk", &zip(&many, false)).contains("more than 512 APK files"));
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn data_left_by_a_dead_process_is_removed() {
        let cache = scratch("stale");
        let dead = cache.join("install-4194304-1");
        let own = cache.join(format!("install-{}-1", std::process::id()));
        let other = cache.join("images");
        for directory in [&dead, &own, &other] { fs::create_dir(directory).unwrap(); }
        remove_stale(&cache);
        assert!(!dead.exists() && own.exists() && other.exists());
        fs::remove_dir_all(cache).unwrap();
    }
}
