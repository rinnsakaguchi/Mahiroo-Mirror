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
# Globals
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------------
# Utils
# ------------------------
def get_system_info():
    total, used, free = shutil.disk_usage("/")
    ram = psutil.virtual_memory()
    return f"CPU: {psutil.cpu_count()} | RAM: {ram.used//10**9}/{ram.total//10**9}GB | Disk: {free//10**9}GB free"

def get_filename(url):
    parsed = urlparse(url)
    filename = unquote(os.path.basename(parsed.path))
    if not filename or filename == "download":
        filename = f"file_{int(time.time())}"
    return filename

def resolve_direct(url):
    try:
        r = requests.head(url, allow_redirects=True, timeout=10)
        return r.url
    except:
        return url

# ------------------------
# Download (WGET)
# ------------------------
async def download_file(msg, url, filename):
    global current_process
    
    cmd = [
        "wget", "-q", "--show-progress", "--progress=bar:force",
        "--tries=3", "--timeout=60", "-c",
        "--user-agent=Mozilla/5.0", "--referer=https://frboxdata.transsion.com/",
        "-O", filename, url
    ]
    
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    current_process = process

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])

    while process.poll() is None:
        line = process.stdout.readline()
        if '%' in line:
            try:
                await msg.edit_text(f"📥 `{filename}`\n\n`{line.strip()}`", 
                                  parse_mode='Markdown', reply_markup=kb)
            except:
                pass

    current_process = None
    if process.returncode != 0 or not os.path.exists(filename):
        raise Exception("Download failed")
    
    size = os.path.getsize(filename) / 10**9
    await msg.edit_text(f"✅ `{filename}`\n📦 {size:.1f} GB", parse_mode='Markdown')

# ------------------------
# Worker
# ------------------------
async def worker(app):
    global current_task, current_file, current_chat, cancel_requested
    while True:
        task = await task_queue.get()
        chat_id = task["chat"]
        url = task["url"]
        
        filename = get_filename(url)
        current_file = filename
        current_chat = chat_id
        cancel_requested = False
        
        try:
            msg = await app.bot.send_message(chat_id, f"🚀 `{filename}`", parse_mode='Markdown')
            await download_file(msg, resolve_direct(url), filename)
            
            if cancel_requested: 
                await msg.edit_text("❌ Cancelled")
                continue
            
            # Upload
            await msg.edit_text("📤 Uploading...")
            import concurrent.futures
            loop = asyncio.get_event_loop()
            
            def upload():
                try:
                    with open(filename, 'rb') as f:
                        r = requests.post("https://store1.gofile.io/uploadFile", files={'file': f})
                    return r.json()['data']['downloadPage']
                except:
                    return None
            
            link = await loop.run_in_executor(None, upload)
            
            if link:
                await msg.edit_text(f"✅ `{link}`")
            else:
                await msg.edit_text("❌ Upload failed")
                
        except Exception as e:
            await app.bot.send_message(chat_id, f"❌ {e}")
        
        finally:
            if os.path.exists(filename):
                os.remove(filename)
            current_task = None
            current_file = None
            current_chat = None

# ------------------------
# Handlers
# ------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🤖 **Mahiro Bot** (wget)\n\n{get_system_info()}\n\n"
        f"`/mirror <url>` or reply to link",
        parse_mode='Markdown'
    )

async def mirror(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = context.args[0] if context.args else None
    if not url and update.message.reply_to_message:
        url = update.message.reply_to_message.text
    
    if not url:
        await update.message.reply_text("❌ Send `/mirror <url>`")
        return
    
    await task_queue.put({"chat": update.effective_chat.id, "url": url})
    await update.message.reply_text("🚀 Started!")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = f"Task: {current_task or 'Idle'}\nFile: `{current_file or 'None'}`\nQueue: {task_queue.qsize()}"
    await update.message.reply_text(status, parse_mode='Markdown')

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_process, cancel_requested
    q = update.callback_query
    await q.answer()
    
    if q.data == "cancel":
        if current_process:
            current_process.terminate()
        cancel_requested = True
        await q.edit_message_text("❌ Cancelled")

# ------------------------
# MAIN - BULLETPROOF
# ------------------------
def main():
    if not TOKEN:
        print("❌ Set TELEGRAM_TOKEN")
        return
    if not shutil.which("wget"):
        print("❌ Install wget")
        return

    app = ApplicationBuilder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mirror", mirror))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(button, pattern="^cancel$"))
    
    # ✅ CORRECT WORKER START
    async def post_init(application):
        await worker(application)
    
    app.post_init = post_init
    
    print("🤖 Mahiro Bot Started! ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
