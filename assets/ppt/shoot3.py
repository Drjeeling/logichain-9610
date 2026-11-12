import asyncio
from playwright.async_api import async_playwright

OUT = "/workspace/transport_agent/assets/ppt"
BASE = "http://127.0.0.1:8000"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = await browser.new_page(viewport={"width": 1680, "height": 1050}, device_scale_factor=2)
        await page.goto(BASE, wait_until="networkidle")
        await page.wait_for_selector("#samplesList button[data-act='run'][data-id='scene3_green'][data-fmt='json']", timeout=20000)
        await page.click("#samplesList button[data-act='run'][data-id='scene3_green'][data-fmt='json']")
        await page.wait_for_selector("#declPanel", timeout=120000)
        await page.wait_for_timeout(2500)
        panel = page.locator("#declPanel")
        await panel.scroll_into_view_if_needed()
        await page.wait_for_timeout(800)
        await panel.screenshot(path=f"{OUT}/09_decl_panel.png")
        box = await panel.bounding_box()
        print("panel box", box)
        await browser.close()


asyncio.run(main())
