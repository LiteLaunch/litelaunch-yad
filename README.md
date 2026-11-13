# litelaunch-yad

Yandex Music downloader, a plugin (DLL) of [LiteLaunch](https://github.com/LiteLaunch/litelaunch)
(`yad <link>`). Lives in the launcher as the submodule `plugins/yad`.

- `yad.rs`: the native part, built into `yad.dll`. It checks the link, takes one from the
  clipboard, knows whether the token is saved, and starts the Python worker. It embeds the files below and
  unpacks them to `%APPDATA%\LiteLaunch\yad`.
- `yad.py`: the worker, run only for a download or for token work. It reports through LiteLaunch's tray icon.
- `ymd_fast.py`: runs the ymd CLI in-process with one keep-alive session, parallel tracks
  (`parallel_downloads`, default 3), a cover fetched once per album and decryption streamed to disk.
  11-track album: 53 s → 19 s, byte-identical files.
- `build_lib.py` and `requirements.txt`: install the dependencies into `lib` with pip.
- `icon.png` (results) and `yad.ico` (notifications: the launcher takes it from the data folder).

Predecessor: the Flow Launcher plugin, [yad-flow-plugin](https://github.com/Slava-Gusak/yad-flow-plugin).

## Build

A `cdylib` against the launcher's SDK (`../../sdk`), so it is built inside the launcher's checkout:
`powershell -File scripts\build-plugins.ps1` there gives `target\plugins\out\<id>\<id>.dll`; the launcher loads it
from `%APPDATA%\LiteLaunch\plugins\<id>\`.
