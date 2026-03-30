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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Utils
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
        return mirrors[:8]
    except:
        return []

# Download
async def download_file(application, chat_id: int, url: str, filename: str):
    global current_process
    
    logger.info(f"wget: {filename}")
    
    cmd = [
        "wget", "--no-verbose", "--show-progress", "--progress=bar:force:noscroll",
        "--tries=3", "--timeout=60", "--limit-rate=10m", "-c",
        "--user-agent=Mozilla/5.0", "--referer=https://frboxdata.transsion.com/",
        "-O", filename, url
    ]
    
    # [FIX 1] Use asyncio subprocess to avoid blocking the bot
    process = await asyncio.create_subprocess_exec(
        *cmd, 
        stdout=asyncio.subprocess.PIPE, 
        stderr=asyncio.subprocess.STDOUT
    )
    current_process = process

    msg = await application.bot.send_message(chat_id, f"📥 `{filename}`", parse_mode='Markdown')
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])
    
    try:
        last_edit_time = 0
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            
            line_str = line.decode('utf-8', errors='ignore').strip()
            
            # Throttling edits to avoid Telegram FloodWait errors (update every 2 seconds)
            if '%' in line_str and (time.time() - last_edit_time > 2.0):
                try:
                    await msg.edit_text(f"📥 `{filename}`\n\n`{line_str}`", parse_mode='Markdown', reply_markup=kb)
                    last_edit_time = time.time()
                except:
                    pass
        rc = await process.wait()
    finally:
        current_process = None

    if rc != 0 or not os.path.exists(filename) or os.path.getsize(filename) == 0:
        raise Exception("Download failed or cancelled")
    
    size_gb = os.path.getsize(filename) / (1024**3)
    await msg.edit_text(f"✅ `{filename}`\n📦 **{size_gb:.2f} GB**", parse_mode='Markdown')

# Worker
async def worker(application):
    logger.info("🟢 Worker online")
    while True:
        try:
            task = await task_queue.get()
            logger.info(f"Task: {task['url'][:50]}...")
            
            chat_id = task["chat"]
            url = task["url"]
            filename = get_filename(url)
            
            await application.bot.send_message(chat_id, f"🚀 **{filename}**", parse_mode='Markdown')
            await download_file(application, chat_id, url, filename)
            
            # GoFile
            await application.bot.send_message(chat_id, "📤 Uploading to GoFile...")
            loop = asyncio.get_event_loop()
            
            # [FIX 4] Dynamically fetch the best GoFile server
            def upload():
                try:
                    server_req = requests.get("https://api.gofile.io/getServer")
                    server = server_req.json()['data']['server']
                    
                    with open(filename, 'rb') as f:
                        r = requests.post(f"https://{server}.gofile.io/uploadFile", files={'file': f})
                    return r.json()['data']['downloadPage']
                except Exception as e:
                    logger.error(f"GoFile Upload Error: {e}")
                    return None
            
            link = await loop.run_in_executor(None, upload)
            
            if link:
                await application.bot.send_message(chat_id, f"✅ **{link}**", parse_mode='Markdown')
            else:
                await application.bot.send_message(chat_id, "❌ Upload gagal")
            
            if os.path.exists(filename):
                os.remove(filename)
                
        except Exception as e:
            logger.error(f"Worker Error: {e}")
        finally:
            task_queue.task_done()

# Commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🤖 **Mahiro Bot v3.0**\n\n{get_system_info()}\n\n"
        f"💡 `/mirror <url>`\n"
        f"💡 Reply link\n"
        f"📊 `/status`",
        parse_mode='Markdown'
    )

async def mirror(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = context.args[0] if context.args else None
    if not url and update.message.reply_to_message and update.message.reply_to_message.text:
        url = update.message.reply_to_message.text
    
    if not url or not ('http' in url):
        await update.message.reply_text("❌ **Kirim URL HTTP!**\n`/mirror <link>`", parse_mode='Markdown')
        return
    
    # [FIX 3] Store the URL in user_data so the callback button can find it
    context.user_data['last_url'] = url
    
    # SF detection
    if "sourceforge.net/projects" in url:
        mirrors = get_sf_mirrors(url)
        if mirrors:
            kb = [[InlineKeyboardButton(m[:25], callback_data=f"sf:{m}")] for m in mirrors]
            await update.message.reply_text("🌐 **SF Mirrors:**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
            return
    
    await task_queue.put({"chat": update.effective_chat.id, "url": url})
    await update.message.reply_text("🚀 **Queue #{}**".format(task_queue.qsize()), parse_mode='Markdown')

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    qsize = task_queue.qsize()
    status_msg = f"📊 **Mahiro Status**\nQueue: `{qsize}`\n{get_system_info()}"
    await update.message.reply_text(status_msg, parse_mode='Markdown')

# Handlers
async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "cancel" and current_process:
        current_process.terminate()
        await query.edit_message_text("❌ **Cancelled**")
    
    elif query.data.startswith("sf:"):
        mirror_name = query.data[3:]
        # Retrieve the original URL stored in the /mirror command
        original_url = context.user_data.get('last_url')
        
        if not original_url:
            await query.edit_message_text("❌ **Session expired. Tolong kirim link lagi.**")
            return
            
        final_url = f"{original_url}?use_mirror={mirror_name}"
        await task_queue.put({"chat": query.message.chat_id, "url": final_url})
        await query.edit_message_text("🌐 **Mirror selected!** 🚀 Added to queue.")

async def auto_mirror(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    urls = re.findall(r'http[s]?://[^\s<>"]+', text)
    if urls:
        # [FIX 2] Inject the URL into context.args before calling mirror
        context.args = [urls[0]]
        await mirror(update, context)

async def post_init(application):
    asyncio.create_task(worker(application))
    print("✅ Worker started!")

# MAIN
def main():
    print("🤖 Mahiro v3.0 starting...")
    
    if not TOKEN:
        print("❌ No TELEGRAM_TOKEN found in environment variables.")
        return
    if not shutil.which("wget"):
        print("❌ No wget found on the system.")
        return

    # [FIX 5] Pass post_init properly through the ApplicationBuilder
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mirror", mirror))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, auto_mirror))
    
    print("🎉 Bot ready!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
