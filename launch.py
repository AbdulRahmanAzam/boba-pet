"""Entry point for the installed `boba-pet` command and `python -m boba_pet`.

The pet's modules import each other by plain name (import chatlog), as
they do when run from a source checkout, so this puts their folder on
sys.path first.
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

USAGE = """usage: boba-pet [--setup] [--autostart] [--uninstall-shortcuts] [--version]

  (no option)            start the pet
  --setup                download the offline voice/search models and add a Start menu shortcut
  --autostart            same as --setup, and start the pet with Windows
  --uninstall-shortcuts  remove the Start menu and startup shortcuts
"""


def main() -> int:
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(USAGE)
        return 0
    if "--version" in args:
        from importlib.metadata import PackageNotFoundError, version

        try:
            print(version("boba-pet"))
        except PackageNotFoundError:
            print("source checkout")
        return 0
    if "--uninstall-shortcuts" in args:
        import pet_setup

        pet_setup.remove_shortcuts()
        return 0
    if "--setup" in args or "--autostart" in args:
        import pet_setup

        return pet_setup.run(autostart="--autostart" in args)

    import pet_setup

    pet_setup.download_in_background()  # first start after a plain pip install
    os.chdir(Path.home())
    import claude_pet

    claude_pet.ClaudePet().run()
    return 0


def setup() -> int:
    """Console command boba-pet-setup: same as boba-pet --setup, with output.
    Pass --autostart to also start the pet with Windows."""
    if "--autostart" not in sys.argv and "--uninstall-shortcuts" not in sys.argv:
        sys.argv.append("--setup")
    return main()


if __name__ == "__main__":
    sys.exit(main())
