# PDF Compiler Bot

Send photos to the bot in Discord and get back one letter-size PDF. This is the `photos_to_pdf.html` tool rebuilt as a bot, so you can use it from any device.

- Takes JPG, PNG, **HEIC/HEIF**, WebP, AVIF, TIFF, BMP and GIF, fixes rotation, and puts transparent images on a white background
- Orientation: portrait or landscape. Fit: fit, fill or stretch. Size: small, balanced or original
- Can sort by date taken. It reads the **Timemark** overlay with OCR, then EXIF, then falls back to upload time
- Can stamp a label on every photo, with a choice of position, color and font (Arial or Times)
- Stays under Discord's upload limit: if the PDF is too big it drops to a smaller size, then splits it into parts if it's still too big
- **Saves nothing to disk.** Each photo is downloaded into memory as soon as you add it and shrunk to a page right away (the original is dropped), so the PDF is ready the moment you name it.

## Using it

### Button panel (easiest)

Run **`/pdf panel`** once in a channel. It posts a panel with five buttons that anyone in the channel can use:

| Button | What it does |
|---|---|
| 📤 **Add Photos** | Turns on collecting in that channel. Drag or paste your photos into the chat (fastest). There's also a **Use upload form instead** button |
| 🗂️ **My Photos** | Shows your photos in page order, with **Remove…** and **Move…** |
| 📄 **Generate PDF** | Opens one form: file name, label text (multi-line), page layout, page order, keep photos. Submit and the PDF is built |
| ⚙️ **Options** | Quality, label position, label color and font. Set quality *before* adding photos so they're pre-processed at it |
| 🗑️ **Clear** | Starts over |

Panels posted by an older version update themselves to the new buttons the first time someone clicks one.

Each person has their own pile, so several people can use the same panel. Your last choices are remembered.

### Commands

| | |
|---|---|
| **DM the bot photos** | They're collected automatically, up to 10 per message, across as many messages as you want |
| `/pdf start` in a channel | Collect the photos *you* post in that channel |
| `/pdf stop` | Stops collecting there and opens the name form (same as **Generate PDF**), then makes the PDF |
| Long-press a message → **Apps → Add photos to PDF** | Add photos someone else posted |
| `/pdf list` · `/pdf move` · `/pdf remove 2,5-7` | Check and change the page order |
| `/pdf make` | Build the PDF. Every option is optional. For a label use `PO 25OSPC055 \| @ KOMO MAI DR. \| TAKEN 4/16/26` (`\|` = new line) |
| `/pdf clear` | Start over (this also happens automatically after `make` unless `keep: True`) |

Each person has their own pile. A pile is forgotten after 12 idle hours or when the bot restarts. If you change the quality to a higher setting after adding photos, or the pre-processing memory (`CACHE_MB`) is full, the bot downloads those photos again when building, so don't delete the photo messages until you have your PDF.

## One-time Discord setup (≈5 min)

1. Go to https://discord.com/developers/applications → **New Application**.
2. **Bot** tab → **Reset Token** → copy the token.
3. **Bot** tab → turn on **Message Content Intent** → Save. The bot can't see photos in normal messages without it.
4. **OAuth2 → URL Generator**: check `bot` + `applications.commands`. Under bot permissions check *Send Messages, Read Message History, Attach Files, View Channels*. Open the generated URL and add the bot to your server.
5. (Optional) To get your server ID for instant command sync: Discord Settings → Advanced → Developer Mode, then right-click the server → Copy Server ID.

## Run it on the Windows VM

The bot runs on whatever machine you start it on. It has to stay on for the bot to answer, so the VM needs to keep running.

1. On the VM, download the newest `pdf-compiler-bot-vX.Y.Z.zip` from [Releases](https://github.com/DOM-LAB-X/pdf-compiler-bot/releases/latest) and extract it, e.g. to `C:\pdf-compiler-bot`.
2. Open PowerShell and install Python, plus Tesseract (Tesseract is optional; it's only used to read Timemark dates):
   ```powershell
   winget install Python.Python.3.13
   winget install UB-Mannheim.TesseractOCR
   ```
3. Double-click **`setup.bat`**. It installs the packages and opens `.env` in Notepad. Paste your `DISCORD_TOKEN` (and `GUILD_ID`), then save.
4. Double-click **`run.bat`** to test it. Leave the window open and try DMing the bot a photo.
5. When it works, close that window, right-click **`install-autostart.bat`** and choose **Run as administrator**. The bot now runs in the background every time the VM boots, with no login needed, and restarts itself if it crashes.

**`stop.bat`** (run as admin) stops it. To remove auto-start: `schtasks /delete /tn "PdfCompilerBot" /f`.

## Updating

On the VM, right-click **`update.bat`** → **Run as administrator**. It downloads the newest release, keeps your `.env`, installs any new packages, and restarts the bot.

**Publishing a new version** (from the dev machine): change the number in `VERSION`, commit, then push a matching tag:

```bash
git commit -am "Describe the change" && git tag v1.0.1 && git push && git push --tags
```

GitHub Actions builds the zip and publishes the release.

## Notes

- **Upload limit:** bots can usually upload 10 MB per file (more in boosted servers). A batch over that is shrunk first, then split into `name_part1of3.pdf`, and so on.
- **Dates:** Discord can strip EXIF data from uploaded images, so the reliable date source is the Timemark overlay burned into the photo. Photos with no readable date are sorted by when you sent them.
- **Memory:** pre-processed pages are held in RAM until the PDF is sent, about 0.1–0.3 MB per photo at balanced and 2–3 MB at original. `CACHE_MB` (default 1000) caps it across everyone. Give the VM 2 GB or more.
- `pdfmaker.py` has no Discord code in it, so you can reuse it in other scripts.
