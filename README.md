# Boba Pet

An always-on-top pixel-art desktop pet for Windows that talks to **Claude Code**
(and Codex, Antigravity, Gemini or Groq). Click it, type or say "Hey Boba", and
the answer pops up in a speech bubble. It watches your Claude Code sessions,
reacts when they finish or fail, and keeps you on track with tasks, focus
timers and a hackathon mode.

## Install (one line)

Open **PowerShell** and run:

```powershell
irm https://raw.githubusercontent.com/AbdulRahmanAzam/boba-pet/main/install.ps1 | iex
```

That's it. The installer:

1. installs [uv](https://docs.astral.sh/uv/) if you don't have it,
2. installs Boba Pet with its own private Python 3.12 (your Python is untouched),
3. downloads the offline voice and search models (about 200 MB, one time),
4. adds **Boba Pet** to the Start menu and starts the pet.

No admin rights, no git clone. To also start the pet with Windows, run
`$env:BOBA_PET_AUTOSTART="1"` before the line above, or later run
`boba-pet-setup --autostart`.

### Already have Python 3.11+?

```powershell
pip install boba-pet
boba-pet
```

or, with [uv](https://docs.astral.sh/uv/), just `uvx boba-pet`. The first
start downloads the voice models in the background; run `boba-pet-setup`
once to get them right away and add a Start menu shortcut.
### Update / uninstall

```powershell
uv tool upgrade boba-pet                       # update (or re-run the install line)
boba-pet-setup --uninstall-shortcuts; uv tool uninstall boba-pet   # remove
```

Your settings and models stay in `%USERPROFILE%\.boba-pet` and
`%USERPROFILE%\.claude-pet*.json`; delete them to remove everything.

## Requirements

- Windows 10 or 11
- One AI to talk through (the pet has no key of its own; pick it from the
  right-click menu > **AI provider**):
  - [Claude Code](https://docs.claude.com/claude-code) (recommended):
    `irm https://claude.ai/install.ps1 | iex`, then sign in once with `claude`
  - Codex CLI (ChatGPT plan), Antigravity CLI (Google account), or a free
    Groq / Gemini API key

## What it does

- **Chat** in a speech bubble; the chat keeps context, shows tokens used
  (↑ input, ↓ output) and has a cheap **Compact** button that rolls the chat
  into a short note.
- **Quick menu**: click the ✱ in the ask box for ask, talk, screenshot, new
  chat, compact, history, effort level and more.
- **Voice, fully offline**: talk with the mic button (Whisper), say
  "Hey Boba" to wake it (Vosk), and have replies read aloud (Windows TTS).
- **Claude Code companion**: it looks busy while a session works, celebrates
  when it finishes, sweats when tests fail and waves when Claude needs your
  permission. A usage meter shows your plan limits and today's tokens.
- **Projects**: "open my portfolio project" finds the folder and opens it in
  VS Code with Claude Code running.
- **Tasks, reminders and focus timer**, with a gentle distraction guard.
- **Hackathons**: track deadlines, and click one for a workspace with a
  checklist, notes, focus sprints, idea/pitch/judge helpers and a prompt coach
  that suggests better Claude Code prompts while you build.
- **Morning tech briefing** from real news feeds, every item linked to its source.
- **Screenshots**: snip part of the screen and ask about it.
- **Computer tools** (optional): the AI can open apps, change volume, type and
  so on. By default it **asks you first** for anything risky; set the mode in
  the menu, or turn it off.
- **Characters**: several pixel-art pets; add your own with one file (see below).

## Privacy

- API keys you paste are saved only in `%USERPROFILE%\.boba-pet\.env` on your PC.
- Speech recognition and the wake word run locally; audio never leaves the PC.
- The pet reads Claude Code transcripts locally to react to sessions; their
  text is not sent anywhere.
- Requests to the AI go through the provider you linked, on your own account.

## Run from source (for contributors)

```powershell
git clone https://github.com/AbdulRahmanAzam/boba-pet
cd boba-pet
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
.venv\Scripts\python.exe launch.py --setup   # optional: models + Start menu shortcut
.venv\Scripts\pythonw.exe claude_pet.py
```

In a clone, the pet keeps `.env`, `models/` and `shots/` next to the code
(all git-ignored). Copy `.env.example` to `.env` if you use Groq or Gemini keys.
Set `BOBA_PET_PROJECTS` (folders separated by `;`) to choose where it looks
for your projects.

### Layout

| File | What it is |
|------|------------|
| `claude_pet.py` | The pet window, menus, chat and everything that ties the parts together |
| `providers.py`, `account.py` | Talking to Claude Code, Codex, Antigravity, Groq and Gemini |
| `chatlog.py` | Chat history, token counts and compaction |
| `sprites.py`, `sprite_*.py` | Pixel-art characters |
| `listener.py`, `wake.py`, `speak.ps1` | Mic input (Whisper), wake word (Vosk), text to speech |
| `cc_watch.py`, `pet_hook.py`, `pet_status.py`, `usage.py` | Claude Code session watching and usage meter |
| `projects.py`, `vscode-ext/` | Project search and the VS Code launcher extension |
| `tasks.py`, `focus.py` | Tasks, reminders, focus timer visuals |
| `hackathons.py`, `hackmode.py` | Hackathon tracker and hackathon mode |
| `news.py` | Morning briefing from RSS/Atom feeds |
| `computer.py`, `pet_mcp.py` | Computer tools and their MCP server |
| `snip.py`, `ui_fx.py` | Screen snipping, fades, markdown and other UI helpers |
| `launch.py`, `pet_setup.py`, `pet_paths.py` | Installed command, first-run setup, data folders |

### Add a character

Drop a `sprite_<name>.py` file next to `sprites.py` with a
`compose_<name>(...)` function (copy `sprite_bluebot.py` to start), an
optional `NAME` for the menu and an optional `EXTRA_PALETTE`. It shows up in
the menu on the next start.

## Contributing

Pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
