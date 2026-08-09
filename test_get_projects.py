import asyncio
import aiohttp
import os
from dotenv import load_dotenv

load_dotenv()

EMAIL = os.getenv("EMAIL", "")
PASSWORD = os.getenv("PASSWORD", "")
HUMANSIGNAL_BASE_URL = "https://app.humansignal.com"

async def test_get_projects():
    if not EMAIL or not PASSWORD:
        print("Thiếu EMAIL hoặc PASSWORD trong file .env!")
        return
        
    print(f"[*] Đang thử đăng nhập vào {HUMANSIGNAL_BASE_URL} bằng {EMAIL}...")
    
    async with aiohttp.ClientSession() as session:
        login_url = f"{HUMANSIGNAL_BASE_URL}/user/login/"
        
        # 1. Lấy CSRF Token
        async with session.get(login_url) as resp:
            html = await resp.text()
            
        csrftoken = ""
        for cookie in session.cookie_jar:
            if cookie.key in ("csrftoken", "csrf_token"):
                csrftoken = cookie.value
                break
                
        if not csrftoken:
            import re
            match = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', html)
            if match:
                csrftoken = match.group(1)
                
        # 2. Đăng nhập
        data = {
            "email": EMAIL,
            "password": PASSWORD,
            "csrfmiddlewaretoken": csrftoken
        }
        headers = {"Referer": login_url}
        
        async with session.post(login_url, data=data, headers=headers) as resp2:
            print(f"[*] HTTP Status khi login: {resp2.status}")
            
        cookie_dict = {}
        for cookie in session.cookie_jar:
            cookie_dict[cookie.key] = cookie.value
            
        if "sessionid" in cookie_dict or "session" in cookie_dict:
            print("[+] Đăng nhập thành công! Đã lấy được cookies.")
        else:
            print("[-] Đăng nhập thất bại, không có session cookie!")
            return
            
        # 3. Thử gọi API lấy danh sách projects
        projects_url = f"{HUMANSIGNAL_BASE_URL}/api/projects/counts"
        print(f"\n[*] Đang gọi API lấy danh sách projects: {projects_url}...")
        
        async with session.get(projects_url) as resp3:
            print(f"[*] HTTP Status API Projects: {resp3.status}")
            if resp3.status == 200:
                data = await resp3.json()
                import json
                print("\n[+] KẾT QUẢ API PROJECTS (JSON):")
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                print(f"[-] Lỗi gọi API: {await resp3.text()}")

if __name__ == "__main__":
    asyncio.run(test_get_projects())
