import asyncio
from playwright.async_api import async_playwright
import os
from dotenv import load_dotenv
import aiohttp
import json

load_dotenv()

EMAIL = os.environ.get("EMAIL")
PASSWORD = os.environ.get("PASSWORD")
PROJECT_ID = "213452"
BASE_URL = "https://app.humansignal.com"

async def run():
    print("1. Mở Playwright để đăng nhập và lấy Cookies...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        
        await page.goto(f"{BASE_URL}/user/login/")
        await page.fill("input[name='email']", EMAIL)
        await page.fill("input[name='password']", PASSWORD)
        async with page.expect_navigation():
            await page.click("button[type='submit']")
            
        print("Đăng nhập thành công! Đang trích xuất cookies...")
        cookies = await context.cookies()
        cookie_dict = {c['name']: c['value'] for c in cookies}
        
        print("Đã lấy được cookies! Đang tắt Playwright...")
        await browser.close()
        
    print("Playwright đã được tắt hoàn toàn.")
    
    print("\n2. Sử dụng aiohttp để gọi API lấy task...")
    async with aiohttp.ClientSession(cookies=cookie_dict) as session:
        # Thử gọi API để lấy danh sách tasks hoặc next task
        # Thử URL 1: /api/projects/{id}/next
        url1 = f"{BASE_URL}/api/projects/{PROJECT_ID}/next/"
        print(f"-> Thử GET {url1}")
        async with session.get(url1) as resp:
            print(f"Status: {resp.status}")
            if resp.status == 200:
                data = await resp.json()
                print("Result:")
                print(json.dumps(data, indent=2, ensure_ascii=False))
            else:
                print(await resp.text())
                
        # Thử URL 2: /api/tasks?project={id}
        url2 = f"{BASE_URL}/api/tasks/?project={PROJECT_ID}&page_size=1"
        print(f"\n-> Thử GET {url2}")
        async with session.get(url2) as resp:
            print(f"Status: {resp.status}")
            if resp.status == 200:
                data = await resp.json()
                print("Result:")
                if isinstance(data, dict) and "tasks" in data:
                    print(json.dumps(data["tasks"], indent=2, ensure_ascii=False)[:500] + "...")
                else:
                    print(json.dumps(data, indent=2, ensure_ascii=False)[:500] + "...")
            else:
                print(await resp.text())

if __name__ == "__main__":
    asyncio.run(run())
