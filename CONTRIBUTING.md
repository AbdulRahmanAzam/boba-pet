# Contributing to Boba Pet

Thanks for helping! Boba is a plain Python + Tk app, so there is no build step.

## Set up

```powershell
git clone https://github.com/AbdulRahmanAzam/boba-pet
cd boba-pet
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
.venv\Scripts\pythonw.exe claude_pet.py
```

Close any installed copy of the pet first, so two pets don't fight over the
same settings file.

## Guidelines

- **Never commit secrets.** API keys belong in `.env` (git-ignored). Use
  `.env.example` for placeholders. Don't commit screenshots (`shots/`) or
  models (`models/`).
- Keep the pet light: standard library first. New packages go in both
  `pyproject.toml` and `requirements.txt`, and should be imported lazily with
  a fallback, so the pet still starts without them.
- Match the surrounding code: type hints, short docstrings that say why, and
  `snake_case`. Run `python -m py_compile *.py` before you push.
- Privacy matters: the pet must not send screen content, audio or transcript
  text anywhere except the AI provider the user linked, and only when they ask.
- Risky computer actions must stay behind the approval prompt in `computer.py`.
- Test with a throwaway home folder (set `USERPROFILE`/`HOME` to a temp dir)
  so you don't touch your real settings.

## Pull requests

1. Fork, then branch from `main`.
2. Keep each PR to one change and describe how you tested it (a GIF of the pet
   helps for UI changes).
3. CI checks that every file compiles and that the package builds.

## Ideas

New characters (`sprite_<name>.py`), more AI providers in `providers.py`,
macOS/Linux support, translations, and better news feeds are all welcome.
