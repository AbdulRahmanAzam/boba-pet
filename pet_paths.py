"""Where the pet keeps files it writes: the .env with API keys, downloaded
speech and search models, and screenshots.

Run from a source checkout (a folder with .git, .env or models/), the pet
keeps them next to the code, as before. Installed with pip, the code folder
is site-packages, so they go to ~/.boba-pet instead. BOBA_PET_HOME
overrides both.
"""

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent


def _data_dir() -> Path:
    custom = os.environ.get("BOBA_PET_HOME")
    if custom:
        return Path(custom).expanduser()
    if any((APP_DIR / name).exists() for name in (".git", ".env", "models")):
        return APP_DIR
    return Path.home() / ".boba-pet"


DATA_DIR = _data_dir()
MODELS_DIR = DATA_DIR / "models"
SHOTS_DIR = DATA_DIR / "shots"
ENV_FILE = DATA_DIR / ".env"
