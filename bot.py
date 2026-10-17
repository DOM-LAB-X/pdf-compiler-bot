"""Discord bot: drop photos in, get a compiled PDF back."""
import asyncio
import io
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import tasks
from dotenv import load_dotenv

import pdfmaker

load_dotenv()
TOKEN = os.environ.get("DISCORD_TOKEN", "")
GUILD_ID = os.getenv("GUILD_ID")  # optional: sync commands to one server instantly
SESSION_TTL = float(os.getenv("SESSION_TTL_HOURS", "12")) * 3600
MAX_PHOTOS = int(os.getenv("MAX_PHOTOS", "300"))
AUTO_CHANNELS = {int(x) for x in re.split(r"[,\s]+", os.getenv("AUTO_COLLECT_CHANNELS", "")) if x}

log = logging.getLogger("pdf-compiler-bot")
VERSION = (Path(__file__).parent / "VERSION").read_text().strip() if (Path(__file__).parent / "VERSION").exists() else "dev"

# ─────────────────────────────────────────────
#   Per-user photo piles
# ─────────────────────────────────────────────
@dataclass
class Photo:
    # Only the Discord attachment link is kept. The image is downloaded into memory when needed.
    attachment: discord.Attachment
    name: str
    uploaded_at: datetime
    taken_at: datetime | None = None
    date_source: str | None = None  # timemark | exif | upload | None (not checked yet)

@dataclass
class Session:
    photos: list[Photo] = field(default_factory=list)
    channels: set[int] = field(default_factory=set)  # channels where /pdf start was run
    touched: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

sessions: dict[int, Session] = {}

def get_session(user_id: int) -> Session:
    s = sessions.get(user_id)
    if not s:
        s = sessions[user_id] = Session()
    s.touched = time.time()
    return s

def drop_session(user_id: int, keep_channels: bool = True):
    s = sessions.pop(user_id, None)
    if s and keep_channels and s.channels:
        get_session(user_id).channels = s.channels

def plural(n: int, word: str = "photo") -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"

async def add_attachments(user: discord.abc.User, attachments: list[discord.Attachment], when: datetime):
    s = get_session(user.id)
    added, skipped = 0, []
    async with s.lock:
        for a in attachments:
            if not pdfmaker.looks_like_image(a.filename, a.content_type):
                skipped.append(f"{a.filename} (not an image)")
                continue
            if len(s.photos) >= MAX_PHOTOS:
                skipped.append(f"{a.filename} (limit is {MAX_PHOTOS} photos)")
                continue
            display = re.sub(r"\.(heic|heif)$", ".jpg", a.filename, flags=re.I)
            s.photos.append(Photo(a, display, when))
            added += 1
    return added, skipped, len(s.photos)

def added_message(added: int, skipped: list[str], total: int) -> str:
    parts = []
    if added:
        parts.append(f"📥 Added {plural(added)}. **{total}** in your pile. Use `/pdf make` when you're ready.")
    if skipped:
        parts.append("⚠️ Skipped: " + ", ".join(skipped[:10]) + (" …" if len(skipped) > 10 else ""))
    return "\n".join(parts) or "Nothing to add."

def parse_numbers(text: str, n: int) -> list[int]:
    """'3', '2,5,7', '4-6' -> zero-based indexes."""
    out = set()
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise ValueError(part)
        a, b = int(m[1]), int(m[2] or m[1])
        out.update(i - 1 for i in range(min(a, b), max(a, b) + 1) if 1 <= i <= n)
    return sorted(out)

# ─────────────────────────────────────────────
#   Client
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True  # needed to see attachments in normal messages

class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        expire_sessions.start()

    async def on_ready(self):
        log.info("PDF Compiler Bot v%s logged in as %s (OCR %s)", VERSION, self.user, "on" if pdfmaker.ocr_available() else "OFF")

client = Bot()

@tasks.loop(minutes=15)
async def expire_sessions():
    cutoff = time.time() - SESSION_TTL
    for uid in [u for u, s in sessions.items() if s.touched < cutoff and not s.lock.locked()]:
        drop_session(uid, keep_channels=False)

@client.event
async def on_message(msg: discord.Message):
    if msg.author.bot or not msg.attachments:
        return
    s = sessions.get(msg.author.id)
    collecting = (
        isinstance(msg.channel, discord.DMChannel)
        or msg.channel.id in AUTO_CHANNELS
        or (s is not None and msg.channel.id in s.channels)
    )
    if not collecting:
        return
    added, skipped, total = await add_attachments(msg.author, msg.attachments, msg.created_at)
    if added or skipped:
        await msg.reply(added_message(added, skipped, total), mention_author=False, silent=True)

# Right-click / long-press a message → Apps → "Add photos to PDF"
@client.tree.context_menu(name="Add photos to PDF")
async def add_from_message(inter: discord.Interaction, message: discord.Message):
    if not message.attachments:
        return await inter.response.send_message("That message has no attachments.", ephemeral=True)
    await inter.response.defer(ephemeral=True, thinking=True)
    added, skipped, total = await add_attachments(inter.user, message.attachments, message.created_at)
    await inter.followup.send(added_message(added, skipped, total), ephemeral=True)

# ─────────────────────────────────────────────
#   /pdf commands
# ─────────────────────────────────────────────
pdf = app_commands.Group(name="pdf", description="Turn photos into a PDF")
client.tree.add_command(pdf)

@pdf.command(name="start", description="Collect the photos you post in this channel (always on in DMs)")
async def pdf_start(inter: discord.Interaction):
    s = get_session(inter.user.id)
    s.channels.add(inter.channel_id)
    note = f" You already have {plural(len(s.photos))}." if s.photos else ""
    await inter.response.send_message(
        f"📸 I'll grab the photos you post here. Up to 10 per message, any image type (HEIC too).{note}\n"
        "Then run `/pdf make`. `/pdf stop` stops collecting here.", ephemeral=True)

@pdf.command(name="stop", description="Stop collecting photos in this channel (your pile is kept)")
async def pdf_stop(inter: discord.Interaction):
    s = sessions.get(inter.user.id)
    if s:
        s.channels.discard(inter.channel_id)
    await inter.response.send_message("🛑 Stopped collecting here. Your photos are still saved.", ephemeral=True)

@pdf.command(name="list", description="Show the photos in your pile, in page order")
async def pdf_list(inter: discord.Interaction):
    s = sessions.get(inter.user.id)
    if not s or not s.photos:
        return await inter.response.send_message("Your pile is empty. Post some photos first.", ephemeral=True)
    icons = {"timemark": "⏱", "exif": "📷", "upload": "📤"}
    lines = []
    for i, p in enumerate(s.photos, 1):
        date = f" · {icons.get(p.date_source, '')} {p.taken_at:%b %d %Y %I:%M %p}" if p.taken_at else ""
        lines.append(f"`{i:>3}` {discord.utils.escape_markdown(p.name)}{date}")
    text = f"**{plural(len(s.photos))}**\n" + "\n".join(lines)
    if len(text) > 1900:
        text = text[:1900].rsplit("\n", 1)[0] + "\n…"
    await inter.response.send_message(text, ephemeral=True)

@pdf.command(name="remove", description="Remove photos by number, e.g. 3 or 2,5,7 or 4-6")
@app_commands.describe(numbers="Photo numbers from /pdf list")
async def pdf_remove(inter: discord.Interaction, numbers: str):
    s = sessions.get(inter.user.id)
    if not s or not s.photos:
        return await inter.response.send_message("Your pile is empty.", ephemeral=True)
    async with s.lock:
        try:
            idx = parse_numbers(numbers, len(s.photos))
        except ValueError as e:
            return await inter.response.send_message(f"Couldn't read `{e}`. Use something like `3`, `2,5` or `4-6`.", ephemeral=True)
        for i in reversed(idx):
            s.photos.pop(i)
    await inter.response.send_message(f"🗑️ Removed {plural(len(idx))}. {plural(len(s.photos))} left.", ephemeral=True)

@pdf.command(name="move", description="Move a photo to a different position")
@app_commands.describe(number="Photo number to move", to="New position")
async def pdf_move(inter: discord.Interaction, number: int, to: int):
    s = sessions.get(inter.user.id)
    n = len(s.photos) if s else 0
    if not (1 <= number <= n and 1 <= to <= n):
        return await inter.response.send_message(f"Numbers must be between 1 and {n}.", ephemeral=True)
    async with s.lock:
        s.photos.insert(to - 1, s.photos.pop(number - 1))
    await inter.response.send_message(f"↕️ Moved photo {number} to position {to}.", ephemeral=True)

@pdf.command(name="clear", description="Delete all photos in your pile")
async def pdf_clear(inter: discord.Interaction):
    s = sessions.get(inter.user.id)
    if s and s.lock.locked():
        return await inter.response.send_message("A PDF is being made right now, try again in a moment.", ephemeral=True)
    drop_session(inter.user.id)
    await inter.response.send_message("🧹 Cleared. Post new photos whenever you're ready.", ephemeral=True)

@pdf.command(name="help", description="How to use the PDF bot")
async def pdf_help(inter: discord.Interaction):
    await inter.response.send_message(
        "**Photos → PDF**\n"
        "1. DM me photos, or run `/pdf start` in a channel and post them there. You can also long-press "
        "any message → **Apps → Add photos to PDF**.\n"
        "2. `/pdf list` shows the order. Fix it with `/pdf move` / `/pdf remove`, or use `sort` in `/pdf make`.\n"
        "3. `/pdf make` builds the PDF. Options: orientation, fit, size, sort by date, a label "
        "(use `|` for new lines, e.g. `PO 25OSPC055 | @ KOMO MAI DR. | TAKEN 4/16/26`).\n"
        "Your pile is cleared after the PDF is made unless you set `keep: True`.",
        ephemeral=True)

async def fetch(p: Photo) -> bytes | None:
    """Download one photo into memory (None if the message was deleted or the link expired)."""
    try:
        return await p.attachment.read()
    except discord.HTTPException:
        return None


async def resolve_dates(photos: list[Photo]) -> dict[str, int]:
    use_ocr = pdfmaker.ocr_available()
    for p in photos:
        if p.date_source is None:
            data = await fetch(p)
            d, src = await asyncio.to_thread(pdfmaker.find_date, data, use_ocr) if data else (None, None)
            p.taken_at, p.date_source = (d, src) if d else (p.uploaded_at.replace(tzinfo=None), "upload")
    counts: dict[str, int] = {}
    for p in photos:
        counts[p.date_source] = counts.get(p.date_source, 0) + 1
    return counts

async def show_progress(inter: discord.Interaction, state: dict):
    last = None
    while True:
        await asyncio.sleep(3)
        text = state["text"]
        if text != last:
            last = text
            try:
                await inter.edit_original_response(content=text)
            except discord.HTTPException:
                pass

@pdf.command(name="make", description="Build a PDF from your photos")
@app_commands.describe(
    filename="Name for the PDF (default: photos)",
    orientation="Page orientation (letter size)",
    fit="How photos sit on the page",
    size="File size vs. quality",
    sort="Page order",
    label="Text stamped on every photo. Use | for new lines",
    label_position="Where the label goes",
    label_color="Label text color",
    label_font="Label font",
    keep="Keep your photos after making the PDF (default: clear them)",
)
@app_commands.choices(
    orientation=[Choice(name="Portrait", value="portrait"), Choice(name="Landscape", value="landscape")],
    fit=[Choice(name="Fit: whole photo with margins", value="fit"),
         Choice(name="Fill: fill page, edges may crop", value="fill"),
         Choice(name="Stretch: fill exactly (may distort)", value="stretch")],
    size=[Choice(name="Small: 1200px, smallest file", value="small"),
          Choice(name="Balanced: 1800px (default)", value="balanced"),
          Choice(name="Original: full resolution", value="original")],
    sort=[Choice(name="Order I sent them (default)", value="upload"),
          Choice(name="Date taken: oldest → newest", value="asc"),
          Choice(name="Date taken: newest → oldest", value="desc")],
    label_position=[Choice(name=n, value=v) for n, v in [
        ("↖ Top left", "tl"), ("↗ Top right", "tr"), ("← Middle left", "ml"),
        ("→ Middle right", "mr"), ("↙ Bottom left", "bl"), ("↘ Bottom right", "br")]],
    label_color=[Choice(name=c.title(), value=c) for c in pdfmaker.COLORS],
    label_font=[Choice(name="Arial", value="sans"), Choice(name="Times", value="serif")],
)
async def pdf_make(
    inter: discord.Interaction,
    filename: str = "photos",
    orientation: str = "portrait",
    fit: str = "fit",
    size: str = "balanced",
    sort: str = "upload",
    label: str = "",
    label_position: str = "tl",
    label_color: str = "red",
    label_font: str = "sans",
    keep: bool = False,
):
    s = sessions.get(inter.user.id)
    if not s or not s.photos:
        return await inter.response.send_message(
            "Your pile is empty. DM me photos, or `/pdf start` here and post them.", ephemeral=True)
    if s.lock.locked():
        return await inter.response.send_message("Already working on one, hang on.", ephemeral=True)
    await inter.response.defer(thinking=True)

    async with s.lock:
        photos = list(s.photos)
        notes = []
        if sort != "upload":
            state = {"text": f"⏳ Reading dates from {plural(len(photos))}…"}
            ticker = asyncio.create_task(show_progress(inter, state))
            try:
                counts = await resolve_dates(photos)
            finally:
                ticker.cancel()
            photos.sort(key=lambda p: p.taken_at, reverse=(sort == "desc"))
            names = {"timemark": "Timemark ⏱", "exif": "EXIF 📷", "upload": "upload time 📤"}
            notes.append(("Sorted oldest → newest" if sort == "asc" else "Sorted newest → oldest")
                         + " (" + ", ".join(f"{n} by {names[k]}" for k, n in counts.items()) + ")")

        opts = pdfmaker.Options(orientation, fit, size, label, label_position, label_color, label_font)
        name = pdfmaker.safe_filename(filename)
        limit = max(1_000_000, inter.filesize_limit - 256 * 1024)  # headroom for upload overhead

        state = {"text": "⏳ Starting…"}
        ticker = asyncio.create_task(show_progress(inter, state))
        try:
            files = await build_within_limit(photos, opts, name, limit, state, notes)
        except Exception as e:
            log.exception("PDF build failed")
            ticker.cancel()
            return await inter.edit_original_response(content=f"❌ Something went wrong: `{e}`")
        ticker.cancel()

        summary = f"✅ **{name}**: {plural(len(photos), 'page')}"
        if label_lines := pdfmaker.label_lines(label):
            summary += f", labeled “{' / '.join(label_lines)}”"
        if notes:
            summary += "\n" + "\n".join(f"-# {n}" for n in notes)
        if not keep:
            drop_session(inter.user.id)
        else:
            summary += f"\n-# Photos kept ({len(s.photos)}). `/pdf clear` when done."

    first, *rest = files
    await inter.edit_original_response(content=summary, attachments=[first])
    for f in rest:
        await inter.followup.send(file=f)

async def build_within_limit(photos, opts, name, limit, state, notes) -> list[discord.File]:
    """Build the PDF. If it's over Discord's upload limit, drop the size preset, then split into parts."""
    sizes = pdfmaker.SIZE_ORDER[pdfmaker.SIZE_ORDER.index(opts.size):]

    for size in sizes:
        opts.size = size
        pages, failed = [], []
        for i, p in enumerate(photos, 1):
            state["text"] = f"⏳ Processing photo {i}/{len(photos)} ({size})…"
            data = await fetch(p)  # in memory only, dropped right after this photo is done
            try:
                pages.append(await asyncio.to_thread(pdfmaker.render_page, data, opts))
            except Exception:
                failed.append(p.name)
        if not pages:
            raise RuntimeError("none of the photos could be read (were the messages deleted?)")
        total = len(pages)
        state["text"] = "⏳ Building PDF…"
        data = await asyncio.to_thread(pdfmaker.pages_to_pdf, pages, opts, name[:-4])
        if len(data) <= limit:
            if failed:
                notes.append("Skipped (unreadable or deleted): " + ", ".join(failed[:10]))
            if size != sizes[0]:
                notes.append(f"Shrunk to **{size}** quality to fit Discord's {limit / 1e6:.0f} MB upload limit")
            return [discord.File(io.BytesIO(data), filename=name)]
        if size != sizes[-1]:
            state["text"] = f"⏳ {len(data) / 1e6:.1f} MB is too big for Discord, retrying smaller…"

    # Still too big at "small": split into parts that each fit
    state["text"] = "⏳ Splitting into parts…"
    per = max(1, int(total * limit / len(data) * 0.9))
    while True:
        chunks = [pages[i:i + per] for i in range(0, total, per)]
        built = [await asyncio.to_thread(pdfmaker.pages_to_pdf, c, opts, name[:-4]) for c in chunks]
        if all(len(b) <= limit for b in built):
            break
        if per == 1:
            raise RuntimeError("a single photo is bigger than Discord's upload limit")
        per = max(1, int(per * 0.8))

    if failed:
        notes.append("Skipped (unreadable or deleted): " + ", ".join(failed[:10]))
    notes.append(f"Split into {len(chunks)} parts to fit Discord's {limit / 1e6:.0f} MB upload limit")
    stem = name[:-4]
    return [discord.File(io.BytesIO(b), filename=f"{stem}_part{i}of{len(built)}.pdf")
            for i, b in enumerate(built, 1)]

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in .env (see .env.example)")
    client.run(TOKEN, root_logger=True)
