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

def log_memory(stage: str):
    import sys
    try:
        if sys.platform == "linux":
            with open('/proc/meminfo') as f:
                meminfo = f.read()
            mem_total = int([x for x in meminfo.split('\n') if 'MemTotal' in x][0].split()[1])
            mem_available = int([x for x in meminfo.split('\n') if 'MemAvailable' in x][0].split()[1])
            mem_used_mb = (mem_total - mem_available) / 1024
            print(f"[MEMORY] {stage} | HỆ THỐNG DÙNG: {mem_used_mb:.2f} MB", flush=True)
            
            with open('/proc/self/status') as f:
                for line in f:
                    if 'VmRSS' in line:
                        py_mem_mb = int(line.split()[1]) / 1024
                        print(f"[MEMORY] {stage} | LÕI PYTHON (Gradio/FastAPI) DÙNG: {py_mem_mb:.2f} MB", flush=True)
                        break
    except Exception:
        pass

# Global state
global_task_state = TaskState()
action_queue = None  # Sẽ khởi tạo bên trong event loop
playwright_context = {}
audio_cache = {}
processed_tasks = set()

async def get_gemini_reponse_async(page: Page, task: CurrentTask):
    # Đã chuyển logic tải audio vào process_task và chạy AI nền.
    pass


def response_parser(response_body: dict) -> CurrentTask:
    task_id = str(response_body.get("id"))
    task_data = response_body.get("data", {})
    project_id = str(response_body.get("project", ""))
    region = task_data.get("region", "Unknown")
    audio_path = task_data.get("audio", "")
    if audio_path.startswith("http"):
        audio_url_path = audio_path
    else:
        audio_url_path = HUMANSIGNAL_BASE_URL + audio_path
    prediction = ""
    parent_prediction_id = None
    original_result = []
    
    predictions = response_body.get("predictions")
    if predictions and isinstance(predictions, list):
        last_pred = predictions[-1]
        parent_prediction_id = last_pred.get("id")
        result_array = last_pred.get("result")
        if result_array and isinstance(result_array, list):
            import copy
            original_result = copy.deepcopy(result_array)
            try:
                # Find the textarea text for transcription
                for item in result_array:
                    if item.get("type") == "textarea" and item.get("from_name") == "transcription":
                        text_list = item.get("value", {}).get("text")
                        if text_list and isinstance(text_list, list):
                            prediction = str(text_list[0]).strip()
                            break
            except Exception:
                pass

    return CurrentTask(
        task_id=task_id,
        prediction=prediction,
        region=region,
        audio_url_path=audio_url_path,
        project_id=project_id,
        parent_prediction_id=parent_prediction_id,
        original_result=original_result,
        task_info=task_data
    )

async def handle_response(page: Page, response: PlaywrightResponse):
    global global_task_state
    
    is_next_task = "api/dm/actions?id=next_task" in response.url
    is_get_task = "api/tasks/" in response.url and response.request.method == "GET"

    if is_next_task or is_get_task:
        print(f"[DEBUG-API] Bắt được request: {response.url} | Status: {response.status}", flush=True)
        if response.status != 200:
            print(f"[ERROR] API bị chặn hoặc lỗi (Status {response.status}). Có thể bị Cloudflare block!", flush=True)
            return

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

async def do_submit_response(page: Page, submit_task: SubmitTask, current_task: CurrentTask):
    try:
        import datetime
        import copy
        import random
        import string
        
        def generate_id(length=10):
            return ''.join(random.choices(string.ascii_letters + string.digits, k=length))
        
        # Clone original result from the parent prediction (which usually only has "transcription")
        new_result = copy.deepcopy(current_task.original_result)
        
        # Add manual user inputs with random IDs
        if submit_task.transcript:
            new_result.append({
                "value": {"text": [submit_task.transcript]},
                "id": generate_id(),
                "from_name": "transcript",
                "to_name": "audio",
                "type": "textarea",
                "origin": "manual"
            })
            
        if submit_task.gender:
            new_result.append({
                "value": {"choices": [submit_task.gender]},
                "id": generate_id(),
                "from_name": "gender",
                "to_name": "audio",
                "type": "choices",
                "origin": "manual"
            })
            
        if submit_task.topic:
            new_result.append({
                "value": {"choices": [submit_task.topic]},
                "id": generate_id(),
                "from_name": "topic",
                "to_name": "audio",
                "type": "choices",
                "origin": "manual"
            })
            
        if submit_task.audio_issues:
            new_result.append({
                "value": {"choices": submit_task.audio_issues},
                "id": generate_id(),
                "from_name": "audio_issues", # Tên field phụ thuộc vào cấu hình thực tế
                "to_name": "audio",
                "type": "choices",
                "origin": "manual"
            })

        # Build payload
        payload = {
            "lead_time": 5.5, # Giả lập thời gian
            "result": new_result,
            "draft_id": None,
            "parent_prediction": current_task.parent_prediction_id,
            "parent_annotation": None,
            "started_at": datetime.datetime.utcnow().isoformat() + "Z",
            "project": current_task.project_id
        }
        
        url = f"{HUMANSIGNAL_BASE_URL}/api/tasks/{current_task.task_id}/annotations?project={current_task.project_id}"
        
        print(f"[DEBUG] Đang gửi POST request tới: {url}", flush=True)
        response = await page.context.request.post(url, data=payload)
        
        if response.ok:
            print("[DEBUG] Submit API thành công!", flush=True)
            # Sau khi submit qua API thành công, giao diện web sẽ không tự nhảy sang câu tiếp theo.
            # Do đó chúng ta giả lập ấn vào nút bấm UI một cách nhanh nhất có thể hoặc trigger API "next_task".
            try:
                await page.evaluate("window.location.reload();")
            except Exception:
                pass
        else:
            print(f"[ERROR] Submit API thất bại, status = {response.status}", flush=True)
            text = await response.text()
            print(f"Response: {text}")

    except Exception as e:
        print(f"[ERROR] do_submit_response: {e}", flush=True)

async def do_skip(page: Page):
    try:
        await page.get_by_test_id("bottombar-skip-button").click()
        print("[DEBUG] Đã nhấn nút Skip thành công trên giao diện web!", flush=True)
    except Exception as e:
        print(f"[ERROR] skip: {e}", flush=True)

import time
import aiohttp

async def init_session():
    print("[DEBUG] Bắt đầu khởi chạy Playwright để lấy phiên đăng nhập...", flush=True)
    p = await async_playwright().start()
    
    # Cấu hình tối ưu tốc độ (Chạy Local với Ngrok)
    browser = await p.chromium.launch(
        headless=True,
        args=[
            '--no-sandbox',
            '--disable-setuid-sandbox'
        ]
    )
    context = await browser.new_context(no_viewport=True)
    
    # Chặn tải hình ảnh, font chữ, video để tiết kiệm tối đa RAM và băng thông
    async def route_interceptor(route):
        if route.request.resource_type in ["image", "font", "stylesheet", "media"]:
            await route.abort()
        else:
            await route.continue_()
    
    await context.route("**/*", route_interceptor)
    
    page = await context.new_page()

    print(f"[DEBUG] Đang truy cập trang đăng nhập: {HUMANSIGNAL_LOGIN_URL}", flush=True)
    await page.goto(HUMANSIGNAL_LOGIN_URL)
    
    print(f"[DEBUG] Đã load xong trang đăng nhập, đang điền thông tin...", flush=True)
    if not EMAIL or not PASSWORD:
        print("[FATAL ERROR] Thiếu EMAIL hoặc PASSWORD trong Environment Variables!", flush=True)
        return None
        
    await page.fill("input[name='email']", EMAIL)
    await page.fill("input[name='password']", PASSWORD)

    print("[DEBUG] Đang bấm nút đăng nhập...", flush=True)
    async with page.expect_navigation():
        await page.click("button[type='submit']")
    
    print("[DEBUG] Đăng nhập thành công! Trích xuất cookies...", flush=True)
    cookies = await context.cookies()
    cookie_dict = {c['name']: c['value'] for c in cookies}
    
    await browser.close()
    await p.stop()
    print("[DEBUG] Đã lấy được cookies và tắt Playwright hoàn toàn để giải phóng RAM!", flush=True)
    
    return cookie_dict

async def fetch_next_task(session: aiohttp.ClientSession):
    url = f"{HUMANSIGNAL_BASE_URL}/api/projects/213452/next/"
    try:
        async with session.get(url, timeout=10) as response:
            if response.status == 200:
                data = await response.json()
                if "id" in data:
                    print(f"[DEBUG-API] Lấy thành công next_task ID: {data['id']}", flush=True)
                    # Gọi thêm API GET /api/tasks/{task_id} để lấy đủ trường (hoặc mock response)
                    # Payload next/ tương đối đầy đủ, truyền luôn vào parser!
                    return data
            elif response.status == 404:
                print("[DEBUG-API] Đã hết task trong project!", flush=True)
            else:
                print(f"[ERROR] fetch_next_task lỗi {response.status}: {await response.text()}", flush=True)
    except Exception as e:
        print(f"[ERROR] Lỗi gọi API lấy task: {e}")
    return None

async def api_polling_loop():
    global action_queue
    action_queue = asyncio.Queue()
    
    log_memory("TRƯỚC KHI MỞ PHIÊN AIOHTTP")
    cookie_dict = await init_session()
    if not cookie_dict:
        return
        
    global global_task_state
    
    async with aiohttp.ClientSession(cookies=cookie_dict) as session:
        # Lấy task đầu tiên
        initial_task_data = await fetch_next_task(session)
        if initial_task_data:
            task_obj = response_parser(initial_task_data)
            await action_queue.put(("process_task", task_obj))
            
        last_action_time = time.time()
        current_task_start_time = time.time()
        
        while True:
            try:
                action, data = await action_queue.get()
                if action == "submit":
                    last_action_time = time.time()
                    print(f"[DEBUG] Bắt đầu gọi API Submit...", flush=True)
                    current_task = global_task_state.task
                    
                    # Submit using aiohttp
                    import copy, datetime, random, string
                    def generate_id(length=10):
                        return ''.join(random.choices(string.ascii_letters + string.digits, k=length))
                    
                    new_result = copy.deepcopy(current_task.original_result)
                    
                    if data.transcript:
                        new_result.append({"value": {"text": [data.transcript]}, "id": generate_id(), "from_name": "transcript", "to_name": "audio", "type": "textarea", "origin": "manual"})
                    if data.gender:
                        new_result.append({"value": {"choices": [data.gender]}, "id": generate_id(), "from_name": "gender", "to_name": "audio", "type": "choices", "origin": "manual"})
                    if data.topic:
                        new_result.append({"value": {"choices": [data.topic]}, "id": generate_id(), "from_name": "topic", "to_name": "audio", "type": "choices", "origin": "manual"})
                    if data.audio_issues:
                        new_result.append({"value": {"choices": data.audio_issues}, "id": generate_id(), "from_name": "audio_issues", "to_name": "audio", "type": "choices", "origin": "manual"})

                    lead_time_val = round(time.time() - current_task_start_time, 3)
                    payload = {
                        "lead_time": lead_time_val,
                        "result": new_result,
                        "draft_id": None,
                        "parent_prediction": current_task.parent_prediction_id,
                        "parent_annotation": None,
                        "started_at": datetime.datetime.utcnow().isoformat() + "Z",
                        "project": current_task.project_id
                    }
                    
                    url = f"{HUMANSIGNAL_BASE_URL}/api/tasks/{current_task.task_id}/annotations?project={current_task.project_id}"
                    print(f"[DEBUG] Đang gửi POST request tới: {url}", flush=True)
                    
                    async with session.post(url, json=payload) as response:
                        if response.status in (200, 201):
                            print("[DEBUG] Submit API thành công!", flush=True)
                        else:
                            print(f"[ERROR] Submit API thất bại, status = {response.status}", flush=True)
                            print(f"Response: {await response.text()}")

                    print(f"[DEBUG] Submit API xong mất {time.time() - last_action_time:.2f}s, tự động lấy task tiếp theo...", flush=True)
                    global_task_state = TaskState() # Reset UI
                    
                    # Fetch next task immediately!
                    next_task_data = await fetch_next_task(session)
                    if next_task_data:
                        task_obj = response_parser(next_task_data)
                        await action_queue.put(("process_task", task_obj))
                        
                elif action == "skip":
                    last_action_time = time.time()
                    print(f"[DEBUG] Bắt đầu gọi API Skip...", flush=True)
                    current_task = global_task_state.task
                    lead_time_val = round(time.time() - current_task_start_time, 3)
                    payload = {
                        "lead_time": lead_time_val,
                        "result": [],
                        "draft_id": None,
                        "parent_prediction": current_task.parent_prediction_id,
                        "parent_annotation": None,
                        "started_at": datetime.datetime.utcnow().isoformat() + "Z",
                        "project": current_task.project_id,
                        "was_cancelled": True
                    }
                    url = f"{HUMANSIGNAL_BASE_URL}/api/tasks/{current_task.task_id}/annotations?project={current_task.project_id}"
                    async with session.post(url, json=payload) as response:
                        if response.status in (200, 201):
                            print("[DEBUG] Skip API thành công!", flush=True)
                        else:
                            print(f"[ERROR] Skip API thất bại, status = {response.status}", flush=True)
                            
                    print(f"[DEBUG] Skip API xong mất {time.time() - last_action_time:.2f}s, tự động lấy task tiếp theo...", flush=True)
                    global_task_state = TaskState() # Reset UI
                    
                    # Fetch next task
                    next_task_data = await fetch_next_task(session)
                    if next_task_data:
                        task_obj = response_parser(next_task_data)
                        await action_queue.put(("process_task", task_obj))
                        
                elif action == "process_task":
                    current_task_start_time = time.time()
                    task_recv_time = time.time()
                    if last_action_time:
                        print(f"[DEBUG] Lấy Task mới sau {task_recv_time - last_action_time:.2f}s. Đang tải Audio...")
                    
                    print(f"[INFO] Processing task {data.task_id}")
                    
                    # TẢI VÀ XỬ LÝ AUDIO TRỰC TIẾP QUA AIOHTTP
                    try:
                        log_memory("TRƯỚC KHI TẢI AUDIO")
                        async with session.get(data.audio_url_path, timeout=15) as audio_resp:
                            if audio_resp.status != 200:
                                raise Exception(f"HTTP {audio_resp.status}")
                            raw_audio_bytes = await audio_resp.read()
                        print(f"[DEBUG-AUDIO] Tải xong file âm thanh gốc qua API: {len(raw_audio_bytes)} bytes", flush=True)
                        
                        def process_audio_fast(raw_bytes):
                            import wave, io, audioop
                            try:
                                with wave.open(io.BytesIO(raw_bytes), 'rb') as wav_in:
                                    n_channels = wav_in.getnchannels()
                                    sampwidth = wav_in.getsampwidth()
                                    framerate = wav_in.getframerate()
                                    n_frames = wav_in.getnframes()
                                    audio_data = wav_in.readframes(n_frames)

                                if n_channels == 2:
                                    audio_data = audioop.tomono(audio_data, sampwidth, 1, 1)
                                    n_channels = 1
                                    
                                if framerate != 16000:
                                    audio_data, _ = audioop.ratecv(audio_data, sampwidth, n_channels, framerate, 16000, None)
                                    framerate = 16000
                                    
                                audio_data = audioop.mul(audio_data, sampwidth, 1.778)

                                out_buffer = io.BytesIO()
                                with wave.open(out_buffer, 'wb') as wav_out:
                                    wav_out.setnchannels(n_channels)
                                    wav_out.setsampwidth(sampwidth)
                                    wav_out.setframerate(framerate)
                                    wav_out.writeframes(audio_data)

                                return out_buffer.getvalue()
                            except Exception as e:
                                print(f"[DEBUG-AUDIO] Cảnh báo lỗi wave ({e}), trả về file gốc!", flush=True)
                                return raw_bytes
                                
                        final_audio_bytes = await asyncio.to_thread(process_audio_fast, raw_audio_bytes)
                    except Exception as e:
                        print(f"[ERROR] Lỗi tải audio từ HumanSignal ({e})!", flush=True)
                        import wave, io
                        out_buffer = io.BytesIO()
                        with wave.open(out_buffer, 'wb') as wav_out:
                            wav_out.setnchannels(1); wav_out.setsampwidth(2); wav_out.setframerate(16000)
                            wav_out.writeframes(b'\x00' * 32000)
                        final_audio_bytes = out_buffer.getvalue()
                    
                    audio_cache.clear()
                    audio_cache[data.task_id] = final_audio_bytes
                    
                    import os
                    current_port = os.environ.get("PORT", "7860")
                    data.audio_data = f"http://127.0.0.1:{current_port}/api/audio/{data.task_id}"

                    # CẬP NHẬT STATE VÀ MỞ KHÓA UI CHO AUDIO (ZERO-WAIT)
                    global_task_state.task = data
                    global_task_state.gemini_response = None
                    print(f"[INFO] ĐÃ CÓ AUDIO, TRẢ VỀ FRONTEND NGAY LẬP TỨC (UI không bị khóa)!", flush=True)
                    task_received_event.set()
                    
                    # BƯỚC 2: CHẠY AI NGẦM (BACKGROUND)
                    async def run_gemini_bg(task_data, audio_bytes):
                        from gemini import get_response_async
                        try:
                            print(f"[DEBUG] Chạy AI ngầm cho task {task_data.task_id}...", flush=True)
                            annotation_resp = await get_response_async(task_data.task_id, audio_bytes, task_data.prediction)
                            global_task_state.gemini_response = annotation_resp
                            print(f"[DEBUG] Xử lý AI nền xong cho task {task_data.task_id}!", flush=True)
                        except Exception as e:
                            print(f"[ERROR] Lỗi xử lý AI nền: {e}")
                            from schemas import AnnotationResponse
                            global_task_state.gemini_response = AnnotationResponse(
                                transcript=task_data.prediction,
                                gender="Unknown", topic="Others", mc="No MC", error_alert="Lỗi chạy nền AI"
                            )
                        finally:
                            task_ready_event.set()
                            
                    asyncio.create_task(run_gemini_bg(data, final_audio_bytes))
            except Exception as e:
                print(f"[ERROR] aiohttp loop error: {e}", flush=True)

    print("[FATAL ERROR] Vòng lặp AIOHTTP đã bị thoát!!!", flush=True)

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    global action_queue
    global task_ready_event
    global task_received_event
    action_queue = asyncio.Queue()
    task_ready_event = asyncio.Event()
    task_received_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop_task = loop.create_task(api_polling_loop())
    yield
    loop_task.cancel()

app = FastAPI(lifespan=lifespan)

task_ready_event = None
task_received_event = None

@app.get("/api/task", response_model=TaskState)
async def get_task(wait_gemini: bool = False):
    if wait_gemini:
        await task_ready_event.wait()
    else:
        await task_received_event.wait()
    return global_task_state

@app.get("/api/audio/{task_id}")
async def get_audio(task_id: str):
    if task_id in audio_cache:
        return Response(content=audio_cache[task_id], media_type="audio/wav")
    raise HTTPException(status_code=404)

@app.post("/api/submit")
async def submit_task(task: SubmitTask):
    task_ready_event.clear()
    task_received_event.clear()
    await action_queue.put(("submit", task))
    return {"status": "queued"}

@app.post("/api/skip")
async def skip_task():
    task_ready_event.clear()
    task_received_event.clear()
    await action_queue.put(("skip", None))
    return {"status": "queued"}

import os
os.environ["GRADIO_ALLOWED_ORIGINS"] = "slacks-gag-exchange.ngrok-free.dev"
import gradio as gr
import frontend
print(f"================ FRONTEND LOADED FROM: {frontend.__file__} ================", flush=True)

# Mount the gradio app onto the root path of FastAPI
demo = frontend.build_ui()
app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 7860))
    
    # Khởi động Ngrok thông qua module riêng
    import ngrok_tunnel
    ngrok_tunnel.start_ngrok(port)

    uvicorn.run(app, host="0.0.0.0", port=port, loop="asyncio")
