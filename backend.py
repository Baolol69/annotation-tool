import sys
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, HTTPException, Response
import asyncio
from playwright.async_api import async_playwright, Page, Response as PlaywrightResponse
import os
import io
from pydub import AudioSegment
import numpy as np
from gemini import get_response, AnnotationResponse
from schemas import CurrentTask, SubmitTask, TaskState
from dotenv import load_dotenv

load_dotenv()

EMAIL = os.getenv("EMAIL", "")
PASSWORD = os.getenv("PASSWORD", "")

HUMANSIGNAL_LOGIN_URL = "https://app.humansignal.com/user/login/"
HUMANSIGNAL_PROJECT_URL = "https://app.humansignal.com/projects/213452/labeling"
HUMANSIGNAL_BASE_URL = "https://app.humansignal.com"

# Global state
global_task_state = TaskState()
action_queue = asyncio.Queue()
playwright_context = {}
audio_cache = {}

async def get_gemini_reponse_async(page: Page, task: CurrentTask):
    print(f"[DEBUG] Bắt đầu tải audio cho task {task.task_id} từ {task.audio_url_path}...")
    response = await page.context.request.get(task.audio_url_path)
    raw_audio_bytes = await response.body()
    print(f"[DEBUG] Tải xong audio ({len(raw_audio_bytes)} bytes). Bắt đầu xử lý âm thanh...")
    
    def process_audio(raw_bytes):
        raw_ram_buffer = io.BytesIO(raw_bytes)
        audio = AudioSegment.from_file(raw_ram_buffer)
        audio = audio.set_channels(1)
        audio = audio.set_frame_rate(16000)
        audio = audio + 5
        processed_ram_buffer = io.BytesIO()
        audio.export(processed_ram_buffer, format="wav")
        return processed_ram_buffer.getvalue()

    final_audio_bytes = await asyncio.to_thread(process_audio, raw_audio_bytes)
    print(f"[DEBUG] Xử lý âm thanh xong. Gửi tới Gemini API...")
    annotation_response = await asyncio.to_thread(get_response, task.task_id, final_audio_bytes, task.prediction)
    print(f"[DEBUG] Nhận được kết quả từ Gemini cho task {task.task_id}!")
    return annotation_response, final_audio_bytes

def extract_prediction(response_body: dict) -> str:
    try:
        predictions = response_body.get("predictions")
        if not predictions or not isinstance(predictions, list):
            return ""
        result = predictions[-1].get("result")
        if not result or not isinstance(result, list):
            return ""
        value = result[0].get("value", {})
        text_list = value.get("text")
        if not text_list or not isinstance(text_list, list):
            return ""
        return str(text_list[0]).strip()
    except (IndexError, AttributeError, TypeError):
        return ""

def response_parser(response_body: dict) -> CurrentTask:
    task_id = str(response_body.get("id"))
    task_data = response_body.get("data", {})
    prediction = extract_prediction(response_body)
    region = task_data.get("region", "Unknown")
    audio_url_path = HUMANSIGNAL_BASE_URL + task_data.get("audio", "")

    return CurrentTask(
        task_id=task_id,
        prediction=prediction,
        region=region,
        audio_url_path=audio_url_path,
    )

async def handle_response(page: Page, response: PlaywrightResponse):
    global global_task_state
    
    is_next_task = "api/dm/actions?id=next_task" in response.url
    is_get_task = "api/tasks/" in response.url and response.request.method == "GET"

    if (is_next_task or is_get_task) and response.status == 200:
        try:
            print(f"[DEBUG] Phát hiện API trả về task: {response.url}", flush=True)
            body = await response.json()
            
            # The API might return a list of tasks or a single task dictionary
            if isinstance(body, list) and len(body) > 0:
                task_data = body[0]
            elif isinstance(body, dict):
                task_data = body
            else:
                return
                
            task = response_parser(task_data)
            
            # Bỏ qua nếu đã xử lý
            if task.task_id in processed_tasks:
                print(f"[DEBUG] Bỏ qua task {task.task_id} vì đã xử lý.", flush=True)
                return
            processed_tasks.add(task.task_id)
            
            print(f"[DEBUG] Đưa task {task.task_id} vào hàng đợi xử lý...", flush=True)
            await action_queue.put(("process_task", task))
            
        except Exception as e:
            print(f"[ERROR] Lỗi phân tích response: {e}", flush=True)

async def do_submit_response(page: Page, submit_task: SubmitTask):
    try:
        if submit_task.transcript:
            try:
                await page.fill("textarea", submit_task.transcript, timeout=3000)
            except Exception as e:
                print(f"[ERROR] Fill transcript failed: {e}")
        
        async def safe_check(val):
            if val:
                try:
                    loc = page.locator(f'input[value="{val}"], input[name="{val}"]')
                    await loc.first.check(timeout=3000)
                except Exception as e:
                    print(f"[ERROR] Check {val} failed: {e}")

        await safe_check(submit_task.gender)
        await safe_check(submit_task.topic)
        for issue in submit_task.audio_issues:
            await safe_check(issue)

        try:
            await page.get_by_test_id("bottombar-submit-button").click(timeout=5000)
        except Exception as e:
            print(f"[ERROR] Click submit failed: {e}")
            try:
                await page.locator("button:has-text('Submit')").first.click(timeout=3000)
            except Exception as e2:
                print(f"[ERROR] Click submit fallback failed: {e2}")

    except Exception as e:
        print(f"[ERROR] submit_response: {e}")

async def do_skip(page: Page):
    try:
        await page.get_by_test_id("bottombar-skip-button").click()
    except Exception as e:
        print(f"[ERROR] skip: {e}")

import time

async def playwright_loop():
    try:
        print("[DEBUG] Bắt đầu khởi chạy Playwright...", flush=True)
        p = await async_playwright().start()
        playwright_context['p'] = p
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(no_viewport=True)
        page = await context.new_page()
        playwright_context['page'] = page

        print(f"[DEBUG] Đang truy cập trang đăng nhập: {HUMANSIGNAL_LOGIN_URL}", flush=True)
        await page.goto(HUMANSIGNAL_LOGIN_URL)
        
        print(f"[DEBUG] Đã load xong trang đăng nhập, đang điền thông tin...", flush=True)
        if not EMAIL or not PASSWORD:
            print("[FATAL ERROR] Thiếu EMAIL hoặc PASSWORD trong Environment Variables!", flush=True)
            return
            
        await page.fill("input[name='email']", EMAIL)
        await page.fill("input[name='password']", PASSWORD)

        print("[DEBUG] Đang bấm nút đăng nhập...", flush=True)
        async with page.expect_navigation():
            await page.click("button[type='submit']")
        
        page.on("response", lambda x: asyncio.create_task(handle_response(page, x)))
        print("[DEBUG] Đã gài hook bắt request, đang chờ task xuất hiện...", flush=True)
        print(f"[DEBUG] Đăng nhập thành công! Chuyển tới trang dự án: {HUMANSIGNAL_PROJECT_URL}", flush=True)
        await page.goto(HUMANSIGNAL_PROJECT_URL, wait_until="domcontentloaded")
        
    except Exception as e:
        print(f"[FATAL ERROR] Playwright failed to start: {e}", flush=True)
        return

    global global_task_state
    last_action_time = time.time()
    
    while not page.is_closed():
        try:
            action, data = await action_queue.get()
            if action == "submit":
                last_action_time = time.time()
                print(f"[DEBUG] Bắt đầu click Submit...")
                await do_submit_response(page, data)
                print(f"[DEBUG] Click Submit xong mất {time.time() - last_action_time:.2f}s, đợi tải trang...")
                global_task_state = TaskState() # Reset UI
            elif action == "skip":
                last_action_time = time.time()
                print(f"[DEBUG] Bắt đầu click Skip...")
                await do_skip(page)
                print(f"[DEBUG] Click Skip xong mất {time.time() - last_action_time:.2f}s, đợi tải trang...")
                global_task_state = TaskState() # Reset UI
            elif action == "process_task":
                task_recv_time = time.time()
                if last_action_time:
                    print(f"[DEBUG] Mạng trả về Task mới sau {task_recv_time - last_action_time:.2f}s (kể từ lúc nhấn nút). Đang xử lý AI...")
                
                print(f"[INFO] Processing task {data.task_id}")
                annotation_resp, audio_bytes = await get_gemini_reponse_async(page, data)
                
                end_time = time.time()
                print(f"[DEBUG] Xử lý AI & Audio xong mất {end_time - task_recv_time:.2f}s")
                if last_action_time:
                    print(f"[DEBUG] === TỔNG THỜI GIAN CHUYỂN TASK: {end_time - last_action_time:.2f}s ===")

                # Store audio in cache and assign URL
                audio_cache[data.task_id] = audio_bytes
                data.audio_data = f"http://127.0.0.1:8000/api/audio/{data.task_id}"

                global_task_state.task = data
                global_task_state.gemini_response = annotation_resp
                print(f"[INFO] Task state updated for UI")
                task_ready_event.set()
                
        except Exception as e:
            print(f"[ERROR] Playwright loop error: {e}")

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    loop_task = asyncio.create_task(playwright_loop())
    yield
    loop_task.cancel()
    if 'p' in playwright_context:
        await playwright_context['p'].stop()

app = FastAPI(lifespan=lifespan)

task_ready_event = asyncio.Event()

@app.get("/api/task", response_model=TaskState)
async def get_task():
    await task_ready_event.wait()
    return global_task_state

@app.get("/api/audio/{task_id}")
async def get_audio(task_id: str):
    if task_id in audio_cache:
        return Response(content=audio_cache[task_id], media_type="audio/wav")
    raise HTTPException(status_code=404)

@app.post("/api/submit")
async def submit_task(task: SubmitTask):
    task_ready_event.clear()
    await action_queue.put(("submit", task))
    return {"status": "queued"}

@app.post("/api/skip")
async def skip_task():
    task_ready_event.clear()
    await action_queue.put(("skip", None))
    return {"status": "queued"}

import gradio as gr
import frontend

# Mount the gradio app onto the root path of FastAPI
demo = frontend.build_ui()
app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("backend:app", host="0.0.0.0", port=port)
