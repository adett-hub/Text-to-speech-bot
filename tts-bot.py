"""
Discord Text-to-Speech bot.

Reads messages aloud in a voice channel using Microsoft Edge neural voices (free, no API key).
- /join, /leave            : bring the bot in / out of your voice channel
- /say <text>              : speak something once
- /ttschannel [channel]    : (admins) auto-read every message posted in a text channel
- /voice <name>            : pick your personal voice (with autocomplete)
- /skip, /clear            : skip the current line / empty the queue
"""

import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path

import discord
import edge_tts
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID")  # optional: instant slash-command sync to one server
DEFAULT_VOICE = os.getenv("DEFAULT_VOICE", "en-US-AriaNeural")
MAX_CHARS = int(os.getenv("MAX_CHARS", "300"))
ANNOUNCE_NAMES = os.getenv("ANNOUNCE_NAMES", "true").lower() == "true"
SKIP_PREFIX = os.getenv("SKIP_PREFIX", "//")  # messages starting with this are not read
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")
SETTINGS_PATH = Path(os.getenv("SETTINGS_PATH", "settings.json"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ttsbot")


# ---------------------------------------------------------------- settings ---

class Settings:
    """Tiny JSON store: per-guild TTS channel and per-user voice."""

    def __init__(self, path: Path):
        self.path = path
        self.data = {"guilds": {}, "voices": {}}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                log.warning("Could not read %s, starting fresh", path)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def tts_channel(self, guild_id: int) -> int | None:
        return self.data["guilds"].get(str(guild_id), {}).get("tts_channel")

    def set_tts_channel(self, guild_id: int, channel_id: int | None):
        self.data["guilds"].setdefault(str(guild_id), {})["tts_channel"] = channel_id
        self.save()

    def voice_for(self, user_id: int) -> str:
        return self.data["voices"].get(str(user_id), DEFAULT_VOICE)

    def set_voice(self, user_id: int, voice: str):
        self.data["voices"][str(user_id)] = voice
        self.save()


# ------------------------------------------------------------- text clean ---

URL_RE = re.compile(r"https?://\S+")
CUSTOM_EMOJI_RE = re.compile(r"<a?:(\w+):\d+>")
CODE_BLOCK_RE = re.compile(r"```.*?```", re.S)
SPOILER_RE = re.compile(r"\|\|.*?\|\|", re.S)
REPEAT_RE = re.compile(r"(.)\1{4,}")  # "hiiiiiiii" -> "hiii"


def clean_text(text: str) -> str:
    text = CODE_BLOCK_RE.sub(" code block ", text)
    text = SPOILER_RE.sub(" spoiler ", text)
    text = URL_RE.sub(" link ", text)
    text = CUSTOM_EMOJI_RE.sub(r" \1 ", text)
    text = REPEAT_RE.sub(r"\1\1\1", text)
    text = re.sub(r"[*_~`>]", "", text)  # markdown symbols
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rsplit(" ", 1)[0] + ", and so on"
    return text


# ------------------------------------------------------------------ player ---

class GuildPlayer:
    """One speaking queue per server, so lines never talk over each other."""

    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self.last_speaker: int | None = None
        self.task = asyncio.create_task(self._run())

    def enqueue(self, text: str, voice: str):
        self.queue.put_nowait((text, voice))

    def clear(self):
        while not self.queue.empty():
            self.queue.get_nowait()

    def stop(self):
        self.clear()
        self.task.cancel()

    async def _run(self):
        loop = asyncio.get_running_loop()
        while True:
            text, voice = await self.queue.get()
            vc = self.guild.voice_client
            if not vc or not vc.is_connected():
                continue
            fd, path = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)
            try:
                await edge_tts.Communicate(text, voice).save(path)
                done = asyncio.Event()
                vc.play(
                    discord.FFmpegPCMAudio(path, executable=FFMPEG_PATH),
                    after=lambda err: loop.call_soon_threadsafe(done.set),
                )
                await done.wait()
            except Exception:
                log.exception("Failed to speak in guild %s", self.guild.id)
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass


# --------------------------------------------------------------------- bot ---

class TTSBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # needed to read channel messages aloud
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings = Settings(SETTINGS_PATH)
        self.players: dict[int, GuildPlayer] = {}
        self.voices: list[str] = []

    async def setup_hook(self):
        self.voices = sorted(v["ShortName"] for v in await edge_tts.list_voices())
        log.info("Loaded %d voices", len(self.voices))
        if DEV_GUILD_ID:
            guild = discord.Object(id=int(DEV_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()  # global sync can take a few minutes to show up

    def player(self, guild: discord.Guild) -> GuildPlayer:
        if guild.id not in self.players:
            self.players[guild.id] = GuildPlayer(guild)
        return self.players[guild.id]

    async def disconnect(self, guild: discord.Guild):
        if guild.id in self.players:
            self.players.pop(guild.id).stop()
        if guild.voice_client:
            await guild.voice_client.disconnect(force=True)


bot = TTSBot()


async def ensure_connected(interaction: discord.Interaction, ephemeral: bool = False) -> discord.VoiceClient | None:
    """Defer the reply, then join (or move to) the caller's voice channel.
    On success, callers must answer with interaction.followup.send(...).
    On failure, an error is sent and None is returned."""
    member = interaction.user
    if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
        await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
        return None
    await interaction.response.defer(ephemeral=ephemeral, thinking=True)  # connecting can take > 3s
    target = member.voice.channel
    vc = interaction.guild.voice_client
    try:
        if vc and vc.channel != target:
            await vc.move_to(target)
        elif not vc:
            await target.connect(self_deaf=True, timeout=20)
    except Exception as e:
        log.exception("Voice connect failed")
        await interaction.followup.send(f"Couldn't join voice: {e}", ephemeral=True)
        return None
    return interaction.guild.voice_client


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    if bot.settings.tts_channel(message.guild.id) != message.channel.id:
        return
    vc = message.guild.voice_client
    if not vc or not vc.is_connected():
        return
    if message.content.startswith(SKIP_PREFIX):
        return

    text = clean_text(message.clean_content)
    if not text and message.attachments:
        text = "sent an attachment"
    if not text:
        return

    player = bot.player(message.guild)
    if ANNOUNCE_NAMES and player.last_speaker != message.author.id:
        text = f"{message.author.display_name} says: {text}"
    player.last_speaker = message.author.id
    player.enqueue(text, bot.settings.voice_for(message.author.id))


@bot.event
async def on_voice_state_update(member: discord.Member, before, after):
    # Leave when everyone else has left the channel.
    vc = member.guild.voice_client
    if vc and vc.channel and not any(not m.bot for m in vc.channel.members):
        await bot.disconnect(member.guild)


# ---------------------------------------------------------------- commands ---

@bot.tree.command(description="Join your voice channel")
@app_commands.guild_only()
async def join(interaction: discord.Interaction):
    if await ensure_connected(interaction):
        channel_id = bot.settings.tts_channel(interaction.guild.id)
        hint = f" Reading messages from <#{channel_id}>." if channel_id else " Use /say, or have an admin set /ttschannel."
        await interaction.followup.send(f"Joined.{hint}")


@bot.tree.command(description="Leave the voice channel")
@app_commands.guild_only()
async def leave(interaction: discord.Interaction):
    if not interaction.guild.voice_client:
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
        return
    await bot.disconnect(interaction.guild)
    await interaction.response.send_message("Bye!")


@bot.tree.command(description="Say something out loud")
@app_commands.guild_only()
@app_commands.describe(text="What to say")
async def say(interaction: discord.Interaction, text: app_commands.Range[str, 1, 1000]):
    if not await ensure_connected(interaction, ephemeral=True):
        return
    cleaned = clean_text(text)
    if not cleaned:
        await interaction.followup.send("Nothing to say after cleanup.", ephemeral=True)
        return
    bot.player(interaction.guild).enqueue(cleaned, bot.settings.voice_for(interaction.user.id))
    await interaction.followup.send(f"🔊 {cleaned}", ephemeral=True)


@bot.tree.command(description="Skip the line currently being spoken")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
    await interaction.response.send_message("Skipped.", ephemeral=True)


@bot.tree.command(description="Clear everything waiting to be spoken")
@app_commands.guild_only()
async def clear(interaction: discord.Interaction):
    if interaction.guild.id in bot.players:
        bot.players[interaction.guild.id].clear()
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
    await interaction.response.send_message("Queue cleared.", ephemeral=True)


@bot.tree.command(description="Choose your TTS voice (e.g. en-US-GuyNeural)")
@app_commands.describe(name="Voice name; start typing to search, e.g. 'en-GB' or 'Guy'")
async def voice(interaction: discord.Interaction, name: str):
    if name not in bot.voices:
        await interaction.response.send_message(f"Unknown voice `{name}`. Pick one from the list.", ephemeral=True)
        return
    bot.settings.set_voice(interaction.user.id, name)
    await interaction.response.send_message(f"Your voice is now `{name}`.", ephemeral=True)


@voice.autocomplete("name")
async def voice_autocomplete(interaction: discord.Interaction, current: str):
    current = current.lower()
    matches = [v for v in bot.voices if current in v.lower()]
    return [app_commands.Choice(name=v, value=v) for v in matches[:25]]


@bot.tree.command(description="Set the text channel to read aloud (leave empty to turn off)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def ttschannel(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    bot.settings.set_tts_channel(interaction.guild.id, channel.id if channel else None)
    if channel:
        await interaction.response.send_message(
            f"I'll read messages from {channel.mention} while I'm in voice. "
            f"Start a message with `{SKIP_PREFIX}` to keep it silent."
        )
    else:
        await interaction.response.send_message("Auto-reading turned off.")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file")
    bot.run(TOKEN, log_handler=None)
