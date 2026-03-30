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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters

TOKEN = os.getenv("TELEGRAM_TOKEN")
task_queue = asyncio.Queue()
current_task = None
current_file = None
current_process = None
current_chat = None
cancel_requested = False
url_cache = {}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_system_info():
    ram = psutil.virtual_memory()
    disk = shutil.disk_usage("/")
    return f"CPU: {psutil.cpu_count()} | RAM: {ram.percent}% | Disk: {disk.free//10**9}GB"

def get_filename(url):
    parsed = urlparse(url)
    filename = unquote(os.path.basename(parsed.path)).strip()
    if not filename or filename in ['download', '', '/']:
        filename = f"file_{int(time.time())}"
    return filename

def get_sf_mirrors(url):
    try:
        r = requests.get(url, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        mirrors = [opt.get('value') for opt in soup.select("select#mirrorSelect option") if opt.get('value')]
        logger.info(f"Found {len(mirrors)} SF mirrors")
        return mirrors[:8]
    except:
        return []

# ------------------------
# DOWNLOAD
# ------------------------
async def download_file(application, chat_id, url, filename):
    global current_process
    
    logger.info(f"Starting wget: {url[:100]}...")
    
    cmd = [
        "wget", "--no-verbose", "--show-progress", "--progress=bar:force:noscroll",
        "--tries=3", "--timeout=60", "--limit-rate=10m", "-c",
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "--referer=https://frboxdata.transsion.com/",
        "-O", filename, url
    ]
    
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    current_process = process

    msg = await application.bot.send_message(chat_id, f"📥 `{filename}`", parse_mode='Markdown')
    
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])
    
    try:
        while process.poll() is None:
            line = process.stdout.readline()
            if '%' in line and 'B/s' in line:
                try:
                    await msg.edit_text(f"📥 `{filename}`\n\n`{line.strip()}`", 
                                      parse_mode='Markdown', reply_markup=kb)
                except:
                    pass
        await process.wait()
    finally:
        current_process = None

    if not os.path.exists(filename) or os.path.getsize(filename) == 0:
        raise Exception("Download failed - empty file")
    
    size_gb = os.path.getsize(filename) / (1024**3)
    await msg.edit_text(f"✅ `{filename}`\n📦 **{size_gb:.2f} GB**", parse_mode='Markdown')

# ------------------------
# WORKER - FIXED
# ------------------------
async def worker(application):
    logger.info("🟢 Worker started!")
    while True:
        try:
            logger.info("Worker waiting for task...")
            task = await task_queue.get()
            logger.info(f"Got task: {task}")
            
            chat_id = task["chat"]
            url = task["url"]
            filename = get_filename(url)
            
            await application.bot.send_message(chat_id, f"🚀 **{filename}**", parse_mode='Markdown')
            
            try:
                await download_file(application, chat_id, url, filename)
                
                # Upload to GoFile
                await application.bot.send_message(chat_id, "📤 Uploading...")
                
                def upload_file():
                    try:
                        with open(filename, 'rb') as f:
                            r = requests.post("https://store1.gofile.io/uploadFile", 
                                            files={'file': f}, timeout=300)
                        data = r.json()
                        return data['data']['downloadPage'] if 'data' in data else None
                    except Exception as e:
                        logger.error(f"Upload error: {e}")
                        return None
                
                loop = asyncio.get_event_loop()
                link = await loop.run_in_executor(None, upload_file)
                
                if link:
                    await application.bot.send_message(chat_id, f"✅ **{link}**", parse_mode='Markdown')
                else:
                    await application.bot.send_message(chat_id, "❌ Upload failed")
                    
            except Exception as e:
                logger.error(f"Task error: {e}")
                await application.bot.send_message(chat_id, f"❌ **{str(e)}**", parse_mode='Markdown')
            
            # Cleanup
            if os.path.exists(filename):
                os.remove(filename)
                
        except Exception as e:
            logger.error(f"Worker crashed: {e}")
            await asyncio.sleep(5)

# ------------------------
# COMMANDS
# ------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🤖 **Mahiro Mirror Bot**\n\n"
        f"{get_system_info()}\n\n"
        f"💡 `/mirror <url>`\n"
        f"💡 Reply ke link\n"
        f"📊 `/status`",
        parse_mode='Markdown'
    )

async def mirror(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Get URL from args or reply
    url = None
    if context.args:
        url = context.args[0]
    elif update.message.reply_to_message and update.message.reply_to_message.text:
        url = update.message.reply_to_message.text
    
    if not url:
        await update.message.reply_text("❌ **Kirim URL!**\n`/mirror <link>`", parse_mode='Markdown')
        return
    
    # SourceForge detection
    if "sourceforge.net/projects" in url:
        mirrors = get_sf_mirrors(url)
        if mirrors:
            kb = [[InlineKeyboardButton(m[:20], callback_data=f"sf:{m}")] for m in mirrors]
            await update.message.reply_text(
                "🌐 **Pilih Mirror SF:**",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode='Markdown'
            )
            return
    
    # Add to queue
    await task_queue.put({"chat": update.effective_chat.id, "url": url})
    await update.message.reply_text("🚀 **Ditambahkan ke queue!**", parse_mode='Markdown')

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    qsize = task_queue.qsize()
    status = f"📊 **Status:**\nQueue: `{qsize}`\nTask: `{current_file or 'None'}`\n{ get_system_info() }"
    await update.message.reply_text(status, parse_mode='Markdown')

# ------------------------
# BUTTONS & MESSAGES
# ------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "cancel" and current_process:
        current_process.terminate()
        await query.edit_message_text("❌ **Dibatalkan!**")
    
    elif query.data.startswith("sf:"):
        mirror = query.data[3:]
        await task_queue.put({
            "chat": query.message.chat_id, 
            "url": query.message.text + f"?use_mirror={mirror}"
        })
        await query.edit_message_text("🌐 **Mirror dipilih!** 🚀")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-detect URLs in messages"""
    text = update.message.text
    urls = re.findall(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\$\\$,]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', text)
    
    if urls:
        await mirror(update, context)

# ------------------------
# MAIN - PERFECT
# ------------------------
def main():
    print("🤖 Starting Mahiro Bot...")
    
    if not TOKEN:
        print("❌ TELEGRAM_TOKEN required!")
        return
        
    if not shutil.which("wget"):
        print("❌ wget required!")
        return

    app = ApplicationBuilder().token(TOKEN).build()
    
    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mirror", mirror))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Start worker ✅
    async def init_app(app):
        logger.info("🔥 Starting worker...")
        asyncio.create_task(worker(app))
        logger.info("✅ Worker ready!")
    
    app.post_init = init_app
    
    print("🚀 Bot fully started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
