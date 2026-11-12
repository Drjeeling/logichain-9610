import asyncio
from playwright.async_api import async_playwright

OUT = "/workspace/transport_agent/assets/ppt"
BASE = "http://127.0.0.1:8000"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = await browser.new_page(viewport={"width": 1680, "height": 1050}, device_scale_factor=2)
        await page.goto(BASE, wait_until="networkidle")
        await page.click("#kbTrigger")
        await page.wait_for_timeout(700)
        await page.click("#kbPresets button[data-q]")
        await page.wait_for_timeout(8000)
        drawer = page.locator(".kb-panel")
        await drawer.screenshot(path=f"{OUT}/10_kb_drawer.png")
        print("done")
        await browser.close()


asyncio.run(main())
