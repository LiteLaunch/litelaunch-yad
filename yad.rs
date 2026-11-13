//! yad — Yandex Music downloader, a LiteLaunch plugin (DLL). Queries are answered here (URL
//! check, clipboard, token presence); Python (yad.py + ymd) runs only for the download itself.
//! The scripts are embedded and unpacked to the plugin's data folder, %APPDATA%\LiteLaunch\yad.

use litelaunch_plugin::util::{CREATE_NEW_CONSOLE, CREATE_NO_WINDOW, clipboard_text, expand_env};
use litelaunch_plugin::{Action, After, Host, Info, Item, Plugin, Reply, export_plugin};
use serde::{Deserialize, Serialize};
use std::os::windows::process::CommandExt;
use std::process::{Command, Stdio};

const FILES: &[(&str, &[u8])] = &[
    ("yad.py", include_bytes!("yad.py")),
    ("ymd_fast.py", include_bytes!("ymd_fast.py")),
    ("build_lib.py", include_bytes!("build_lib.py")),
    ("requirements.txt", include_bytes!("requirements.txt")),
    ("icon.png", include_bytes!("icon.png")),
    ("yad.ico", include_bytes!("yad.ico")),
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

#[derive(Serialize, Deserialize)]
pub enum Op {
    Download(String),
    SaveToken(String),
    ResetToken,
    OpenFolder,
    InstallLib,
}

#[derive(Default)]
pub struct Yad;

export_plugin!(Yad);

fn icon(host: &Host) -> String {
    host.data_dir.join("icon.png").to_string_lossy().into()
}

fn has_lib(host: &Host) -> bool {
    host.data_dir.join("lib").join("ymd").exists()
}

fn has_token(host: &Host) -> bool {
    let Some(v) = litelaunch_plugin::util::read_json(&host.data_dir.join("token.json")) else { return false };
    ["token_enc", "token"].iter().any(|k| v[k].as_str().is_some_and(|s| !s.is_empty()))
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

impl Plugin for Yad {
    type Settings = YadConfig;
    type Op = Op;

    fn info(&self, host: &Host) -> Info {
        Info {
            name: "Yandex Music Downloader".into(),
            description: "Скачивает треки, альбомы, плейлисты и артистов с Яндекс Музыки по ссылке".into(),
            version: env!("CARGO_PKG_VERSION").into(),
            icon: Some(icon(host)),
            ..Default::default()
        }
    }

    /// Writes the embedded scripts to the data folder (when changed).
    fn init(&self, host: &Host) {
        litelaunch_plugin::util::unpack(&host.data_dir, FILES);
    }

    fn query(&self, host: &Host, cfg: &YadConfig, q: &str) -> Reply {
        let item = |title: String, sub: String, action: Action| Item::new(title, sub, Some(icon(host)), 0, action);
        let folder = expand_env(&cfg.download_dir);
        if !host.python_ok {
            return vec![litelaunch_plugin::install_python_item(Some(icon(host)))].into();
        }
        if !has_lib(host) {
            return vec![item(
                "Установить зависимости yad".into(),
                "Нет папки lib: Enter запустит pip (~16 МБ) в отдельном окне".into(),
                Action::op(&Op::InstallLib),
            )]
            .into();
        }
        if !has_token(host) {
            if q.starts_with("y0_") {
                let short: String = q.chars().take(6).collect();
                return vec![item(
                    format!("Сохранить токен: {short}…"),
                    "Enter — проверить и сохранить (хранится зашифрованным, DPAPI)".into(),
                    Action::op(&Op::SaveToken(q.to_string())),
                )]
                .into();
            }
            return vec![item("Введите токен Яндекс Музыки".into(), "Вставьте токен, начинающийся с y0_…".into(), Action::Nothing)].into();
        }
        if q == "reset token" || q == "сбросить токен" {
            return vec![item("Сбросить токен".into(), "Enter — удалить сохранённый токен".into(), Action::op(&Op::ResetToken))].into();
        }
        let download = |url: String, title: String| {
            let sub = format!("{url}  →  {folder}");
            item(title, sub, Action::op(&Op::Download(url)))
        };
        let open_folder = item("Открыть папку загрузок".into(), folder.clone(), Action::op(&Op::OpenFolder));
        if q.is_empty() {
            let first = match clean_url(&clipboard_text()) {
                Some(url) => {
                    let title = format!("Скачать {} из буфера обмена", kind(&url));
                    download(url, title)
                }
                None => item(
                    "Вставьте ссылку на трек, альбом, плейлист или артиста".into(),
                    "Пример: yad https://music.yandex.ru/album/123".into(),
                    Action::Nothing,
                ),
            };
            return vec![first, open_folder].into();
        }
        match clean_url(q) {
            Some(url) => {
                let title = format!("Скачать {}", kind(&url));
                vec![download(url, title), open_folder].into()
            }
            None => vec![item("Некорректная ссылка".into(), "Ожидается ссылка на music.yandex.ru".into(), Action::Nothing)].into(),
        }
    }

    /// Downloads and token checks happen in a detached hidden Python process.
    fn run(&self, host: &Host, cfg: &mut YadConfig, op: Op) -> After {
        let dir = &host.data_dir;
        let worker = |args: &[&str]| {
            let mut cmd = Command::new(&host.python);
            cmd.arg("-B")
                .arg(dir.join("yad.py"))
                .args(args)
                .current_dir(dir)
                .env("PYTHONIOENCODING", "utf-8")
                .creation_flags(CREATE_NO_WINDOW)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            cmd
        };
        match op {
            Op::Download(url) => {
                let _ = worker(&["download", &url]).spawn();
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
                // pip output is worth seeing: console python.exe, in its own window.
                let python = litelaunch_plugin::util::console_python(&host.python);
                let _ = Command::new(python).arg(dir.join("build_lib.py")).current_dir(dir).creation_flags(CREATE_NEW_CONSOLE).spawn();
            }
        }
        After::Hide
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
