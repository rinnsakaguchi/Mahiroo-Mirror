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
    """Fix Transsion/OSS URLs"""
    if "frboxdata.transsion.com" in url:
        return url
    if "oss" in url.lower() or "aliyuncs" in url:
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
        return mirrors[:10]  # Limit to 10 fastest
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
# WGET Download (FINAL VERSION)
# ------------------------
async def download_file(msg, url, filename):
    global current_process
    
    # Ultimate wget command
    cmd = [
        "wget",
        "-v",                                    # Verbose
        "--progress=bar:force:noscroll",         # Sexy progress bar
        "--tries=5",                             # Retry 5x
        "--timeout=90",                          # 90s timeout
        "--limit-rate=15m",                      # 15MB/s max
        "-c",                                    # Continue/Resume
        "--no-check-certificate",                # Skip SSL
        "--no-cache",                            # No cache
        "--header", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "--header", "Accept: */*",
        "--header", "Accept-Language: en-US,en;q=0.9",
        "--header", "Accept-Encoding: gzip, deflate, br",
        "--header", "Connection: keep-alive",
        "--header", "Referer: https://frboxdata.transsion.com/",
        "-O", filename,
        url
    ]
    
    process = subprocess.Popen(
        cmd, 
        stdout=subprocess.PIPE, 
        stderr=subprocess.STDOUT, 
        text=True,
        bufsize=1,
        universal_newlines=True
    )
    current_process = process

    cancel_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel Download", callback_data="cancel_download")
    ]])

    last_update = time.time()
    last_progress = ""
    error_detected = False
    
    while process.poll() is None:
        line = process.stdout.readline().strip()
        if not line:
            continue
            
        logger.info(f"WGET: {line}")
        
        # Error detection
        error_keywords = ['error', 'failed', '403 forbidden', '404 not found', '401 unauthorized']
        if any(keyword in line.lower() for keyword in error_keywords):
            error_detected = True
            logger.error(f"Error detected: {line}")
        
        # Progress parsing (enhanced)
        if '%' in line and ('[' in line or 'MB' in line):
            percent_match = re.search(r'(\d+(?:\.\d+)?)%', line)
            speed_match = re.search(r'([0-9.]+[KMG]?B/s)', line)
            size_match = re.search(r'([0-9.]+[KMG]?B)', line)
            
            if percent_match:
                progress = f"[{percent_match.group(1)}%] "
                if speed_match:
                    progress += f"⚡ {speed_match.group(1)} "
                if size_match:
                    progress += size_match.group(1)
                
                if progress != last_progress and time.time() - last_update > 1.5:
                    last_progress = progress
                    last_update = time.time()
                    try:
                        await msg.edit_text(
                            f"📥 Downloading...\n"
                            f"`{filename}`\n\n"
                            f"{progress}",
                            parse_mode="Markdown",
                            reply_markup=cancel_keyboard
                        )
                    except Exception as e:
                        logger.error(f"Edit message failed: {e}")

    code = process.wait()
    current_process = None
    
    # Comprehensive file validation
    if not os.path.exists(filename):
        raise Exception("File not created")
    
    file_size = os.path.getsize(filename)
    if file_size == 0:
        raise Exception("File is empty (0 bytes)")
    
    if file_size < 512:  # Less than 512 bytes
        raise Exception(f"File too small: {file_size} bytes")
    
    file_size_gb = file_size / (1024**3)
    file_size_mb = file_size / (1024**2)
    
    await msg.edit_text(
        f"✅ Download Complete!\n"
        f"`{filename}`\n"
        f"📦 {file_size_gb:.2f} GB ({file_size_mb:.1f} MB)",
        parse_mode="Markdown"
    )
    
    return filename

# ------------------------
# Worker (FINAL)
# ------------------------
async def worker(app):
    global current_task, current_file, current_process, current_chat, cancel_requested
    while True:
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
            chat_id, 
            f"🚀 Starting mirror...\n`{filename}`", 
            parse_mode="Markdown"
        )

        try:
            # Process URL
            if mirror:
                final_url = build_sf_mirror(url, mirror)
            else:
                final_url = fix_oss_url(resolve_direct(url))
            
            logger.info(f"Downloading: {final_url}")
            
            # Download
            await download_file(msg, final_url, filename)

            if cancel_requested:
                await msg.edit_text("❌ Operation cancelled by user.")
                continue

            # Upload
            current_task = "Uploading"
            upload_keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel Upload", callback_data="cancel_upload")
            ]])
            
            await msg.edit_text(
                "📤 Uploading to GoFile...\n"
                f"`{filename}`\n"
                "(Click Cancel to discard file)", 
                parse_mode="Markdown",
                reply_markup=upload_keyboard
            )

            loop = asyncio.get_event_loop()
            link = await loop.run_in_executor(None, upload_gofile, filename)

            if cancel_requested:
                await msg.edit_text("❌ Upload cancelled.")
            elif link:
                await msg.edit_text(
                    f"✅ **Mirror Complete!**\n\n"
                    f"🔗 `{link}`\n\n"
                    f"💾 `{filename}`",
                    parse_mode="Markdown"
                )
            else:
                await msg.edit_text("❌ GoFile upload failed. File discarded.")

        except subprocess.TimeoutExpired:
            await msg.edit_text("⏰ Download timeout.")
        except Exception as e:
            error_msg = f"❌ Error\n`{str(e)}`"
            await msg.edit_text(error_msg, parse_mode="Markdown")
            logger.error(f"Download error: {e}")

        finally:
            # Cleanup
            if os.path.exists(filename):
                try:
                    os.remove(filename)
                except:
                    pass
            current_task = None
            current_file = None
            current_process = None
            current_chat = None
            cancel_requested = False

# ------------------------
# Commands
# ------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sys = get_system_info()
    await update.message.reply_text(
        f"🤖 **Mahiro Mirror Bot** (wget v2.0)\n\n"
        f"💻 CPU: {sys['cpu']} cores\n"
        f"🧠 RAM: {sys['ram']}\n"
        f"💾 Disk: {sys['disk']}\n\n"
        f"📋 **Commands:**\n"
        f"`/mirror <link>` - Start mirroring\n"
        f"`/status` - Bot status\n"
        f"`Reply link` - Quick mirror",
        parse_mode="Markdown"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sys = get_system_info()
    queue_size = task_queue.qsize()
    task_status = f"{current_task}\n`{current_file}`" if current_task else "🟢 Idle"
    
    await update.message.reply_text(
        f"📊 **Bot Status**\n\n"
        f"💻 CPU: {sys['cpu']} cores\n"
        f"🧠 RAM: {sys['ram']}\n"
        f"💾 Disk: {sys['disk']}\n\n"
        f"📂 Current: {task_status}\n"
        f"📋 Queue: {queue_size}",
        parse_mode="Markdown"
    )

async def mirror(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        url = context.args[0]
    elif update.message.reply_to_message and update.message.reply_to_message.text:
        url = update.message.reply_to_message.text
    else:
        await update.message.reply_text("❌ **Usage:**\n`/mirror <link>`\n**or** reply to link", parse_mode="Markdown")
        return

    cache_id = str(int(time.time() * 1000))
    url_cache[cache_id] = (url, time.time())

    # Cleanup expired cache
    now = time.time()
    expired = [cid for cid, (_, ts) in url_cache.items() if now - ts > CACHE_EXPIRY]
    for cid in expired:
        del url_cache[cid]

    # SourceForge auto-detection
    if "sourceforge.net/projects" in url:
        mirrors = get_sf_mirrors(url)
        if mirrors:
            if len(mirrors) == 1:
                await task_queue.put({"chat": update.effective_chat.id, "url": url, "mirror": mirrors[0]})
                await update.message.reply_text(f"🌐 Auto-selected: `{mirrors[0]}`", parse_mode="Markdown")
                return
            
            # Mirror selection buttons
            buttons = []
            for i, m in enumerate(mirrors[:10]):
                row = [InlineKeyboardButton(m[:20], callback_data=f"sf|{cache_id}|{m}")]
                buttons.append(row)
            
            await update.message.reply_text(
                "🌐 **Choose SourceForge Mirror:**",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="Markdown"
            )
            return

    # Default mirror button
    buttons = [[
        InlineKeyboardButton("🚀 Start Mirror", callback_data=f"link|{cache_id}"),
        InlineKeyboardButton("⏭ Skip", callback_data="skip")
    ]]
    
    await update.message.reply_text(
        f"🔗 **Detected:** `{url[:50]}...`\n\n"
        f"👇 Choose action:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )

# ------------------------
# Callback Handler
# ------------------------
async def mirror_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_process, current_chat
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("sf|"):
        _, cache_id, mirror = data.split("|", 2)
        url_info = url_cache.get(cache_id)
        if url_info:
            url, _ = url_info
            await query.message.edit_text(f"🌐 **Mirror:** `{mirror}`\n🚀 Starting...", parse_mode="Markdown")
            await task_queue.put({"chat": query.message.chat_id, "url": url, "mirror": mirror})
        del url_cache[cache_id]

    elif data.startswith("link|"):
        _, cache_id = data.split("|")
        url_info = url_cache.get(cache_id)
        if url_info:
            url, _ = url_info
            await query.message.edit_text("🚀 **Mirroring started...**")
            await task_queue.put({"chat": query.message.chat_id, "url": url})
        del url_cache[cache_id]

    elif data == "cancel_download":
        if current_chat == query.message.chat_id and current_process:
            current_process.terminate()
            await query.edit_message_text("❌ **Download cancelled.**")
        else:
            await query.answer("No active download.")

    elif data == "cancel_upload":
        if current_chat == query.message.chat_id:
            cancel_requested = True
            await query.edit_message_text("❌ **Upload cancelled.**")
        else:
            await query.answer("No active upload.")

    elif data == "skip":
        await query.edit_message_text("⏭ **Skipped.**")

# ------------------------
# MAIN
# ------------------------
def main():
    if not shutil.which("wget"):
        logger.error("❌ wget not found! Install: sudo apt install wget")
        return

    app = ApplicationBuilder().token(TOKEN).build()
    
    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler(["mirror", "m"], mirror))
    app.add_handler(CallbackQueryHandler(mirror_select))

    # Start worker
    async def start_worker(app):
        await worker(app)

    app.post_init = lambda app: asyncio.create_task(start_worker(app))
    
    logger.info("🎉 Mahiro Mirror Bot (wget) STARTED!")
    print("🤖 Bot running... Press Ctrl+C to stop")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
