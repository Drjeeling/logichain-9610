"""TRAE 好应用大赏 · 演示视频录制脚本。

录制品：首页 → 9610 核心演示（A 直通 / C 拦截 / B 复核）→ 法规 RAG 问答。
输出：assets/ppt/demo_video.mp4（Playwright webm 封装，后转 mp4 如有 ffmpeg）
"""
import asyncio
import os
from playwright.async_api import async_playwright

OUT = "/workspace/transport_agent/assets/ppt"
BASE = "http://127.0.0.1:8000"
VIDEO_DIR = os.path.join(OUT, "video_raw")

SCENES = [
    # (场景 id, 标签)
    ("scene3_green", "9610 核心演示 · A 级直通"),
    ("scene2_dg", "危险品硬边界 · C 级拦截"),
    ("scene4_lowvalue", "9610 低报预警 · B 级复核"),
]


async def main():
    os.makedirs(VIDEO_DIR, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context(
            viewport={"width": 1600, "height": 1000},
            device_scale_factor=1.5,
            record_video_dir=VIDEO_DIR,
            record_video_size={"width": 1600, "height": 1000},
        )
        page = await context.new_page()

        # 1. 首页
        await page.goto(BASE, wait_until="networkidle")
        await page.wait_for_timeout(3000)
        print("rec: home")

        # 2. 三个场景依次运行
        for sid, label in SCENES:
            btn = page.locator(f"#samplesList button[data-act='run'][data-id='{sid}'][data-fmt='json']").first
            await btn.scroll_into_view_if_needed()
            await page.wait_for_timeout(800)
            await btn.click()
            print(f"rec: run {label}", flush=True)
            try:
                await page.wait_for_selector(".customs-viz", timeout=120000)
            except Exception:
                print(f"WARN: no .customs-viz for {sid}")
            await page.wait_for_timeout(2000)
            viz = page.locator(".customs-viz").first
            try:
                await viz.scroll_into_view_if_needed()
            except Exception:
                pass
            await page.wait_for_timeout(4000)  # 结果停留
            # 滚到清单草稿区（如有）
            decl = page.locator("text=9610 申报清单").first
            try:
                if await decl.count():
                    await decl.scroll_into_view_if_needed()
                    await page.wait_for_timeout(2500)
            except Exception:
                pass
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(1000)

        # 3. 法规知识库 RAG 问答
        try:
            await page.click("#kbTrigger")
            await page.wait_for_timeout(800)
            preset = page.locator("#kbPresets button[data-q]").first
            if await preset.count():
                await preset.click()
            else:
                await page.fill("#kbInput", "9610 出口锂电池需要什么监管条件")
                await page.click("#kbAskBtn")
            await page.wait_for_timeout(7000)  # 等回答渲染
            await page.screenshot(path=f"{OUT}/video_kb_end.png")
            await page.wait_for_timeout(3000)
        except Exception as e:
            print(f"WARN kb: {e}")

        # 4. 收尾
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(2000)

        video = page.video
        await context.close()
        path = await video.path()
        await browser.close()
        print("VIDEO:", path)


asyncio.run(main())
