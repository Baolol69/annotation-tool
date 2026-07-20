import asyncio
from playwright.async_api import async_playwright
import os
import json
from dotenv import load_dotenv

load_dotenv()

EMAIL = os.environ.get("EMAIL")
PASSWORD = os.environ.get("PASSWORD")

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False) # Mở trình duyệt để xem
        context = await browser.new_context()
        page = await context.new_page()
        
        # Hàm bắt API Submit
        async def handle_request(request):
            if "api/tasks/" in request.url and "/annotations" in request.url and request.method == "POST":
                print("\n" + "="*50)
                print(f"[API BẮT ĐƯỢC] {request.method} {request.url}")
                print("PAYLOAD:")
                try:
                    payload = request.post_data_json
                    print(json.dumps(payload, indent=2, ensure_ascii=False))
                except Exception as e:
                    print(request.post_data)
                print("="*50 + "\n")
        
        page.on("request", handle_request)
        
        print("[DEBUG] Đang truy cập trang đăng nhập...")
        await page.goto("https://app.humansignal.com/user/login/")
        await page.fill("input[name='email']", EMAIL)
        await page.fill("input[name='password']", PASSWORD)
        async with page.expect_navigation():
            await page.click("button[type='submit']")
            
        print("[DEBUG] Đã đăng nhập thành công! Đang vào trang dự án...")
        await page.goto("https://app.humansignal.com/projects/213452/labeling")
        
        print("[DEBUG] Đang đợi bạn thao tác Submit trên trình duyệt hiện lên...")
        print("[DEBUG] Vui lòng thao tác điền thông tin và bấm Submit trên cửa sổ trình duyệt Chromium vừa mở.")
        
        # Treo vô hạn để bạn thao tác tay trên trình duyệt
        await page.wait_for_timeout(3600000)
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
