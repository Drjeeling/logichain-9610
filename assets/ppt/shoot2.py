import asyncio
from playwright.async_api import async_playwright

OUT = "/workspace/transport_agent/assets/ppt"
BASE = "http://127.0.0.1:8000"


async def run_case(page, sid, prefix):
    await page.goto(BASE, wait_until="networkidle")
    await page.wait_for_selector(f"#samplesList button[data-act='run'][data-id='{sid}'][data-fmt='json']", timeout=20000)
    await page.click(f"#samplesList button[data-act='run'][data-id='{sid}'][data-fmt='json']")
    await page.wait_for_selector(".customs-viz", timeout=120000)
    await page.wait_for_timeout(2200)
    viz = page.locator(".customs-viz").first
    await viz.scroll_into_view_if_needed()
    await page.wait_for_timeout(500)
    await viz.screenshot(path=f"{OUT}/{prefix}_customs.png")
    # 顶部：等级 + 槽位
    box = await viz.bounding_box()
    await page.screenshot(path=f"{OUT}/{prefix}_top.png", clip={
        "x": box["x"], "y": box["y"], "width": box["width"], "height": min(330, box["height"])
    })
    print(sid, "done", box)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = await browser.new_page(viewport={"width": 1680, "height": 1050}, device_scale_factor=2)
        await run_case(page, "scene3_green", "07_scene3_A")
        await run_case(page, "scene4_lowvalue", "08_scene4_B")
        await browser.close()


asyncio.run(main())
