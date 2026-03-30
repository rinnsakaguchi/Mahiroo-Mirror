import asyncio
from playwright.async_api import async_playwright
import subprocess
import os
import requests

URL = os.getenv("URL")
PASSWORD = os.getenv("PASSWORD")

def upload_pixeldrain(file_path):
    print("☁️ Uploading to PixelDrain...")

    with open(file_path, "rb") as f:
        r = requests.post(
            "https://pixeldrain.com/api/file",
            files={"file": f}
        )

    if r.status_code != 200:
        print("❌ Upload failed:", r.text)
        return None

    data = r.json()
    link = f"https://pixeldrain.com/u/{data['id']}"
    return link


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        print("🔗 Opening page...")
        await page.goto(URL)

        print("🔑 Input password...")
        await page.fill('input[type="password"]', PASSWORD)
        await page.keyboard.press("Enter")

        await page.wait_for_timeout(5000)

        print("📥 Getting download link...")
        links = await page.eval_on_selector_all(
            "a",
            "elements => elements.map(e => e.href)"
        )

        download_link = None
        for l in links:
            if "download" in l:
                download_link = l
                break

        if not download_link:
            print("❌ Failed to get link")
            await browser.close()
            exit(1)

        print("✅ Found:", download_link)

        filename = "downloaded_file"

        cmd = [
            "curl",
            "-L",
            "--fail",
            "-o", filename,
            "-H", "User-Agent: Mozilla/5.0",
            "-H", "Referer: https://frbox.transsion.com/",
            download_link
        ]

        print("🚀 Downloading...")
        result = subprocess.run(cmd)

        if result.returncode != 0:
            print("❌ Download failed")
            exit(1)

        print("✅ Download done!")

        # upload ke pixeldrain
        link = upload_pixeldrain(filename)

        if not link:
            print("❌ Upload failed")
            exit(1)

        print("✅ PixelDrain Link:", link)

        # simpan ke file biar muncul di artifact/log
        with open("result.txt", "w") as f:
            f.write(link)

        await browser.close()

asyncio.run(main())
