import asyncio
import os
from playwright.async_api import async_playwright

OUT = "/workspace/transport_agent/assets/ppt"
BASE = "http://127.0.0.1:8000"


async def main():
    os.makedirs(OUT, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = await browser.new_page(viewport={"width": 1680, "height": 1050}, device_scale_factor=2)
        await page.goto(BASE, wait_until="networkidle")
        await page.wait_for_selector("#samplesList button[data-act='run']", timeout=20000)
        await page.wait_for_timeout(1200)

        # 1. 首页全貌
        await page.screenshot(path=f"{OUT}/01_home.png", full_page=True)
        await page.screenshot(path=f"{OUT}/01b_home_top.png")
        print("shot 01")

        # 2. 跑场景一（JSON 快链路），等结果
        await page.click("#samplesList button[data-act='run'][data-id='scene1_ip'][data-fmt='json']")
        await page.wait_for_selector(".customs-viz", timeout=120000)
        await page.wait_for_timeout(2500)

        trace = page.locator("#agentTrace")
        if await trace.count():
            await trace.screenshot(path=f"{OUT}/03_agent_trace.png")
            print("shot 03 trace")

        viz = page.locator(".customs-viz").first
        await viz.scroll_into_view_if_needed()
        await page.wait_for_timeout(600)
        await viz.screenshot(path=f"{OUT}/02_customs_scene1.png")
        print("shot 02 customs")

        # 3. 制单面板
        decl = page.locator("text=9610 申报清单").first
        if await decl.count():
            await decl.scroll_into_view_if_needed()
            await page.wait_for_timeout(1500)
            await page.screenshot(path=f"{OUT}/04_declaration.png", full_page=False)
            print("shot 04 decl")

        # 4. 整页结果
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(500)
        await page.screenshot(path=f"{OUT}/05_result_full.png", full_page=True)
        print("shot 05 full")

        # 5. 知识库抽屉
        await page.click("#kbTrigger")
        await page.wait_for_timeout(600)
        preset = page.locator("#kbPresets button[data-q]").first
        if await preset.count():
            await preset.click()
        else:
            await page.fill("#kbInput", "9610 出口电池需要什么监管条件")
            await page.click("#kbAskBtn")
        await page.wait_for_timeout(6000)
        await page.screenshot(path=f"{OUT}/06_kb.png")
        print("shot 06 kb")

        await browser.close()


asyncio.run(main())
