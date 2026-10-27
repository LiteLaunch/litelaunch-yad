"""
yad — загрузчик Яндекс Музыки для LiteLaunch.

Поиск (разбор ссылки, буфер обмена, наличие токена) делает сам LiteLaunch.
Этот скрипт запускается только по Enter и делает долгую работу:

    pythonw yad.py download <url>
    pythonw yad.py save_token        (токен в переменной окружения YAD_TOKEN)
    pythonw yad.py reset_token
    python  yad.py doctor            (проверка: lib, токен, настройки — без сети)

Настройки — секция "yad" в %APPDATA%\\LiteLaunch\\config.json.
Токен — token.json рядом со скриптом, зашифрован DPAPI ключом пользователя Windows.
"""
import sys

# Кодировки фиксируем ДО любых других импортов
if sys.stdout:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr:
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import os
YAD_DIR = os.path.dirname(os.path.abspath(__file__))
LIB_PATH = os.path.join(YAD_DIR, "lib")
sys.path.insert(0, LIB_PATH)

import re
import json
import base64
import ctypes
import hashlib
import msvcrt
import winreg
import tempfile
import subprocess
from ctypes import wintypes
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape, quoteattr
from urllib.parse import urlparse, urlunparse

DEFAULT_DOWNLOAD_DIR = os.path.join("%USERPROFILE%", "Music", "YandexMusic")
QUALITY_OPTIONS      = {"FLAC": "2", "AAC 192 kbps": "1", "AAC 64 kbps": "0"}
LYRICS_OPTIONS       = ("none", "text", "lrc")
COVER_RESOLUTION     = "original"
TOKEN_PATH           = os.path.join(YAD_DIR, "token.json")
LAUNCHER_CONFIG      = os.path.join(os.environ.get("APPDATA", ""), "LiteLaunch", "config.json")
ICON_PATH            = os.path.join(YAD_DIR, "icon.png")

# Свой AppUserModelID: уведомления подписаны «Yandex Music Downloader» с нашей иконкой
TOAST_APP_ID   = "LiteLaunch.Yad"
TOAST_APP_NAME = "Yandex Music Downloader"

# /users/<логин>/playlists/<номер> — старый формат, ymd ждёт /playlists/<uuid>
OLD_PLAYLIST_RE = re.compile(r"/users/([^/]+)/playlists/(\d+)$")
NEW_PLAYLIST_RE = re.compile(r"/playlists/([^/]+)$")

# Разбор вывода ymd для итоговой статистики
YMD_PROGRESS_RE    = re.compile(r"^\[(\d+)/(\d+)\]")
YMD_DOWNLOADING_RE = re.compile(r"\] Загружается ")
YMD_UNAVAILABLE_RE = re.compile(r"Трек .* не доступен для скачивания")

TYPES = {"/track/": "трек", "/album/": "альбом", "/playlists/": "плейлист", "/playlist/": "плейлист", "/artist/": "артист"}


# === DPAPI ===

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
crypt32  = ctypes.WinDLL("crypt32", use_last_error=True)
kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(func, data: bytes) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not func(ctypes.byref(blob_in), None, None, None, None,
                CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def encrypt_token(token: str) -> str:
    return base64.b64encode(_dpapi(crypt32.CryptProtectData, token.encode("utf-8"))).decode("ascii")


def decrypt_token(data: str) -> str:
    return _dpapi(crypt32.CryptUnprotectData, base64.b64decode(data)).decode("utf-8")


# === Токен и настройки ===

def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_token_file(data: dict):
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_token() -> str:
    data = _read_json(TOKEN_PATH)
    # Токен открытым текстом (старая версия плагина) → шифруем
    if data.get("token"):
        token = data.pop("token")
        data["token_enc"] = encrypt_token(token)
        save_token_file(data)
        return token
    if data.get("token_enc"):
        try:
            return decrypt_token(data["token_enc"])
        except Exception:
            # Файл с другого компьютера/пользователя — расшифровать нельзя
            return ""
    return ""


def check_token(token: str) -> str:
    """'ok' | 'invalid' | 'unknown' (нет сети и т.п.)."""
    try:
        from yandex_music import Client
        from yandex_music.exceptions import UnauthorizedError
    except Exception:
        return "unknown"
    try:
        Client(token).init()
        return "ok"
    except UnauthorizedError:
        return "invalid"
    except Exception:
        return "unknown"


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return default


class Settings:
    def __init__(self):
        raw = _read_json(LAUNCHER_CONFIG).get("yad") or {}
        download_dir = (raw.get("download_dir") or "").strip() or DEFAULT_DOWNLOAD_DIR
        self.download_dir = os.path.normpath(os.path.expandvars(os.path.expanduser(download_dir)))
        self.quality = QUALITY_OPTIONS.get(raw.get("quality"), "2")
        lyrics = raw.get("lyrics_format")
        self.lyrics_format = lyrics if lyrics in LYRICS_OPTIONS else "none"
        self.skip_existing = _as_bool(raw.get("skip_existing"), True)
        self.open_folder_after = _as_bool(raw.get("open_folder_after"), False)
        self.playlist_folder = _as_bool(raw.get("playlist_folder"), True)
        self.only_music = _as_bool(raw.get("only_music"), False)
        self.stick_to_artist = _as_bool(raw.get("stick_to_artist"), False)
        try:
            self.parallel_downloads = max(1, min(8, int(raw.get("parallel_downloads", 3))))
        except (TypeError, ValueError):
            self.parallel_downloads = 3


def detect_type(url: str) -> str:
    return next((name for part, name in TYPES.items() if part in url), "объект")


def is_album_url(url: str) -> bool:
    return "/album/" in url and "/track/" not in url


def resolve_playlist(url: str, token: str, need_title: bool) -> tuple:
    """
    Возвращает (url, название плейлиста или None).
    Старый формат /users/<логин>/playlists/<номер> переводится в /playlists/<uuid>,
    который понимает ymd. Название запрашивается, только если оно нужно (need_title).
    """
    parsed = urlparse(url)
    old = OLD_PLAYLIST_RE.search(parsed.path)
    new = None if old else NEW_PLAYLIST_RE.search(parsed.path)
    if not old and not (need_title and new):
        return url, None

    from yandex_music import Client
    client = Client(token)

    if old:
        login, kind = old.groups()
        playlist = client.users_playlists(int(kind), login)
        if playlist is None or not playlist.playlist_uuid:
            raise RuntimeError(f"{login}/{kind}")
        url = urlunparse((parsed.scheme, parsed.netloc, f"/playlists/{playlist.playlist_uuid}", '', '', ''))
    else:
        playlist = client.playlist(new.group(1))
        if playlist is None:
            raise RuntimeError(new.group(1))
    return url, playlist.title


def safe_folder_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:100] or "playlist"


def acquire_lock(url: str):
    """
    Блокировка от двойного запуска одной и той же загрузки.
    Возвращает открытый файл (держать до конца загрузки) или None, если ссылка уже качается.
    Windows снимает блокировку сама, даже если процесс упадёт.
    """
    name = "yad-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".lock"
    f = open(os.path.join(tempfile.gettempdir(), name), "a+")
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        return f
    except OSError:
        f.close()
        return None


# === Уведомления ===

def register_app_id():
    """Неупакованному приложению для тостов достаточно ключа AppUserModelId в HKCU."""
    key_path = rf"Software\Classes\AppUserModelId\{TOAST_APP_ID}"
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, TOAST_APP_NAME)
            winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, ICON_PATH)
    except OSError:
        pass


# Нативное уведомление Windows; если WinRT недоступен — старый balloon tip.
# Все данные передаются через переменные окружения, чтобы ничего не экранировать в скрипте.
NOTIFY_PS_SCRIPT = r"""
try {
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
    $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
    $doc.LoadXml($env:YAD_TOAST_XML)
    $toast = [Windows.UI.Notifications.ToastNotification]::new($doc)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:YAD_TOAST_APP).Show($toast)
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    $notify = New-Object System.Windows.Forms.NotifyIcon
    $notify.Icon = [System.Drawing.SystemIcons]::Information
    $notify.Visible = $true
    $notify.ShowBalloonTip(4000, $env:YAD_TITLE, $env:YAD_MESSAGE, [System.Windows.Forms.ToolTipIcon]::Info)
    Start-Sleep -Seconds 5
    $notify.Dispose()
}
"""


class COPYDATASTRUCT(ctypes.Structure):
    _fields_ = [("dwData", ctypes.c_size_t), ("cbData", wintypes.DWORD), ("lpData", ctypes.c_void_p)]


user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_void_p,
                                       wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
WM_COPYDATA = 0x004A
CMD_NOTIFY = 3  # main.rs


def notify_launcher(title: str, message: str, folder: str = None) -> bool:
    """Уведомление через значок LiteLaunch в трее: без запуска PowerShell. False — лаунчер не запущен."""
    hwnd = user32.FindWindowW("LiteLaunchWindow", None)
    if not hwnd:
        return False
    payload = json.dumps({"title": title, "text": message, "folder": folder or "", "icon": "yad"})
    buf = ctypes.create_unicode_buffer(payload)
    # Длина в байтах UTF-16: эмодзи вроде 🔄 занимают две единицы
    cds = COPYDATASTRUCT(CMD_NOTIFY, len(payload.encode("utf-16-le")), ctypes.cast(buf, ctypes.c_void_p))
    result = ctypes.c_size_t()
    sent = user32.SendMessageTimeoutW(hwnd, WM_COPYDATA, 0, ctypes.addressof(cds), 0x2, 3000, ctypes.byref(result))
    return bool(sent and result.value)


def notify(title: str, message: str, folder: str = None):
    if notify_launcher(title, message, folder):
        return
    register_app_id()
    actions = ""
    if folder:
        actions = (
            "<actions><action activationType=\"protocol\" content=\"Открыть папку\" "
            f"arguments={quoteattr(Path(folder).as_uri())}/></actions>"
        )
    toast_xml = (
        "<toast><visual><binding template=\"ToastGeneric\">"
        f"<text>{xml_escape(title)}</text><text>{xml_escape(message)}</text>"
        f"<image placement=\"appLogoOverride\" src={quoteattr(Path(ICON_PATH).as_uri())}/>"
        f"</binding></visual>{actions}</toast>"
    )
    subprocess.Popen(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", NOTIFY_PS_SCRIPT],
        creationflags=subprocess.CREATE_NO_WINDOW,
        env={
            **os.environ,
            "YAD_TOAST_XML": toast_xml,
            "YAD_TOAST_APP": TOAST_APP_ID,
            "YAD_TITLE": title,
            "YAD_MESSAGE": message,
        },
    )


# === Загрузка ===

def download_stats(stdout: str) -> tuple:
    """(скачано, недоступно, всего или None) по выводу ymd."""
    done = unavailable = 0
    total = None
    for line in stdout.splitlines():
        if match := YMD_PROGRESS_RE.match(line):
            total = int(match.group(2))
        if YMD_DOWNLOADING_RE.search(line):
            done += 1
        elif YMD_UNAVAILABLE_RE.search(line):
            unavailable += 1
    return done, unavailable, total


def format_stats(stdout: str, is_artist: bool, failed: int = 0) -> str:
    started, unavailable, total = download_stats(stdout)
    done = started - failed
    if started == 0 and unavailable == 0:
        return "Всё уже скачано, новых треков нет"
    parts = [f"Скачано: {done}"]
    # Для артиста ymd знает общее число треков лишь приблизительно — пропуски не считаем
    if total and not is_artist:
        skipped = total - started - unavailable
        if skipped > 0:
            parts.append(f"пропущено: {skipped}")
    if unavailable:
        parts.append(f"недоступно: {unavailable}")
    if failed:
        parts.append(f"ошибок: {failed}")
    return ", ".join(parts)


def last_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[-1] if lines else ""


def run_download(url: str, media_type: str, token: str, settings: Settings, playlist_title: str = None):
    if playlist_title:
        # Плейлист целиком в своей папке, без раскладки по артистам/альбомам
        target_dir = os.path.join(settings.download_dir, safe_folder_name(playlist_title))
        path_pattern = "#track-artist - #title"
    else:
        target_dir = settings.download_dir
        path_pattern = "#album-artist/#album/#number - #title" if is_album_url(url) else "#album-artist/#album/#title"

    cmd = [
        "--quality", settings.quality,
        "--cover-resolution", COVER_RESOLUTION,
        "--path-pattern", path_pattern,
        "--embed-cover",
        "--lyrics-format", settings.lyrics_format,
        "--dir", target_dir,
        "-u", url,
    ]
    if settings.skip_existing:
        cmd.append("--skip-existing")
    if settings.only_music:
        cmd.append("--only-music")
    if settings.stick_to_artist:
        cmd.append("--stick-to-artist")

    try:
        import ymd_fast
        code, output, failures = ymd_fast.run(cmd, token, settings.parallel_downloads)
    except Exception as e:
        notify("❌ Yandex Music", f"Ошибка запуска: {str(e)[:200]}")
        return

    if code != 0:
        # ymd пишет ошибки через print()
        notify("❌ Yandex Music", f"Ошибка: {(last_line(output) or 'Unknown error')[:200]}")
        return
    stats = format_stats(output, "/artist/" in url, failed=len(failures))
    if failures:
        path, error = failures[0]
        notify(f"⚠️ {media_type.capitalize()}: скачано не всё", f"{stats}. Первая ошибка: {Path(path).name}: {str(error)[:120]}",
               folder=target_dir)
    else:
        notify(f"✅ {media_type.capitalize()} скачан успешно!", stats, folder=target_dir)
    if settings.open_folder_after:
        os.startfile(target_dir)


# === Команды ===

def cmd_download(url: str):
    token = load_token()
    if not token:
        notify("❌ Yandex Music", "Токен не задан! Напишите yad y0_...")
        return

    lock = acquire_lock(url)
    if lock is None:
        notify("⏳ Yandex Music", "Эта ссылка уже скачивается")
        return

    try:
        settings = Settings()
        media_type = detect_type(url)
        os.makedirs(settings.download_dir, exist_ok=True)
        notify("⏳ Yandex Music", f"Скачивание: {media_type}...")
        try:
            url, playlist_title = resolve_playlist(url, token, settings.playlist_folder)
        except Exception as e:
            notify("❌ Yandex Music", f"Не удалось найти плейлист: {str(e)[:200]}")
            return
        run_download(url, media_type, token, settings, playlist_title)
    finally:
        lock.close()
        try:
            os.remove(lock.name)
        except OSError:
            pass


def cmd_save_token():
    token = os.environ.pop("YAD_TOKEN", "").strip()
    if not token:
        return
    status = check_token(token)
    if status == "invalid":
        notify("❌ Yandex Music", "Токен неверный — Яндекс его не принял. Проверьте и введите заново.")
        return
    save_token_file({"token_enc": encrypt_token(token)})
    if status == "ok":
        notify("✅ Yandex Music", "Токен сохранён! Теперь вставляйте ссылки.")
    else:
        notify("⚠️ Yandex Music", "Токен сохранён, но проверить его не удалось (нет связи с Яндексом).")


def cmd_reset_token():
    save_token_file({})
    notify("🔄 Yandex Music", "Токен сброшен. Введите новый.")


def cmd_doctor():
    """Проверка без сети и без изменений."""
    try:
        import ymd  # noqa: F401
        import yandex_music  # noqa: F401
        print("lib: ok")
    except Exception as e:
        print(f"lib: ОШИБКА {e}")
    print("token:", "ok" if load_token() else "нет или не расшифровывается")
    s = Settings()
    print(f"settings: dir={s.download_dir} quality={s.quality} lyrics={s.lyrics_format}")


COMMANDS = {
    "download": lambda args: cmd_download(args[0]),
    "save_token": lambda args: cmd_save_token(),
    "reset_token": lambda args: cmd_reset_token(),
    "doctor": lambda args: cmd_doctor(),
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(2)
    COMMANDS[sys.argv[1]](sys.argv[2:])
