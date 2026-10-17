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
WORKERS = int(os.getenv("PROCESS_WORKERS", "4"))  # photos processed at the same time
CACHE_MB = float(os.getenv("CACHE_MB", "1000"))  # RAM for photos pre-processed while you upload

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
    # Pre-processed in the background as soon as the photo is added, so Generate is quick (RAM only)
    base: pdfmaker.Page | None = None  # rendered without a label
    base_size: str | None = None
    prep: asyncio.Task | None = None

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
    for p in s.photos if s else []:
        if p.prep:
            p.prep.cancel()
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
            photo = Photo(a, display, when)
            s.photos.append(photo)
            photo.prep = asyncio.create_task(prepare(photo, get_prefs(user.id)))
            added += 1
    return added, skipped, len(s.photos)


prep_sem = asyncio.Semaphore(WORKERS)


def cache_used() -> int:
    return sum(len(p.base.jpeg) for s in sessions.values() for p in s.photos if p.base)


async def prepare(p: Photo, pref: dict):
    """Download + render one photo in the background right after it's added (with the user's last
    quality setting) and read its date if they sort by date. Only the small rendered page is kept."""
    async with prep_sem:
        data = await fetch(p)
        if not data:
            return
        if pref["sort"] != "upload" and p.date_source is None:
            set_date(p, *await asyncio.to_thread(pdfmaker.find_date, data, pdfmaker.ocr_available()))
        if cache_used() < CACHE_MB * 1e6:
            try:
                page = await asyncio.to_thread(pdfmaker.render_page, data, pdfmaker.Options(size=pref["size"]))
            except Exception:
                return  # tried again (and reported) when the PDF is built
            if cache_used() + len(page.jpeg) <= CACHE_MB * 1e6:  # re-check: others may have finished meanwhile
                p.base, p.base_size = page, pref["size"]


def set_date(p: Photo, d: datetime | None, src: str | None):
    p.taken_at, p.date_source = (d, src) if d else (p.uploaded_at.replace(tzinfo=None), "upload")

def added_message(added: int, skipped: list[str], total: int) -> str:
    parts = []
    if added:
        parts.append(f"📥 Added {plural(added)}. **{total}** in your pile. When you're done: **Generate PDF** on the panel, or `/pdf stop`.")
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
        self.add_view(PanelView())  # panel buttons keep working after a restart
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
        "When you're done, run `/pdf stop`: it asks for a file name and makes the PDF.", ephemeral=True)

@pdf.command(name="stop", description="Stop collecting here, name the PDF and make it")
async def pdf_stop(inter: discord.Interaction):
    s = sessions.get(inter.user.id)
    if s:
        s.channels.discard(inter.channel_id)
    if not s or not s.photos:
        return await inter.response.send_message("🛑 Stopped collecting here. You have no photos yet.", ephemeral=True)
    await inter.response.send_modal(NameModal(inter.user.id))

@pdf.command(name="list", description="Show the photos in your pile, in page order")
async def pdf_list(inter: discord.Interaction):
    s = sessions.get(inter.user.id)
    if not s or not s.photos:
        return await inter.response.send_message("Your pile is empty. Post some photos first.", ephemeral=True)
    await inter.response.send_message(list_text(s), ephemeral=True)


def list_text(s: Session) -> str:
    icons = {"timemark": "⏱", "exif": "📷", "upload": "📤"}
    lines = []
    for i, p in enumerate(s.photos, 1):
        date = f" · {icons.get(p.date_source, '')} {p.taken_at:%b %d %Y %I:%M %p}" if p.taken_at else ""
        lines.append(f"`{i:>3}` {discord.utils.escape_markdown(p.name)}{date}")
    text = f"**{plural(len(s.photos))}**\n" + "\n".join(lines)
    if len(text) > 1900:
        text = text[:1900].rsplit("\n", 1)[0] + "\n…"
    return text

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
        "**Quickest:** DM me photos (or `/pdf start` in a channel and post them), then `/pdf stop`. "
        "It asks for a file name and makes the PDF.\n"
        "**Buttons:** `/pdf panel` posts a panel: Add Photos → drop photos in chat → Generate PDF → name it.\n\n"
        "**More commands:**\n"
        "• Long-press any message → **Apps → Add photos to PDF** to add someone else's photos.\n"
        "• `/pdf list` shows the order. Fix it with `/pdf move` / `/pdf remove`, or pick a date order in the form.\n"
        "• `/pdf make` builds the PDF with every option as a command field: orientation, fit, size, sort, a label "
        "(use `|` for new lines, e.g. `PO 25OSPC055 | @ KOMO MAI DR. | TAKEN 4/16/26`).\n"
        "Photos are processed while you upload, so the PDF is ready right after you name it. "
        "Your pile is cleared after the PDF is made unless you tick **Keep** (`keep: True`).",
        ephemeral=True)

async def fetch(p: Photo) -> bytes | None:
    """Download one photo into memory (None if the message was deleted or the link expired)."""
    try:
        return await p.attachment.read()
    except discord.HTTPException:
        return None


async def resolve_dates(photos: list[Photo]) -> dict[str, int]:
    use_ocr = pdfmaker.ocr_available()
    sem = asyncio.Semaphore(WORKERS)

    async def one(p: Photo):
        async with sem:
            data = await fetch(p)
            set_date(p, *(await asyncio.to_thread(pdfmaker.find_date, data, use_ocr) if data else (None, None)))

    await asyncio.gather(*(one(p) for p in photos if p.date_source is None))
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
    opts = pdfmaker.Options(orientation, fit, size, label, label_position, label_color, label_font)
    await generate(inter, s, opts, filename, sort, keep)


async def generate(inter: discord.Interaction, s: Session, opts: pdfmaker.Options, filename: str, sort: str, keep: bool):
    """Build and send the PDF. The interaction must already be deferred."""
    label = opts.label
    async with s.lock:
        photos = list(s.photos)
        notes = []
        pending = [p.prep for p in photos if p.prep and not p.prep.done()]
        if pending:  # still pre-processing the last photos you dropped: let those finish instead of redoing them
            state = {"text": f"⏳ Processing {plural(len(pending))}…"}
            ticker = asyncio.create_task(show_progress(inter, state))
            await asyncio.wait(pending)
            ticker.cancel()
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
        sem, done = asyncio.Semaphore(WORKERS), 0

        async def one(p: Photo, size=size):
            nonlocal done
            async with sem:
                try:
                    if p.base and p.base_size == size and not opts.label:
                        return p.base  # pre-processed while uploading: nothing left to do
                    if p.base and pdfmaker.can_derive(p.base_size, size):
                        data = p.base.jpeg  # add the label / shrink from the pre-processed page, no download
                    else:
                        data = await fetch(p)  # in memory only, dropped right after this photo is done
                    return await asyncio.to_thread(pdfmaker.render_page, data, opts)
                except Exception:
                    return None
                finally:
                    done += 1
                    state["text"] = f"⏳ Processing photo {done}/{len(photos)} ({size})…"

        results = await asyncio.gather(*(one(p) for p in photos))  # keeps page order
        pages = [r for r in results if r]
        failed = [p.name for p, r in zip(photos, results) if r is None]
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

# ─────────────────────────────────────────────
#   Button panel + forms  (/pdf panel)
# ─────────────────────────────────────────────
ACCENT = discord.Color.from_str("#e54d2e")
PAGE_CHOICES = {
    "portrait|fit": "Portrait · Fit (whole photo, margins)",
    "portrait|fill": "Portrait · Fill (edges may crop)",
    "portrait|stretch": "Portrait · Stretch (may distort)",
    "landscape|fit": "Landscape · Fit (whole photo, margins)",
    "landscape|fill": "Landscape · Fill (edges may crop)",
    "landscape|stretch": "Landscape · Stretch (may distort)",
}
SIZE_CHOICES = {"small": "Small file (1200px)", "balanced": "Balanced (1800px)", "original": "Original quality"}
SORT_CHOICES = {"upload": "Order I uploaded them", "asc": "Date taken: oldest → newest", "desc": "Date taken: newest → oldest"}
POSITION_CHOICES = {"tl": "Label top left ↖", "tr": "Label top right ↗", "ml": "Label middle left ←",
                    "mr": "Label middle right →", "bl": "Label bottom left ↙", "br": "Label bottom right ↘"}
FONT_NAMES = {"sans": "Arial", "serif": "Times"}

# Last-used choices per user (kept after a PDF is made, unlike the photos)
prefs: dict[int, dict] = {}
DEFAULT_PREFS = {"page": "portrait|fit", "size": "balanced", "sort": "upload", "label_position": "tl",
                 "label_color": "red", "label_font": "sans", "filename": "photos", "label": ""}


def get_prefs(user_id: int) -> dict:
    return prefs.setdefault(user_id, dict(DEFAULT_PREFS))


def panel_embed() -> discord.Embed:
    e = discord.Embed(
        title="📄 PDF Compiler",
        color=ACCENT,
        description=(
            "Turn your photos into one PDF in **two steps**:\n"
            "`01` 📤 **Add Photos**, then drop your photos in the chat. Any format (HEIC too), as many as you want\n"
            "`02` 📄 **Generate PDF**: name it, pick the layout, done\n\n"
            "🗂️ **My Photos** to check the order · ⚙️ **Options** for quality and label style\n"
            "-# You can also DM me photos. Photos are never saved to disk, only held in memory until you generate."
        ),
    )
    if client.user:
        e.set_thumbnail(url=client.user.display_avatar.url)
    return e


class PanelView(discord.ui.View):
    """The main buttons. Persistent: keeps working after the bot restarts."""

    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        # A panel posted by an older version updates itself to the current buttons on first click
        msg = inter.message
        if msg and not msg.flags.ephemeral:
            current = [(b.custom_id, b.label) for b in self.children]
            posted = [(c.custom_id, c.label) for row in msg.components for c in getattr(row, "children", [])]
            if posted != current:
                asyncio.create_task(update_panel(msg))
        return True

    @discord.ui.button(label="Add Photos", emoji="📤", style=discord.ButtonStyle.success, custom_id="pdfpanel:upload")
    async def upload(self, inter: discord.Interaction, _):
        s = get_session(inter.user.id)
        s.channels.add(inter.channel_id)  # collect what this user posts here (Discord's normal upload is much faster)
        have = f" You have **{plural(len(s.photos))}** so far." if s.photos else ""
        await inter.response.send_message(
            f"📸 **Drop your photos right here in the chat**: drag them in or paste them, up to 10 per message, "
            f"as many messages as you want. I'll confirm each batch.{have}\nThen hit **Generate PDF**.",
            view=AddPhotosView(), ephemeral=True)

    @discord.ui.button(label="My Photos", emoji="🗂️", style=discord.ButtonStyle.primary, custom_id="pdfpanel:photos")
    async def photos(self, inter: discord.Interaction, _):
        s = sessions.get(inter.user.id)
        if not s or not s.photos:
            return await inter.response.send_message("You haven't uploaded any photos yet.", view=PanelView(), ephemeral=True)
        await inter.response.send_message(list_text(s), view=PhotosView(), ephemeral=True)

    @discord.ui.button(label="Generate PDF", emoji="📄", style=discord.ButtonStyle.success, custom_id="pdfpanel:generate")
    async def generate_btn(self, inter: discord.Interaction, _):
        await open_name_form(inter)

    @discord.ui.button(label="Options", emoji="⚙️", style=discord.ButtonStyle.secondary, custom_id="pdfpanel:options")
    async def options(self, inter: discord.Interaction, _):
        await inter.response.send_message(settings_text(), view=SettingsView(inter.user.id), ephemeral=True)

    @discord.ui.button(label="Clear", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="pdfpanel:clear")
    async def clear(self, inter: discord.Interaction, _):
        s = sessions.get(inter.user.id)
        if s and s.lock.locked():
            return await inter.response.send_message("A PDF is being made right now, try again in a moment.", ephemeral=True)
        drop_session(inter.user.id)
        await inter.response.send_message("🧹 Cleared your photos.", ephemeral=True)


async def update_panel(msg: discord.Message):
    try:
        await msg.edit(embed=panel_embed(), view=PanelView())
    except discord.HTTPException:
        pass


async def open_name_form(inter: discord.Interaction):
    """Generate PDF: straight to the name form (page options are in it, the rest are remembered)."""
    s = sessions.get(inter.user.id)
    if not s or not s.photos:
        return await inter.response.send_message("No photos yet. Hit **Add Photos** and drop some in the chat first.", ephemeral=True)
    await inter.response.send_modal(NameModal(inter.user.id))


class AddPhotosView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900)

    @discord.ui.button(label="Generate PDF", emoji="📄", style=discord.ButtonStyle.success)
    async def gen(self, inter: discord.Interaction, _):
        await open_name_form(inter)

    @discord.ui.button(label="Use upload form instead", emoji="🗃️", style=discord.ButtonStyle.secondary)
    async def form(self, inter: discord.Interaction, _):
        await inter.response.send_modal(UploadModal())


class UploadModal(discord.ui.Modal, title="Upload photos"):
    def __init__(self):
        super().__init__()
        self.uploads = [discord.ui.FileUpload(max_values=10, required=(i == 0)) for i in range(3)]
        for i, up in enumerate(self.uploads):
            self.add_item(discord.ui.Label(
                text="Photos" if i == 0 else "More photos (optional)",
                description="Up to 10 here. JPG, PNG, HEIC, WebP…" if i == 0 else None,
                component=up))

    async def on_submit(self, inter: discord.Interaction):
        files = [a for up in self.uploads for a in up.values]
        await inter.response.defer(ephemeral=True, thinking=True)
        added, skipped, total = await add_attachments(inter.user, files, discord.utils.utcnow())
        await inter.followup.send(added_message(added, skipped, total),
                                  view=PanelView(), ephemeral=True)


class PhotosView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900)

    @discord.ui.button(label="Remove…", emoji="➖", style=discord.ButtonStyle.secondary)
    async def remove(self, inter: discord.Interaction, _):
        await inter.response.send_modal(RemoveModal())

    @discord.ui.button(label="Move…", emoji="↕️", style=discord.ButtonStyle.secondary)
    async def move(self, inter: discord.Interaction, _):
        await inter.response.send_modal(MoveModal())

    @discord.ui.button(label="Generate PDF", emoji="📄", style=discord.ButtonStyle.success)
    async def gen(self, inter: discord.Interaction, _):
        await open_name_form(inter)


class RemoveModal(discord.ui.Modal, title="Remove photos"):
    numbers = discord.ui.TextInput(label="Photo numbers", placeholder="e.g. 3   or   2,5,7   or   4-6")

    async def on_submit(self, inter: discord.Interaction):
        s = sessions.get(inter.user.id)
        if not s or not s.photos:
            return await inter.response.edit_message(content="No photos left.", view=None)
        try:
            idx = parse_numbers(self.numbers.value, len(s.photos))
        except ValueError as e:
            return await inter.response.send_message(f"Couldn't read `{e}`.", ephemeral=True)
        async with s.lock:
            for i in reversed(idx):
                s.photos.pop(i)
        msg = f"🗑️ Removed {plural(len(idx))}.\n" + (list_text(s) if s.photos else "No photos left.")
        await inter.response.edit_message(content=msg, view=PhotosView() if s.photos else None)


class MoveModal(discord.ui.Modal, title="Move a photo"):
    number = discord.ui.TextInput(label="Photo number", placeholder="e.g. 5", max_length=4)
    to = discord.ui.TextInput(label="New position", placeholder="e.g. 1", max_length=4)

    async def on_submit(self, inter: discord.Interaction):
        s = sessions.get(inter.user.id)
        n = len(s.photos) if s else 0
        try:
            a, b = int(self.number.value), int(self.to.value)
            assert 1 <= a <= n and 1 <= b <= n
        except (ValueError, AssertionError):
            return await inter.response.send_message(f"Numbers must be between 1 and {n}.", ephemeral=True)
        async with s.lock:
            s.photos.insert(b - 1, s.photos.pop(a - 1))
        await inter.response.edit_message(content=f"↕️ Moved photo {a} to position {b}.\n" + list_text(s), view=PhotosView())


def settings_text() -> str:
    return ("**⚙️ Options**: saved as you change them and used every time you hit **Generate PDF**.\n"
            "-# Photos you add from now on are pre-processed at this quality, so pick it before uploading.")


def _select(placeholder: str, choices: dict, current: str, row: int) -> discord.ui.Select:
    return discord.ui.Select(placeholder=placeholder, row=row, options=[
        discord.SelectOption(label=label, value=value, default=(value == current)) for value, label in choices.items()])


class SettingsView(discord.ui.View):
    """Quality and label style. Changes are remembered for next time (layout and order are in the name form)."""

    def __init__(self, user_id: int):
        super().__init__(timeout=900)
        p = get_prefs(user_id)
        for row, (key, placeholder, choices) in enumerate([
            ("size", "Quality", SIZE_CHOICES), ("label_position", "Label position", POSITION_CHOICES),
        ]):
            sel = _select(placeholder, choices, p[key], row)
            sel.callback = self._on_select(key, sel)
            self.add_item(sel)
        self.color_btn.label = f"Label color: {p['label_color'].title()}"
        self.font_btn.label = f"Font: {FONT_NAMES[p['label_font']]}"

    def _on_select(self, key: str, sel: discord.ui.Select):
        async def callback(inter: discord.Interaction):
            get_prefs(inter.user.id)[key] = sel.values[0]
            await inter.response.edit_message(view=SettingsView(inter.user.id))
        return callback

    @discord.ui.button(label="Label color", emoji="🎨", style=discord.ButtonStyle.secondary, row=2)
    async def color_btn(self, inter: discord.Interaction, _):
        p = get_prefs(inter.user.id)
        colors = list(pdfmaker.COLORS)
        p["label_color"] = colors[(colors.index(p["label_color"]) + 1) % len(colors)]
        await inter.response.edit_message(view=SettingsView(inter.user.id))

    @discord.ui.button(label="Font", emoji="🔤", style=discord.ButtonStyle.secondary, row=2)
    async def font_btn(self, inter: discord.Interaction, _):
        p = get_prefs(inter.user.id)
        p["label_font"] = "serif" if p["label_font"] == "sans" else "sans"
        await inter.response.edit_message(view=SettingsView(inter.user.id))

    @discord.ui.button(label="Generate PDF", emoji="📄", style=discord.ButtonStyle.success, row=2)
    async def next_btn(self, inter: discord.Interaction, _):
        await open_name_form(inter)


class NameModal(discord.ui.Modal, title="Name your PDF"):
    def __init__(self, user_id: int):
        super().__init__()
        p = get_prefs(user_id)
        self.filename = discord.ui.TextInput(default=p["filename"], max_length=100)
        self.label = discord.ui.TextInput(
            style=discord.TextStyle.paragraph, required=False, default=p["label"] or None, max_length=300,
            placeholder="PO 25OSPC055\n@ KOMO MAI DR.\nTAKEN 4/16/26")
        self.page = _select("Page layout", PAGE_CHOICES, p["page"], None)
        self.sort = _select("Page order", SORT_CHOICES, p["sort"], None)
        self.keep = discord.ui.Checkbox()
        self.add_item(discord.ui.Label(text="File name", description=".pdf is added for you", component=self.filename))
        self.add_item(discord.ui.Label(text="Label on every photo (optional)",
                                       description="Each line becomes a line in the label box", component=self.label))
        self.add_item(discord.ui.Label(text="Page layout", component=self.page))
        self.add_item(discord.ui.Label(text="Page order", component=self.sort))
        self.add_item(discord.ui.Label(text="Keep my photos after making it", component=self.keep))

    async def on_submit(self, inter: discord.Interaction):
        s = sessions.get(inter.user.id)
        if not s or not s.photos:
            return await inter.response.send_message("Your photos are gone (cleared or expired). Upload them again.", ephemeral=True)
        if s.lock.locked():
            return await inter.response.send_message("Already working on one, hang on.", ephemeral=True)
        p = get_prefs(inter.user.id)
        p["filename"], p["label"] = self.filename.value.strip() or "photos", self.label.value
        p["page"], p["sort"] = (self.page.values or [p["page"]])[0], (self.sort.values or [p["sort"]])[0]
        orientation, fit = p["page"].split("|")
        opts = pdfmaker.Options(orientation, fit, p["size"], p["label"], p["label_position"], p["label_color"], p["label_font"])
        await inter.response.defer(thinking=True)
        await generate(inter, s, opts, p["filename"], p["sort"], bool(self.keep.value))


@pdf.command(name="panel", description="Post the PDF Compiler button panel in this channel")
async def pdf_panel(inter: discord.Interaction):
    await inter.response.send_message(embed=panel_embed(), view=PanelView())


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in .env (see .env.example)")
    client.run(TOKEN, root_logger=True)
