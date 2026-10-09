"""First-run setup for an installed pet: download the offline models and
add Start menu / startup shortcuts.

    boba-pet --setup        download models + Start menu shortcut
    boba-pet --autostart    also start the pet when Windows starts
    boba-pet --uninstall-shortcuts

Models (about 200 MB in all) go to pet_paths.MODELS_DIR:
  - Vosk small English, for the "Hey Boba" wake word
  - Whisper base (faster-whisper), for talking to the pet
  - potion-base-2M (model2vec), for finding your projects by meaning
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path

import pet_paths

VOSK_NAME = "vosk-model-small-en-us-0.15"
VOSK_URL = f"https://alphacephei.com/vosk/models/{VOSK_NAME}.zip"
SHORTCUT_NAME = "Boba Pet.lnk"
CREATE_NO_WINDOW = 0x08000000


def say(text: str) -> None:
    try:
        print(text, flush=True)
    except (OSError, ValueError):  # pythonw has no console
        pass


def get_vosk() -> None:
    target = pet_paths.MODELS_DIR / "vosk" / VOSK_NAME
    if target.is_dir():
        return
    say("Downloading wake word model (40 MB)...")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "vosk.zip"
        urllib.request.urlretrieve(VOSK_URL, archive)
        with zipfile.ZipFile(archive) as z:
            z.extractall(tmp)
        shutil.move(str(Path(tmp) / VOSK_NAME), str(target))


def get_whisper() -> None:
    import listener

    say("Downloading speech model (150 MB)...")
    listener.load_model()


def get_embedder() -> None:
    import projects

    if projects.EMBED_MODEL.is_dir():
        return
    say("Downloading project search model (8 MB)...")
    from model2vec import StaticModel

    StaticModel.from_pretrained(projects.EMBED_MODEL_ID).save_pretrained(
        str(projects.EMBED_MODEL))


def models_missing() -> bool:
    m = pet_paths.MODELS_DIR
    return not ((m / "vosk" / VOSK_NAME).is_dir() and (m / "whisper").is_dir()
                and (m / "potion-base-2M").is_dir())


def download_models() -> list[str]:
    """Fetch every missing model; returns the problems (empty when all good)."""
    problems = []
    for step in (get_vosk, get_whisper, get_embedder):
        try:
            step()
        except Exception as e:  # noqa: BLE001 - keep going, the pet works without any one model
            problems.append(f"{step.__name__}: {e}")
    return problems


def download_in_background() -> None:
    if models_missing():
        threading.Thread(target=download_models, daemon=True).start()


def launcher() -> Path:
    """The installed GUI command (boba-pet.exe), or pythonw + this module."""
    exe = shutil.which("boba-pet")
    if exe:
        return Path(exe)
    return Path(sys.executable).with_name("pythonw.exe")


def _shortcut(link: Path) -> None:
    target = launcher()
    args = "" if target.stem.lower() == "boba-pet" else f'"{pet_paths.APP_DIR / "launch.py"}"'
    link.parent.mkdir(parents=True, exist_ok=True)
    script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:BP_LINK);"
              "$s.TargetPath=$env:BP_TARGET;$s.Arguments=$env:BP_ARGS;"
              "$s.WorkingDirectory=$env:USERPROFILE;$s.Description='Boba Pet';$s.Save()")
    env = dict(os.environ, BP_LINK=str(link), BP_TARGET=str(target), BP_ARGS=args)
    subprocess.run(["powershell", "-NoProfile", "-Command", script], env=env,
                   check=True, creationflags=CREATE_NO_WINDOW)


def _start_menu() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def add_shortcuts(autostart: bool) -> None:
    _shortcut(_start_menu() / SHORTCUT_NAME)
    say("Added Boba Pet to the Start menu.")
    if autostart:
        _shortcut(_start_menu() / "Startup" / SHORTCUT_NAME)
        say("Boba Pet will start with Windows.")


def remove_shortcuts() -> None:
    for link in (_start_menu() / SHORTCUT_NAME, _start_menu() / "Startup" / SHORTCUT_NAME):
        link.unlink(missing_ok=True)
    say("Removed Boba Pet shortcuts.")


def run(autostart: bool = False) -> int:
    say(f"Boba Pet data folder: {pet_paths.DATA_DIR}")
    problems = download_models()
    for p in problems:
        say(f"  skipped {p}")
    try:
        add_shortcuts(autostart)
    except (OSError, subprocess.CalledProcessError, KeyError) as e:
        say(f"  couldn't add shortcuts: {e}")
    say("Setup done. Start the pet from the Start menu or run: boba-pet")
    return 0
