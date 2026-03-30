import os
import asyncio
import subprocess
import requests
import psutil
import shutil
import time
import logging
from urllib.parse import urlparse, unquote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.getenv("TELEGRAM_TOKEN")

task_queue = asyncio.Queue()
current_process = None
current_file = None
current_chat = None

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
# RESOLVE DIRECT (SAFE)
# ------------------------
def resolve_direct(url):
    try:
        r = requests.get(
            url,
            allow_redirects=True,
            timeout=10,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://frboxdata.transsion.com/"
            }
        )
        return r.url
    except:
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
# DOWNLOAD (CURL STABLE)
# ------------------------
async def download_file(msg, url, filename):
    global current_process

    cmd = [
        "curl",
        "-L",
        "--retry", "5",
        "--retry-delay", "3",
        "-o", filename,
        "-H", "User-Agent: Mozilla/5.0",
        "-H", "Accept: */*"
    ]

    # khusus transsion
    if "transsion.com" in url:
        cmd += ["-H", "Referer: https://frboxdata.transsion.com/"]

    cmd.append(url)

    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    current_process = process

    cancel_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data="cancel")
    ]])

    # simple progress loop
    last_update = time.time()
    while process.poll() is None:
        if time.time() - last_update > 3:
            last_update = time.time()
            try:
                await msg.edit_text(
                    f"📥 Downloading...\n`{filename}`",
                    parse_mode="Markdown",
                    reply_markup=cancel_keyboard
                )
            except:
                pass
        await asyncio.sleep(1)

    code = process.wait()

    if code != 0 or not os.path.exists(filename):
        raise Exception("Download failed")

    # VALIDASI HTML (biar gak keupload error page)
    with open(filename, "rb") as f:
        head = f.read(300).lower()
        if b"<html" in head or b"<!doctype" in head:
            os.remove(filename)
            raise Exception("Link expired / forbidden (HTML detected)")

    current_process = None

# ------------------------
# WORKER
# ------------------------
async def worker(app):
    global current_process, current_file, current_chat

    while True:
        task = await task_queue.get()
        chat = task["chat"]
        url = task["url"]

        parsed = urlparse(url)
        filename = unquote(os.path.basename(parsed.path)) or f"file_{int(time.time())}"

        current_file = filename
        current_chat = chat

        msg = await app.bot.send_message(chat, f"📥 Start\n`{filename}`", parse_mode="Markdown")

        try:
            if "transsion.com" not in url:
                url = resolve_direct(url)

            await download_file(msg, url, filename)

            await msg.edit_text("📤 Uploading...")

            loop = asyncio.get_event_loop()
            link = await loop.run_in_executor(None, upload_gofile, filename)

            if link:
                await msg.edit_text(f"✅ Done\n{link}")
            else:
                await msg.edit_text("❌ Upload failed")

        except Exception as e:
            await msg.edit_text(f"❌ Error\n{e}")

        finally:
            if os.path.exists(filename):
                os.remove(filename)

            current_process = None
            current_file = None
            current_chat = None

# ------------------------
# COMMANDS
# ------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sys = get_system_info()
    await update.message.reply_text(
        f"🤖 Mahiro Bot Ready\n\n"
        f"CPU: {sys['cpu']}\n"
        f"RAM: {sys['ram']}\n"
        f"Disk: {sys['disk']}\n\n"
        "/mirror <link>"
    )

async def mirror(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("/mirror <link>")

    url = context.args[0]
    await task_queue.put({"chat": update.effective_chat.id, "url": url})

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_process

    if current_process:
        try:
            current_process.terminate()
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

    logger.info("BOT STARTED")
    app.run_polling()

if __name__ == "__main__":
    main()
