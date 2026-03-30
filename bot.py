import os
import asyncio
import subprocess
import requests
import psutil
import shutil
import time
import logging
import re
from urllib.parse import urlparse, unquote
from bs4 import BeautifulSoup

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler

# ------------------------
# Configuration & Globals
# ------------------------
TOKEN = os.getenv("TELEGRAM_TOKEN")

task_queue = asyncio.Queue()
current_task = None
current_file = None
current_process = None
current_chat = None
cancel_requested = False

url_cache = {}
CACHE_EXPIRY = 300

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------------------
# System info
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
# URL Helpers
# ------------------------
def resolve_direct(url):
    try:
        r = requests.head(url, allow_redirects=True, timeout=15)
        return r.url
    except:
        return url

def fix_oss_url(url):
    if "frboxdata.transsion.com" in url:
        return url
    return url

def get_filename(url):
    parsed = urlparse(url)
    filename = unquote(os.path.basename(parsed.path))
    if not filename or filename == "download":
        parts = parsed.path.split("/")
        filename = parts[-2] if len(parts) > 2 else f"file_{int(time.time())}"
    return filename

# ------------------------
# SourceForge
# ------------------------
def get_sf_mirrors(url):
    try:
        page = requests.get(url, timeout=15)
        soup = BeautifulSoup(page.text, "html.parser")
        mirrors = []
        for option in soup.select("select#mirrorSelect option"):
            mirror_name = option.get("value")
            if mirror_name:
                mirrors.append(mirror_name)
        return mirrors[:10]
    except:
        return []

def build_sf_mirror(url, mirror):
    if "sourceforge.net/projects" in url:
        return url.replace("download", f"download?use_mirror={mirror}")
    return url

# ------------------------
# GoFile Upload
# ------------------------
def upload_gofile(file_path):
    try:
        with open(file_path, "rb") as f:
            r = requests.post("https://store1.gofile.io/uploadFile", files={"file": f}, timeout=300)
        return r.json()["data"]["downloadPage"]
    except:
        return None

# ------------------------
# WGET Download (FINAL)
# ------------------------
async def download_file(msg, url, filename):
    global current_process
    
    cmd = [
        "wget", "-v", "--progress=bar:force:noscroll",
        "--tries=5", "--timeout=90", "--limit-rate=15m", "-c",
        "--no-check-certificate", "--no-cache",
        "--header", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "--header", "Referer: https://frboxdata.transsion.com/",
        "--header", "Accept: */*",
        "-O", filename, url
    ]
    
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, universal_newlines=True
    )
    current_process = process

    cancel_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data="cancel_download")
    ]])

    last_update = time.time()
    while process.poll() is None:
        line = process.stdout.readline().strip()
        if '%' in line:
            try:
                await msg.edit_text(
                    f"📥 `{filename}`\n\n`{line}`",
                    parse_mode="Markdown", reply_markup=cancel_keyboard
                )
            except:
                pass
            last_update = time.time()

    code = process.wait()
    current_process = None
    
    if code != 0 or not os.path.exists(filename) or os.path.getsize(filename) == 0:
        raise Exception(f"wget failed (code: {code})")
    
    size_gb = os.path.getsize(filename) / (1024**3)
    await msg.edit_text(f"✅ `{filename}`\n📦 {size_gb:.2f} GB", parse_mode="Markdown")
    return filename

# ------------------------
# Worker (FIXED)
# ------------------------
async def worker(app):
    global current_task, current_file, current_process, current_chat, cancel_requested
    while True:
        try:
            task = await task_queue.get()
            chat_id = task["chat"]
            url = task["url"]
            mirror = task.get("mirror")

            filename = get_filename(url)
            current_file = filename
            current_task = "Downloading"
            current_chat = chat_id
            cancel_requested = False
            
            msg = await app.bot.send_message(
                chat_id, f"🚀 `{filename}`", parse_mode="Markdown"
            )

            try:
                final_url = build_sf_mirror(url, mirror) if mirror else fix_oss_url(resolve_direct(url))
                await download_file(msg, final_url, filename)

                if cancel_requested:
                    await msg.edit_text("❌ Cancelled.")
                    continue

                # Upload
                current_task = "Uploading"
                await msg.edit_text("📤 Uploading...", reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel_upload")
                ]]))

                loop = asyncio.get_event_loop()
                link = await loop.run_in_executor(None, upload_gofile, filename)

                if cancel_requested:
                    await msg.edit_text("❌ Cancelled.")
                elif link:
                    await msg.edit_text(f"✅ `{link}`")
                else:
                    await msg.edit_text("❌ Upload failed.")

            except Exception as e:
                await msg.edit_text(f"❌ `{str(e)}`", parse_mode="Markdown")

            finally:
                if os.path.exists(filename):
                    os.remove(filename)
                current_task = None
                current_file = None
                current_process = None
                current_chat = None
                cancel_requested = False

        except Exception as e:
            logger.error(f"Worker error: {e}")

# ------------------------
# Commands
# ------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sys = get_system_info()
    await update.message.reply_text(
        f"🤖 **Mahiro v2.0** (wget)\n\n"
        f"CPU: {sys['cpu']} | RAM: {sys['ram']} | Disk: {sys['disk']}\n\n"
        f"`/mirror <link>` or reply link",
        parse_mode="Markdown"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    queue_size = task_queue.qsize()
    task_status = f"{current_task} `{current_file}`" if current_task else "Idle"
    await update.message.reply_text(f"📊 Queue: {queue_size}\n📂 {task_status}")

async def mirror(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = context.args[0] if context.args else None
    if not url and update.message.reply_to_message:
        url = update.message.reply_to_message.text
    
    if not url:
        await update.message.reply_text("❌ `/mirror <link>`")
        return

    cache_id = str(int(time.time() * 1000))
    url_cache[cache_id] = (url, time.time())

    if "sourceforge.net/projects" in url:
        mirrors = get_sf_mirrors(url)
        if mirrors:
            buttons = [[InlineKeyboardButton(m[:15], callback_data=f"sf|{cache_id}|{m}")] for m in mirrors[:8]]
            await update.message.reply_text("🌐 Mirrors:", reply_markup=InlineKeyboardMarkup(buttons))
            return

    await update.message.reply_text(
        "🚀 Start?",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Yes", callback_data=f"link|{cache_id}")]])
    )

# ------------------------
# Callback
# ------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_process
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("sf|"):
        _, cache_id, mirror = data.split("|", 2)
        url = url_cache.pop(cache_id, (None, None))[0]
        if url:
            await task_queue.put({"chat": query.message.chat_id, "url": url, "mirror": mirror})
            await query.edit_message_text("🌐 Starting...")

    elif data.startswith("link|"):
        cache_id = data.split("|")[1]
        url = url_cache.pop(cache_id, (None, None))[0]
        if url:
            await task_queue.put({"chat": query.message.chat_id, "url": url})
            await query.edit_message_text("🚀 Mirroring...")

    elif data == "cancel_download" and current_process:
        current_process.terminate()
        await query.edit_message_text("❌ Cancelled.")
    elif data == "cancel_upload":
        cancel_requested = True
        await query.edit_message_text("❌ Cancelled.")

# ------------------------
# MAIN (FIXED EVENT LOOP)
# ------------------------
async def main_async():
    """Fixed async main"""
    if not shutil.which("wget"):
        print("❌ Install wget!")
        return

    app = ApplicationBuilder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("mirror", mirror))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    # Start worker CORRECTLY
    asyncio.create_task(worker(app))
    
    print("🤖 Bot running... (Fixed!)")
    await app.run_polling(drop_pending_updates=True)

def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\n👋 Bot stopped.")

if __name__ == "__main__":
    main()
