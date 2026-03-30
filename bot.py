import os
import asyncio
import requests
import psutil
import shutil
import time
import logging
import re
from urllib.parse import urlparse, unquote
from bs4 import BeautifulSoup

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, 
    CommandHandler, 
    ContextTypes, 
    CallbackQueryHandler, 
    MessageHandler, 
    filters
)

# Config
TOKEN = os.getenv("TELEGRAM_TOKEN")
task_queue = asyncio.Queue()
current_process = None

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Utils ---
def get_system_info():
    ram = psutil.virtual_memory()
    disk = shutil.disk_usage("/")
    return f"CPU: {psutil.cpu_count()} | RAM: {ram.percent}% | Disk: {disk.free//10**9}GB"

def get_filename(url):
    """Clean filename to prevent path injection/wget errors."""
    parsed = urlparse(url)
    # Get just the last part of the path
    raw_path = unquote(os.path.basename(parsed.path)).strip()
    
    # Remove slashes and backslashes that wget interprets as directories
    filename = raw_path.replace("/", "_").replace("\\", "_")
    
    # Remove other problematic characters for Linux/Windows filesystems
    filename = re.sub(r'[<>:"|?*]', '', filename)
    
    if not filename or filename in ['download', '', '_']:
        filename = f"file_{int(time.time())}.zip"
    
    # Truncate extremely long filenames (max 100 chars) to avoid OS errors
    if len(filename) > 100:
        ext = os.path.splitext(filename)[1]
        filename = filename[:90] + ext
        
    return filename

def get_sf_mirrors(url):
    try:
        r = requests.get(url, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        mirrors = [opt.get('value') for opt in soup.select("select#mirrorSelect option") if opt.get('value')]
        return mirrors[:8]
    except Exception as e:
        logger.error(f"SF Mirror Error: {e}")
        return []

# --- Download Core ---
async def download_file(application, chat_id: int, url: str, filename: str):
    global current_process
    
    logger.info(f"Starting wget for: {filename}")
    
    cmd = [
        "wget", "--no-verbose", "--show-progress", "--progress=bar:force:noscroll",
        "--tries=3", "--timeout=30", "--limit-rate=50m", "-c",
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "--referer=https://frboxdata.transsion.com/",
        "-O", filename, url
    ]
    
    process = await asyncio.create_subprocess_exec(
        *cmd, 
        stdout=asyncio.subprocess.PIPE, 
        stderr=asyncio.subprocess.STDOUT
    )
    current_process = process

    msg = await application.bot.send_message(chat_id, f"📥 **Preparing:** `{filename}`", parse_mode='Markdown')
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])
    
    try:
        last_edit_time = 0
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            
            line_str = line.decode('utf-8', errors='ignore').strip()
            
            # Edit every 3 seconds to stay safe from Telegram's Rate Limits
            if '%' in line_str and (time.time() - last_edit_time > 3.0):
                try:
                    await msg.edit_text(f"📥 `{filename}`\n\n`{line_str}`", parse_mode='Markdown', reply_markup=kb)
                    last_edit_time = time.time()
                except Exception:
                    pass
        rc = await process.wait()
    finally:
        current_process = None

    if rc != 0:
        if os.path.exists(filename): os.remove(filename)
        raise Exception(f"Wget exited with code {rc}")
    
    if not os.path.exists(filename) or os.path.getsize(filename) == 0:
        raise Exception("File is empty or was not created.")
    
    size_gb = os.path.getsize(filename) / (1024**3)
    await msg.edit_text(f"✅ `{filename}`\n📦 **{size_gb:.2f} GB**", parse_mode='Markdown')

# --- Worker Logic ---
async def worker(application):
    logger.info("🟢 Worker online and waiting for tasks")
    while True:
        task = await task_queue.get()
        chat_id = task["chat"]
        url = task["url"]
        filename = get_filename(url)
        
        try:
            await download_file(application, chat_id, url, filename)
            
            # Uploading
            status_msg = await application.bot.send_message(chat_id, "📤 **Uploading to GoFile...**", parse_mode='Markdown')
            
            def upload():
                try:
                    srv = requests.get("https://api.gofile.io/getServer", timeout=10).json()
                    server = srv['data']['server']
                    with open(filename, 'rb') as f:
                        r = requests.post(f"https://{server}.gofile.io/uploadFile", files={'file': f}, timeout=600)
                    return r.json()['data']['downloadPage']
                except Exception as e:
                    logger.error(f"Upload logic failed: {e}")
                    return None
            
            link = await asyncio.get_event_loop().run_in_executor(None, upload)
            
            if link:
                await status_msg.edit_text(f"✅ **Link Ready:**\n{link}", disable_web_page_preview=True)
            else:
                await status_msg.edit_text("❌ **Upload failed.** Storage server error.")
            
        except Exception as e:
            logger.error(f"Worker Error: {e}")
            await application.bot.send_message(chat_id, f"❌ **Error:** `{str(e)}`", parse_mode='Markdown')
        finally:
            if os.path.exists(filename):
                os.remove(filename)
            task_queue.task_done()

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    await update.message.reply_text(
        f"🤖 **Mahiro Bot v3.1**\n\n{get_system_info()}\n\n"
        f"💡 Paste any link to mirror it.\n"
        f"💡 `/mirror <url>`\n"
        f"📊 `/status`",
        parse_mode='Markdown'
    )

async def mirror(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    
    url = context.args[0] if context.args else None
    if not url and update.message.reply_to_message and update.message.reply_to_message.text:
        url = update.message.reply_to_message.text
    
    if not url or not url.startswith('http'):
        await update.message.reply_text("❌ **Send a valid HTTP link!**")
        return
    
    context.user_data['last_url'] = url
    
    if "sourceforge.net/projects" in url:
        mirrors = get_sf_mirrors(url)
        if mirrors:
            kb = [[InlineKeyboardButton(m[:25], callback_data=f"sf:{m}")] for m in mirrors]
            await update.message.reply_text("🌐 **Select SF Mirror:**", reply_markup=InlineKeyboardMarkup(kb))
            return
    
    await task_queue.put({"chat": update.effective_chat.id, "url": url})
    await update.message.reply_text(f"🚀 **Added to Queue** (#{task_queue.qsize()})")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    await update.message.reply_text(f"📊 **Status**\nQueue: `{task_queue.qsize()}`\n{get_system_info()}", parse_mode='Markdown')

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "cancel" and current_process:
        try:
            current_process.terminate()
            await query.edit_message_text("❌ **Download Cancelled.**")
        except: pass
    
    elif query.data.startswith("sf:"):
        mirror_name = query.data[3:]
        original_url = context.user_data.get('last_url')
        if original_url:
            final_url = f"{original_url}?use_mirror={mirror_name}"
            await task_queue.put({"chat": query.message.chat_id, "url": final_url})
            await query.edit_message_text("🌐 **Mirror Selected!** Task queued.")
        else:
            await query.edit_message_text("❌ Link expired. Please send again.")

async def auto_mirror(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # FIX: Safety check for None updates
    if not update.message or not update.message.text:
        return
        
    text = update.message.text
    urls = re.findall(r'http[s]?://[^\s<>"]+', text)
    if urls:
        context.args = [urls[0]]
        await mirror(update, context)

async def post_init(application):
    asyncio.create_task(worker(application))

def main():
    if not TOKEN:
        print("❌ ERROR: TELEGRAM_TOKEN environment variable is missing!")
        return

    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mirror", mirror))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, auto_mirror))
    
    print("🚀 Mahiro Bot is running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
