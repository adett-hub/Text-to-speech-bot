# Discord TTS Bot

Reads text aloud in a voice channel using free Microsoft Edge neural voices (no API key).

## Commands
| Command | What it does |
|---|---|
| `/join` / `/leave` | Bot joins or leaves your voice channel |
| `/say <text>` | Speaks a line once |
| `/ttschannel [#channel]` | (Manage Server) Auto-read every message in that channel while the bot is in voice. Run with no channel to turn off |
| `/voice <name>` | Pick your personal voice — autocomplete searches ~400 voices (try `en-GB`, `Guy`, `Jenny`) |
| `/skip` / `/clear` | Skip the current line / empty the queue |

Messages starting with `//` are not read. Links, custom emoji, spoilers and code blocks are spoken as short placeholders, and the bot leaves on its own when the channel empties.

## Setup (Windows)

**1. Create the bot** at https://discord.com/developers/applications
- New Application → **Bot** tab → *Reset Token* and copy it
- Same tab: turn on **Message Content Intent**
- **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; permissions *View Channels, Send Messages, Connect, Speak*. Open the URL to invite it.

**2. Install FFmpeg** (plays the audio)
```powershell
winget install Gyan.FFmpeg
```
Open a new terminal and check `ffmpeg -version` works.

**3. Install and run** (Python 3.10+)
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # then paste your token into .env
python bot.py
```

**Tip:** put your server's ID in `DEV_GUILD_ID` (right-click server → Copy Server ID, with Developer Mode on) so slash commands show up instantly instead of after a few minutes.

## Notes
- discord.py 2.7.1+ is required: Discord has required end-to-end encrypted voice (DAVE) since March 2026, and older versions can't join voice channels.
- Settings (TTS channels, chosen voices) are saved to `settings.json` next to the bot.
- Keep `.env` private — anyone with the token controls the bot.
