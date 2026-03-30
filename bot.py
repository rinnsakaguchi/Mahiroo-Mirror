import os
import asyncio
import subprocess
import requests
import psutil
import shutil
import time
import logging
from urllib.parse import urlparse, unquote
from bs4 import BeautifulSoup

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler

TOKEN = os.getenv("TELEGRAM_TOKEN")

task_queue = asyncio.Queue()
current_task = None
current_file = None
current_process = None
current_chat = None
cancel_requested = False

url_cache = {}
CACHE_EXPIRY = 300

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------------
# SYSTEM INFO
# ------------------------
def get_system_info():
    total, used, free = shutil.disk_usage("/")
    ram = psutil.virtual_memory()
    return {
        "cpu": psutil.cpu_count(),
        "ram": f"{ram.used // (1024 ** 3)}/{ram.total // (1024 ** 3)} GB",
        "disk": f"{free // (1024 ** 3)} GB free"
    }

# ------------------------
# RESOLVE DIRECT
# ------------------------
def resolve_direct(url):
    try:
        r = requests.get(url, allow_redirects=True, timeout=10, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://frboxdata.transsion.com/"
        })
        return r.url
    except:
        return url

# ------------------------
# SOURCEFORGE
# ------------------------
def get_sf_mirrors(url):
    try:
        page = requests.get(url, timeout=10)
        soup = BeautifulSoup(page.text, "html.parser")
        return [o.get("value") for o in soup.select("select#mirrorSelect option") if o.get("value")]
    except:
        return []

def build_sf_mirror(url, mirror):
    if "sourceforge.net/projects" in url:
        return url.replace("download", f"download?use_mirror={mirror}")
    return url

# ------------------------
# GOFILE UPLOAD
# ------------------------
def upload_gofile(file):
    with open(file, "rb") as f:
        r = requests.post("https://store1.gofile.io/uploadFile", files={"file": f})
    try:
        return r.json()["data"]["downloadPage"]
    except:
        return None

# ------------------------
# DOWNLOAD (PRO MODE)
# ------------------------
async def download_file(msg, url, filename):
    global current_process

    TEMP_DIR = f"{filename}_parts"
    os.makedirs(TEMP_DIR, exist_ok=True)

    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    if "transsion.com" in url:
        headers["Referer"] = "https://frboxdata.transsion.com/"

    r = requests.head(url, headers=headers, allow_redirects=True)

    if "content-length" not in r.headers:
        raise Exception("Cannot get file size")

    total_size = int(r.headers["content-length"])
    parts = 8
    chunk_size = total_size // parts

    processes = []
    current_process = processes

    cancel_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel Download", callback_data="cancel_download")
    ]])

    # Start chunks
    for i in range(parts):
        start = i * chunk_size
        end = total_size - 1 if i == parts - 1 else (start + chunk_size - 1)

        part_file = os.path.join(TEMP_DIR, f"part{i}")

        cmd = [
            "curl",
            "-L",
            "--retry", "5",
            "-H", "User-Agent: Mozilla/5.0",
        ]

        if "transsion.com" in url:
            cmd += ["-H", "Referer: https://frboxdata.transsion.com/"]

        cmd += [
            "-H", f"Range: bytes={start}-{end}",
            "-o", part_file,
            url
        ]

        p = subprocess.Popen(cmd)
        processes.append(p)

    # Monitor progress
    last_update = time.time()
    while any(p.poll() is None for p in processes):
        downloaded = sum(
            os.path.getsize(os.path.join(TEMP_DIR, f))
            for f in os.listdir(TEMP_DIR)
            if os.path.exists(os.path.join(TEMP_DIR, f))
        )

        percent = (downloaded / total_size) * 100

        if time.time() - last_update > 2:
            last_update = time.time()
            try:
                await msg.edit_text(
                    f"📥 Downloading\n`{filename}`\n\n{percent:.2f}%",
                    parse_mode="Markdown",
                    reply_markup=cancel_keyboard
                )
            except:
                pass

        await asyncio.sleep(1)

    # Check
    for p in processes:
        if p.returncode != 0:
            raise Exception("Chunk download failed")

    # Merge
    with open(filename, "wb") as outfile:
        for i in range(parts):
            with open(os.path.join(TEMP_DIR, f"part{i}"), "rb") as pf:
                outfile.write(pf.read())

    shutil.rmtree(TEMP_DIR)
    current_process = None

# ------------------------
# WORKER
# ------------------------
async def worker(app):
    global current_task, current_file, current_process, current_chat, cancel_requested

    while True:
        task = await task_queue.get()
        chat = task["chat"]
        url = task["url"]
        mirror = task.get("mirror")

        parsed = urlparse(url)
        filename = unquote(os.path.basename(parsed.path)) or f"file_{int(time.time())}"

        current_file = filename
        current_task = "Downloading"
        current_chat = chat
        cancel_requested = False

        msg = await app.bot.send_message(chat, f"📥 Start\n`{filename}`", parse_mode="Markdown")

        try:
            if mirror:
                url = build_sf_mirror(url, mirror)
            elif "transsion.com" not in url:
                url = resolve_direct(url)

            await download_file(msg, url, filename)

            if cancel_requested:
                await msg.edit_text("❌ Cancelled")
                continue

            current_task = "Uploading"

            await msg.edit_text("📤 Uploading...")

            loop = asyncio.get_event_loop()
            link = await loop.run_in_executor(None, upload_gofile, filename)

            if cancel_requested:
                await msg.edit_text("❌ Upload cancelled")
            elif link:
                await msg.edit_text(f"✅ Done\n{link}")
            else:
                await msg.edit_text("❌ Upload failed")

        except Exception as e:
            await msg.edit_text(f"❌ Error\n{e}")

        finally:
            if os.path.exists(filename):
                os.remove(filename)

            current_task = None
            current_file = None
            current_process = None
            current_chat = None
            cancel_requested = False

# ------------------------
# COMMANDS
# ------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sys = get_system_info()
    await update.message.reply_text(
        f"🤖 Ready\nCPU:{sys['cpu']}\nRAM:{sys['ram']}\nDisk:{sys['disk']}"
    )

async def mirror(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("/mirror <link>")

    url = context.args[0]
    await task_queue.put({"chat": update.effective_chat.id, "url": url})

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_process

    if isinstance(current_process, list):
        for p in current_process:
            try:
                p.terminate()
            except:
                pass

    await update.message.reply_text("❌ Cancelled")

# ------------------------
# MAIN
# ------------------------
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mirror", mirror))
    app.add_handler(CommandHandler("cancel", cancel))

    async def start_worker(app):
        asyncio.create_task(worker(app))

    app.post_init = start_worker

    app.run_polling()

if __name__ == "__main__":
    main()
