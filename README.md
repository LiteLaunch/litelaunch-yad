# litelaunch-yad

Yandex Music downloader, a built-in plugin of [LiteLaunch](https://github.com/LiteLaunch/litelaunch)
(`yad <link>`). Lives in the launcher as the submodule `plugins/yad`.

- `yad.rs`: the native part, compiled into the launcher. It checks the link, takes one from the
  clipboard, knows whether the token is saved, and starts the Python worker. It embeds the files below and
  unpacks them to `%APPDATA%\LiteLaunch\yad`.
- `yad.py`: the worker, run only for a download or for token work. It reports through LiteLaunch's tray icon.
- `ymd_fast.py`: runs the ymd CLI in-process with one keep-alive session, parallel tracks
  (`parallel_downloads`, default 3), a cover fetched once per album and decryption streamed to disk.
  11-track album: 53 s → 19 s, byte-identical files.
- `build_lib.py` and `requirements.txt`: install the dependencies into `lib` with pip.
- `icon.png` (results) and `yad.ico` (notifications, resource 2 in the launcher's `app.rc`).

Predecessor: the Flow Launcher plugin, [yad-flow-plugin](https://github.com/Slava-Gusak/yad-flow-plugin).
