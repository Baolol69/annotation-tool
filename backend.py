import sys
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, HTTPException, Response
import asyncio
from playwright.async_api import async_playwright, Page, Response as PlaywrightResponse
import os
import io
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
action_queue = None  # Sẽ khởi tạo bên trong event loop
playwright_context = {}
audio_cache = {}
processed_tasks = set()

async def get_gemini_reponse_async(page: Page, task: CurrentTask):
    print(f"[DEBUG] Bắt đầu tải audio cho task {task.task_id} từ {task.audio_url_path}...", flush=True)
    
    # Get cookies to authenticate the request
    cookies = await page.context.cookies()
    cookie_dict = {c["name"]: c["value"] for c in cookies}
    
    def download_audio():
        import requests
        resp = requests.get(task.audio_url_path, cookies=cookie_dict, timeout=30)
        resp.raise_for_status()
        return resp.content
        
    def process_audio_fast(raw_bytes):
        import wave
        import io
        import audioop
        try:
            with wave.open(io.BytesIO(raw_bytes), 'rb') as wav_in:
                n_channels = wav_in.getnchannels()
                sampwidth = wav_in.getsampwidth()
                framerate = wav_in.getframerate()
                n_frames = wav_in.getnframes()
                audio_data = wav_in.readframes(n_frames)

            # 1. Chuyển sang Mono nếu đang là Stereo
            if n_channels == 2:
                audio_data = audioop.tomono(audio_data, sampwidth, 1, 1)
                n_channels = 1
                
            # 2. Resample về 16000Hz để giảm dung lượng cực nhanh
            if framerate != 16000:
                audio_data, _ = audioop.ratecv(audio_data, sampwidth, n_channels, framerate, 16000, None)
                framerate = 16000
                
            # 3. Tăng âm lượng lên khoảng +5dB (nhân hệ số ~1.778)
            audio_data = audioop.mul(audio_data, sampwidth, 1.778)

            out_buffer = io.BytesIO()
            with wave.open(out_buffer, 'wb') as wav_out:
                wav_out.setnchannels(n_channels)
                wav_out.setsampwidth(sampwidth)
                wav_out.setframerate(framerate)
                wav_out.writeframes(audio_data)

            return out_buffer.getvalue()
        except Exception as e:
            print(f"[DEBUG-AUDIO] Cảnh báo, lỗi dùng wave ({e}), tự động trả về file gốc!", flush=True)
            return raw_bytes

    try:
        raw_audio_bytes = await asyncio.wait_for(
            asyncio.to_thread(download_audio),
            timeout=40.0
        )
        final_audio_bytes = await asyncio.to_thread(process_audio_fast, raw_audio_bytes)
        print(f"[DEBUG] Xử lý âm thanh siêu tốc xong ({len(final_audio_bytes)} bytes). Gửi tới Gemini API...", flush=True)
    except Exception as e:
        print(f"[ERROR] Lỗi tải audio từ HumanSignal ({e})! Bỏ qua audio để chống kẹt hệ thống.", flush=True)
        import wave, io
        out_buffer = io.BytesIO()
        with wave.open(out_buffer, 'wb') as wav_out:
            wav_out.setnchannels(1)
            wav_out.setsampwidth(2)
            wav_out.setframerate(16000)
            wav_out.writeframes(b'\x00' * 32000) # 1 second of silence
        final_audio_bytes = out_buffer.getvalue()
    
    from gemini import get_response_async
    annotation_response = await get_response_async(task.task_id, final_audio_bytes, task.prediction)
    print(f"[DEBUG] Nhận được kết quả từ Gemini cho task {task.task_id}!", flush=True)
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
            print(f"[DEBUG] Đang đọc nội dung response từ Playwright...", flush=True)
            
            # Thêm timeout vào phòng trường hợp Playwright bị treo khi đọc body
            body = await asyncio.wait_for(response.json(), timeout=10)
            print(f"[DEBUG] Đã đọc xong nội dung response!", flush=True)
            
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
            
        except asyncio.TimeoutError:
            print(f"[ERROR] Quá thời gian (10s) khi đọc response body từ Playwright!", flush=True)
        except Exception as e:
            print(f"[ERROR] Lỗi phân tích response: {e}", flush=True)

async def do_submit_response(page: Page, submit_task: SubmitTask):
    try:
        if submit_task.transcript:
            try:
                await page.fill("textarea", submit_task.transcript, timeout=3000)
            except Exception as e:
                print(f"[ERROR] Fill transcript failed: {e}", flush=True)
        
        async def safe_check(val):
            if val:
                try:
                    loc = page.locator(f'input[value="{val}"], input[name="{val}"]')
                    await loc.first.check(timeout=3000)
                except Exception as e:
                    print(f"[ERROR] Check {val} failed: {e}", flush=True)

        await safe_check(submit_task.gender)
        await safe_check(submit_task.topic)
        if submit_task.audio_issues:
            for issue in submit_task.audio_issues:
                await safe_check(issue)

        try:
            await page.get_by_test_id("bottombar-submit-button").click(timeout=5000)
            print("[DEBUG] Đã nhấn nút Submit thành công trên giao diện web!", flush=True)
        except Exception as e:
            print(f"[ERROR] Click submit failed: {e}", flush=True)
            try:
                await page.locator("button:has-text('Submit')").first.click(timeout=3000)
            except Exception as e2:
                print(f"[ERROR] Click submit fallback failed: {e2}", flush=True)

    except Exception as e:
        print(f"[ERROR] submit_response: {e}", flush=True)

async def do_skip(page: Page):
    try:
        await page.get_by_test_id("bottombar-skip-button").click()
        print("[DEBUG] Đã nhấn nút Skip thành công trên giao diện web!", flush=True)
    except Exception as e:
        print(f"[ERROR] skip: {e}", flush=True)

import time

async def playwright_loop():
    global action_queue
    action_queue = asyncio.Queue()
    try:
        print("[DEBUG] Bắt đầu khởi chạy Playwright...", flush=True)
        p = await async_playwright().start()
        playwright_context['p'] = p
        
        # Cấu hình siêu tiết kiệm RAM cho Render (512MB limit)
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage', # Tránh crash do thiếu shared memory
                '--disable-gpu',
                '--no-zygote',
                '--disable-extensions',
                '--single-process', # Ép chạy 1 process duy nhất để giảm cực mạnh RAM
                '--js-flags="--max-old-space-size=128"' # Ép giới hạn RAM của React xuống 128MB
            ]
        )
        context = await browser.new_context(no_viewport=True)
        
        # Chặn tải hình ảnh, font chữ, video để tiết kiệm tối đa RAM và băng thông
        async def route_interceptor(route):
            if route.request.resource_type in ["image", "font", "stylesheet"]:
                await route.abort()
            else:
                await route.continue_()
        
        await context.route("**/*", route_interceptor)
        
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
                print(f"[DEBUG] Bắt đầu click Submit...", flush=True)
                await do_submit_response(page, data)
                print(f"[DEBUG] Click Submit xong mất {time.time() - last_action_time:.2f}s, đợi tải trang...", flush=True)
                global_task_state = TaskState() # Reset UI
            elif action == "skip":
                last_action_time = time.time()
                print(f"[DEBUG] Bắt đầu click Skip...", flush=True)
                await do_skip(page)
                print(f"[DEBUG] Click Skip xong mất {time.time() - last_action_time:.2f}s, đợi tải trang...", flush=True)
                global_task_state = TaskState() # Reset UI
            elif action == "process_task":
                task_recv_time = time.time()
                if last_action_time:
                    print(f"[DEBUG] Mạng trả về Task mới sau {task_recv_time - last_action_time:.2f}s (kể từ lúc nhấn nút). Đang xử lý AI...")
                
                print(f"[INFO] Processing task {data.task_id}")
                annotation_resp, audio_bytes = await get_gemini_reponse_async(page, data)
                
                end_time = time.time()
                print(f"[DEBUG] Xử lý AI & Audio xong mất {end_time - task_recv_time:.2f}s", flush=True)
                if last_action_time:
                    print(f"[DEBUG] === TỔNG THỜI GIAN CHUYỂN TASK: {end_time - last_action_time:.2f}s ===", flush=True)

                # Store audio in cache and assign URL
                audio_cache[data.task_id] = audio_bytes
                
                # Sửa lỗi hardcode port 8000
                import os
                current_port = os.environ.get("PORT", "8000")
                data.audio_data = f"http://127.0.0.1:{current_port}/api/audio/{data.task_id}"

                global_task_state.task = data
                global_task_state.gemini_response = annotation_resp
                print(f"[INFO] Task state updated for UI. ĐÃ MỞ KHÓA GIAO DIỆN CHỜ NGƯỜI DÙNG BẤM NÚT!", flush=True)
                task_ready_event.set()
                
        except Exception as e:
            print(f"[ERROR] Playwright loop error: {e}", flush=True)
            
        # Giải phóng bộ nhớ rác sau mỗi vòng lặp để chống tràn RAM 512MB
        import gc
        gc.collect()

    print("[FATAL ERROR] Vòng lặp Playwright đã bị thoát (page closed)!!!", flush=True)

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
