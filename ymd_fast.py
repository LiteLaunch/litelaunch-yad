"""
Ускоренный запуск ymd в текущем процессе.

ymd (yandex-music-downloader) сам по себе качает треки строго по одному и на каждый
запрос открывает новое TLS-соединение. Здесь его CLI запускается как есть (разбор
ссылки, обход альбома/артиста/плейлиста, пропуск существующих, теги — всё его), а
подменяются три вещи:

  * HTTP: все запросы yandex_music идут через одну requests.Session (keep-alive);
  * core.download_track: трек ставится в пул из N потоков, а цикл ymd идёт дальше.
    Семафор держит цикл не более чем на N треков впереди, чтобы подписанные ссылки
    на файлы не успевали протухнуть на длинных дискографиях;
  * сама загрузка трека: обложка — один раз на альбом (в ymd она качается заново для
    каждого трека и только потом берётся из кэша), файл пишется на диск потоком с
    расшифровкой на лету, а не держится в памяти целиком дважды.
"""
import hashlib
import io
import sys
import random
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout

import requests
from Crypto.Cipher import AES
from requests.adapters import HTTPAdapter

CHUNK = 256 * 1024
TRIES = 5


def _share_session(session: requests.Session):
    """yandex_music вызывает requests.request(); направляем его в общую сессию."""
    from yandex_music.utils import request as ym_request
    ym_request.requests = types.SimpleNamespace(
        request=session.request,
        Timeout=requests.Timeout,
        RequestException=requests.RequestException,
    )


def _stream_to_file(session: requests.Session, urls: list, key, path, timeout: int):
    """Качает файл в path, расшифровывая AES-CTR на лету. Повторяет при сетевых ошибках."""
    for attempt in range(TRIES):
        aes = AES.new(key=bytes.fromhex(key), nonce=bytes(12), mode=AES.MODE_CTR) if key else None
        try:
            with session.get(random.choice(urls), stream=True, timeout=timeout) as resp:
                resp.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in resp.iter_content(CHUNK):
                        f.write(aes.decrypt(chunk) if aes else chunk)
            return
        except requests.RequestException:
            if attempt == TRIES - 1:
                raise
            time.sleep(2 * (attempt + 1))


class _Parallel:
    def __init__(self, core, session: requests.Session, workers: int, timeout: int):
        self.core = core
        self.session = session
        self.timeout = timeout
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.slots = threading.Semaphore(workers)
        self.futures = []
        self.covers = {}
        self.cover_locks = {}
        self.guard = threading.Lock()

    def submit(self, **kwargs):
        """Подменяет core.download_track: возвращается сразу, трек качается в пуле."""
        self.slots.acquire()
        future = self.pool.submit(self._download, **kwargs)
        future.add_done_callback(lambda _: self.slots.release())
        self.futures.append((kwargs["track_info"].path, future))

    def wait(self) -> list:
        """Ждёт все треки; возвращает [(путь, ошибка)] для неудавшихся."""
        self.pool.shutdown(wait=True)
        return [(path, f.exception()) for path, f in self.futures if f.exception()]

    def _cover(self, track, resolution):
        album = track.albums[0] if track.albums else None
        if track.cover_uri is None or not (album and album.id):
            return None
        with self.guard:
            lock = self.cover_locks.setdefault(album.id, threading.Lock())
        with lock:
            if album.id not in self.covers:
                size = "orig" if resolution == -1 else f"{resolution}x{resolution}"
                data = track.download_cover_bytes(size=size)
                mime = self.core.guess_mime_type(data)
                if mime is None:
                    raise RuntimeError("Unknown cover mime type")
                self.covers[album.id] = self.core.AlbumCover(data=data, mime_type=mime)
            return self.covers[album.id]

    def _download(self, track_info, cover_resolution, lyrics_format, embed_cover,
                  covers_cache=None, compatibility_level=1):
        core = self.core
        target = track_info.path
        track = track_info.track

        text_lyrics = None
        if lyrics_format != core.LyricsFormat.NONE and (info := track.lyrics_info):
            if lyrics_format == core.LyricsFormat.LRC and info.has_available_sync_lyrics:
                lrc_path = target.with_suffix(".lrc")
                if not lrc_path.is_file() and (lyrics := track.get_lyrics(format_="LRC")):
                    core.write_via_temporary_file(lyrics.fetch_lyrics().encode("utf-8"), lrc_path)
            elif info.has_available_text_lyrics:
                if lyrics := track.get_lyrics(format_="TEXT"):
                    text_lyrics = lyrics.fetch_lyrics()

        # yad всегда встраивает обложку; без встраивания ymd кладёт cover.jpg — этот случай не нужен
        cover = self._cover(track, cover_resolution) if embed_cover else None

        info = track_info.download_info
        name = hashlib.sha256(target.name.encode()).hexdigest()
        tmp = target.parent / core.TEMPORARY_FILE_NAME_TEMPLATE.format(name)
        try:
            _stream_to_file(self.session, info.urls, info.decryption_key, tmp, self.timeout)
            core.set_tags(tmp, track, info.file_format.container, text_lyrics, cover, compatibility_level)
            tmp.replace(target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


def run(argv: list, token: str, workers: int = 3, timeout: int = 20) -> tuple:
    """
    Запускает ymd с аргументами argv (без --token).
    Возвращает (код выхода, вывод ymd, [(путь, ошибка)] неудавшихся треков).
    """
    from ymd import cli, core

    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=workers + 4)
    session.mount("https://", adapter)
    _share_session(session)

    parallel = _Parallel(core, session, max(1, workers), timeout)
    original = core.download_track
    core.download_track = parallel.submit

    out = io.StringIO()
    old_argv = sys.argv
    sys.argv = ["ymd", *argv, "--token", token]
    failures = []
    try:
        with redirect_stdout(out), redirect_stderr(out):
            try:
                code = cli.main() or 0
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
            except Exception as e:
                print(f"{type(e).__name__}: {e}")
                code = 1
            finally:
                failures = parallel.wait()
    finally:
        sys.argv = old_argv
        core.download_track = original
        session.close()
    return code, out.getvalue(), failures
