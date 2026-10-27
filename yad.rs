//! yad — Yandex Music downloader. Queries are answered here (URL check, clipboard, token
//! presence); Python (plugins/yad/yad.py + ymd) runs only for the download itself.
//! The scripts are embedded and unpacked to %APPDATA%\LiteLaunch\yad.

use crate::search::{Action, Item};
use serde::{Deserialize, Serialize};
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use windows::Win32::System::DataExchange::*;
use windows::Win32::System::Environment::ExpandEnvironmentStringsW;
use windows::Win32::System::Memory::*;
use windows::Win32::System::Ole::CF_UNICODETEXT;
use windows::core::HSTRING;

const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const CREATE_NEW_CONSOLE: u32 = 0x0000_0010;

const FILES: &[(&str, &[u8])] = &[
    ("yad.py", include_bytes!("yad.py")),
    ("ymd_fast.py", include_bytes!("ymd_fast.py")),
    ("build_lib.py", include_bytes!("build_lib.py")),
    ("requirements.txt", include_bytes!("requirements.txt")),
    ("icon.png", include_bytes!("icon.png")),
];

const HOSTS: &[&str] = &["music.yandex.ru", "music.yandex.com", "music.yandex.by", "music.yandex.kz", "music.yandex.uz"];

#[derive(Serialize, Deserialize, Clone)]
#[serde(default)]
pub struct YadConfig {
    pub keyword: String,
    /// Environment variables are allowed: %USERPROFILE%\Music\YandexMusic.
    pub download_dir: String,
    /// "FLAC", "AAC 192 kbps" or "AAC 64 kbps".
    pub quality: String,
    /// "none", "text" or "lrc".
    pub lyrics_format: String,
    pub skip_existing: bool,
    pub open_folder_after: bool,
    /// A playlist goes to <download_dir>\<playlist title>\Artist - Title.
    pub playlist_folder: bool,
    /// Artist downloads: skip podcasts and audiobooks.
    pub only_music: bool,
    /// Artist downloads: only the artist's own albums.
    pub stick_to_artist: bool,
    /// Tracks downloaded at once (1–8).
    pub parallel_downloads: u32,
}

impl Default for YadConfig {
    fn default() -> Self {
        Self {
            keyword: "yad".into(),
            download_dir: "%USERPROFILE%\\Music\\YandexMusic".into(),
            quality: "FLAC".into(),
            lyrics_format: "none".into(),
            skip_existing: true,
            open_folder_after: false,
            playlist_folder: true,
            only_music: false,
            stick_to_artist: false,
            parallel_downloads: 3,
        }
    }
}

#[derive(Clone)]
pub enum Op {
    Download(String),
    SaveToken(String),
    ResetToken,
    OpenFolder,
    InstallLib,
}

pub fn dir() -> PathBuf {
    crate::config::dir().join("yad")
}

pub fn icon() -> String {
    dir().join("icon.png").to_string_lossy().into()
}

fn flow_plugin_dir() -> PathBuf {
    PathBuf::from(std::env::var("APPDATA").unwrap_or_default()).join("FlowLauncher").join("Plugins").join("yad")
}

/// Unpacks the embedded scripts (when changed) and, once, takes the token and the
/// dependencies from the old Flow Launcher plugin so nothing has to be set up again.
pub fn install() {
    let dir = dir();
    let _ = std::fs::create_dir_all(&dir);
    for (name, bytes) in FILES {
        let path = dir.join(name);
        if std::fs::read(&path).ok().as_deref() != Some(*bytes) {
            let _ = std::fs::write(&path, bytes);
        }
    }
    let flow = flow_plugin_dir();
    let token = dir.join("token.json");
    if !token.exists() && flow.join("config.json").exists() {
        let _ = std::fs::copy(flow.join("config.json"), &token);
    }
    if !has_lib() && flow.join("lib").join("ymd").exists() {
        copy_dir(&flow.join("lib"), &dir.join("lib"));
    }
}

fn copy_dir(from: &Path, to: &Path) {
    let _ = std::fs::create_dir_all(to);
    let Ok(entries) = std::fs::read_dir(from) else { return };
    for e in entries.flatten() {
        let name = e.file_name();
        if name == "__pycache__" || name == "flowlauncher" {
            continue;
        }
        if e.path().is_dir() {
            copy_dir(&e.path(), &to.join(&name));
        } else {
            let _ = std::fs::copy(e.path(), to.join(&name));
        }
    }
}

fn has_lib() -> bool {
    dir().join("lib").join("ymd").exists()
}

fn has_token() -> bool {
    let Some(v) = std::fs::read_to_string(dir().join("token.json"))
        .ok()
        .and_then(|s| serde_json::from_str::<serde_json::Value>(s.trim_start_matches('\u{feff}')).ok())
    else {
        return false;
    };
    ["token_enc", "token"].iter().any(|k| v[k].as_str().is_some_and(|s| !s.is_empty()))
}

pub fn expand_env(s: &str) -> String {
    let src = HSTRING::from(s);
    let mut buf = vec![0u16; 1024];
    let n = unsafe { ExpandEnvironmentStringsW(&src, Some(&mut buf)) } as usize;
    if n == 0 || n > buf.len() {
        return s.to_string();
    }
    String::from_utf16_lossy(&buf[..n - 1])
}

pub fn clipboard_text() -> String {
    unsafe {
        if OpenClipboard(None).is_err() {
            return String::new();
        }
        let mut text = String::new();
        if let Ok(h) = GetClipboardData(CF_UNICODETEXT.0 as u32) {
            let hg = windows::Win32::Foundation::HGLOBAL(h.0);
            let p = GlobalLock(hg) as *const u16;
            if !p.is_null() {
                let len = (0..).take_while(|&i| *p.add(i) != 0).count();
                text = String::from_utf16_lossy(std::slice::from_raw_parts(p, len));
                let _ = GlobalUnlock(hg);
            }
        }
        let _ = CloseClipboard();
        text
    }
}

/// `https://music.yandex.ru/album/1/track/2/?utm=x` -> `https://music.yandex.ru/album/1/track/2`;
/// `/artist/5/albums` -> `/artist/5` (the only artist form ymd understands). None if not Yandex Music.
pub fn clean_url(s: &str) -> Option<String> {
    let s = s.trim();
    let (scheme, rest) = s.split_once("://")?;
    if !scheme.eq_ignore_ascii_case("http") && !scheme.eq_ignore_ascii_case("https") {
        return None;
    }
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let host = rest[..end].to_lowercase();
    if !HOSTS.contains(&host.as_str()) {
        return None;
    }
    let path = rest[end..].split(['?', '#']).next().unwrap_or_default().trim_end_matches('/');
    let mut path = path.to_string();
    if let Some(i) = path.find("/artist/") {
        let digits = path[i + 8..].chars().take_while(char::is_ascii_digit).count();
        if digits > 0 {
            path.truncate(i + 8 + digits);
        }
    }
    Some(format!("{}://{host}{path}", scheme.to_lowercase()))
}

/// What the link points to, in the accusative: "Скачать альбом", "Скачать артиста".
fn kind(url: &str) -> &'static str {
    const TYPES: &[(&str, &str)] =
        &[("/track/", "трек"), ("/album/", "альбом"), ("/playlists/", "плейлист"), ("/playlist/", "плейлист"), ("/artist/", "артиста")];
    TYPES.iter().find(|(part, _)| url.contains(part)).map_or("объект", |(_, name)| name)
}

fn item(title: impl Into<String>, sub: impl Into<String>, action: Action) -> Item {
    Item::new(title, sub, Some(icon()), 0, action)
}

pub fn search(cfg: &YadConfig, q: &str) -> Vec<Item> {
    let folder = expand_env(&cfg.download_dir);
    if !has_lib() {
        return vec![item(
            "Установить зависимости yad",
            "Нет папки lib: Enter запустит pip (~16 МБ) в отдельном окне",
            Action::Yad(Op::InstallLib),
        )];
    }
    if !has_token() {
        if q.starts_with("y0_") {
            let short: String = q.chars().take(6).collect();
            return vec![item(
                format!("Сохранить токен: {short}…"),
                "Enter — проверить и сохранить (хранится зашифрованным, DPAPI)",
                Action::Yad(Op::SaveToken(q.to_string())),
            )];
        }
        return vec![item("Введите токен Яндекс Музыки", "Вставьте токен, начинающийся с y0_…", Action::Nothing)];
    }
    if q == "reset token" || q == "сбросить токен" {
        return vec![item("Сбросить токен", "Enter — удалить сохранённый токен", Action::Yad(Op::ResetToken))];
    }
    let download = |url: String, title: String| {
        let sub = format!("{url}  →  {folder}");
        item(title, sub, Action::Yad(Op::Download(url)))
    };
    let open_folder = item("Открыть папку загрузок", folder.clone(), Action::Yad(Op::OpenFolder));
    if q.is_empty() {
        let first = match clean_url(&clipboard_text()) {
            Some(url) => {
                let title = format!("Скачать {} из буфера обмена", kind(&url));
                download(url, title)
            }
            None => item("Вставьте ссылку на трек, альбом, плейлист или артиста", "Пример: yad https://music.yandex.ru/album/123", Action::Nothing),
        };
        return vec![first, open_folder];
    }
    match clean_url(q) {
        Some(url) => {
            let title = format!("Скачать {}", kind(&url));
            vec![download(url, title), open_folder]
        }
        None => vec![item("Некорректная ссылка", "Ожидается ссылка на music.yandex.ru", Action::Nothing)],
    }
}

/// Runs an action; downloads and token checks happen in a detached hidden Python process.
pub fn run(op: &Op, cfg: &YadConfig, python: &str) {
    let dir = dir();
    let worker = |args: &[&str]| {
        let mut cmd = Command::new(python);
        cmd.arg("-B")
            .arg(dir.join("yad.py"))
            .args(args)
            .current_dir(&dir)
            .env("PYTHONIOENCODING", "utf-8")
            .creation_flags(CREATE_NO_WINDOW)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        cmd
    };
    match op {
        Op::Download(url) => {
            let _ = worker(&["download", url]).spawn();
        }
        // The token goes through the environment so it never shows up in process lists.
        Op::SaveToken(token) => {
            let _ = worker(&["save_token"]).env("YAD_TOKEN", token).spawn();
        }
        Op::ResetToken => {
            let _ = worker(&["reset_token"]).spawn();
        }
        Op::OpenFolder => {
            let folder = expand_env(&cfg.download_dir);
            let _ = std::fs::create_dir_all(&folder);
            let _ = Command::new("explorer.exe").arg(folder).spawn();
        }
        Op::InstallLib => {
            // pip output is worth seeing: console python.exe next to pythonw.exe, in its own window.
            let console = python.to_lowercase().replace("pythonw.exe", "python.exe");
            let exe = if Path::new(&console).exists() { console } else { "python".into() };
            let _ = Command::new(exe).arg(dir.join("build_lib.py")).current_dir(&dir).creation_flags(CREATE_NEW_CONSOLE).spawn();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn urls() {
        assert_eq!(
            clean_url("https://music.yandex.ru/album/1/track/2/?utm_source=x").as_deref(),
            Some("https://music.yandex.ru/album/1/track/2")
        );
        assert_eq!(clean_url("https://music.yandex.com/artist/55/albums").as_deref(), Some("https://music.yandex.com/artist/55"));
        assert_eq!(clean_url("https://youtube.com/watch?v=1"), None);
        assert_eq!(clean_url("music.yandex.ru/album/1"), None);
        assert_eq!(kind("https://music.yandex.ru/users/me/playlists/3"), "плейлист");
    }
}
