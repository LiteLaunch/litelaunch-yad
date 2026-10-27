"""
Пересобирает папку lib из requirements.txt и удаляет из неё то, что yad не нужно.

Запуск (тем Python, что указан в config.json LiteLaunch):
    python build_lib.py
"""
import os
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
LIB_PATH = os.path.join(PLUGIN_DIR, "lib")
REQUIREMENTS = os.path.join(PLUGIN_DIR, "requirements.txt")

# Пути внутри lib, которые yad не использует
JUNK = [
    "bin",                # .exe-запускалки, привязанные к конкретному Python
    "share",              # man-страницы mutagen
    "tests",              # тесты yandex_music, ставятся как пакет верхнего уровня
    "Crypto/SelfTest",    # самотесты pycryptodome
]


def main():
    if os.path.isdir(LIB_PATH):
        shutil.rmtree(LIB_PATH)

    subprocess.run(
        [sys.executable, "-m", "pip", "install",
         "--target", LIB_PATH,
         "--requirement", REQUIREMENTS,
         "--no-user", "--disable-pip-version-check"],
        check=True,
    )

    for rel in JUNK:
        path = os.path.join(LIB_PATH, *rel.split("/"))
        if os.path.isdir(path):
            shutil.rmtree(path)
            print(f"Удалено: lib/{rel}")

    print("Готово.")
    input("Enter — закрыть окно")


if __name__ == "__main__":
    main()
