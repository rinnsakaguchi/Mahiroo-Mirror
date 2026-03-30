import asyncio
from playwright.async_api import async_playwright
import subprocess
import os

URL = os.getenv("URL")
PASSWORD = os.getenv("PASSWORD")

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

        print("✅ Done!")
        await browser.close()

asyncio.run(main())
