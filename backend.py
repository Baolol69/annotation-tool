import sys
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, HTTPException, Response
import asyncio
import os
import io
import copy
import datetime
import random
import string
import time
import numpy as np
from gemini import get_response, AnnotationResponse
from schemas import CurrentTask, SubmitTask, TaskState
from dotenv import load_dotenv

load_dotenv()

EMAIL = os.getenv("EMAIL", "")
PASSWORD = os.getenv("PASSWORD", "")
PROJECT_ID=os.getenv("PROJECT_ID", 274318)

HUMANSIGNAL_LOGIN_URL = "https://app.humansignal.com/user/login/"
HUMANSIGNAL_PROJECT_URL = f"https://app.humansignal.com/projects/{PROJECT_ID}/labeling"
HUMANSIGNAL_BASE_URL = "https://app.humansignal.com"

def log_memory(stage: str):
    import sys
    try:
        import psutil
        import os
        process = psutil.Process(os.getpid())
        mem_info = process.memory_info()
        print(f"[MEMORY] {stage} | TÀI NGUYÊN PYTHON ĐANG DÙNG: {mem_info.rss / 1024 / 1024:.2f} MB", flush=True)
    except ImportError:
        try:
            if sys.platform == "linux":
                with open('/proc/self/status') as f:
                    for line in f:
                        if 'VmRSS' in line:
                            py_mem_mb = int(line.split()[1]) / 1024
                            print(f"[MEMORY] {stage} | TÀI NGUYÊN PYTHON ĐANG DÙNG: {py_mem_mb:.2f} MB", flush=True)
                            break
        except Exception:
            pass
    except Exception:
        pass

# Global state
global_task_state = TaskState()
action_queue = None  # Sẽ khởi tạo bên trong event loop
playwright_context = {}
audio_cache = {}
processed_tasks = set()




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



import time
import aiohttp

async def init_session():
    print("[DEBUG] Đang khởi tạo phiên đăng nhập trực tiếp qua API (Không dùng Playwright)...", flush=True)
    if not EMAIL or not PASSWORD:
        print("[FATAL ERROR] Thiếu EMAIL hoặc PASSWORD trong file .env!", flush=True)
        return None
        
    async with aiohttp.ClientSession() as login_session:
        login_url = f"{HUMANSIGNAL_BASE_URL}/user/login/"
        
        # Bước 1: Lấy CSRF Token
        async with login_session.get(login_url) as resp:
            html = await resp.text()
            
        csrftoken = ""
        for cookie in login_session.cookie_jar:
            if cookie.key in ("csrftoken", "csrf_token"):
                csrftoken = cookie.value
                break
                
        if not csrftoken:
            import re
            match = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', html)
            if match:
                csrftoken = match.group(1)
                
        # Bước 2: Gửi POST Request
        data = {
            "email": EMAIL,
            "password": PASSWORD,
            "csrfmiddlewaretoken": csrftoken
        }
        headers = {"Referer": login_url}
        
        async with login_session.post(login_url, data=data, headers=headers) as resp2:
            print(f"[DEBUG] Đăng nhập trả về HTTP {resp2.status}", flush=True)
            
        cookie_dict = {}
        for cookie in login_session.cookie_jar:
            cookie_dict[cookie.key] = cookie.value
            
        if "sessionid" in cookie_dict or "session" in cookie_dict:
            print("[DEBUG] Đăng nhập thành công! Đã lấy được cookies.", flush=True)
            return cookie_dict
            
        print(f"[ERROR] Đăng nhập thất bại. Cookies hiện tại: {cookie_dict}", flush=True)
        return cookie_dict

async def pagination_loop(session: aiohttp.ClientSession, project_id: str, prefetch_queue: asyncio.Queue):
    current_page = 1
    while True:
        url = f"{HUMANSIGNAL_BASE_URL}/api/tasks/?project={project_id}&page={current_page}&page_size=100"
        try:
            async with session.get(url, timeout=15) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    tasks = data.get("tasks", []) if isinstance(data, dict) else data
                    if not tasks:
                        print(f"[DEBUG-PAGINATION] Hết task ở trang {current_page}. Đợi 10s rồi quét lại từ đầu.", flush=True)
                        await asyncio.sleep(10)
                        current_page = 1
                        continue
                    
                    found_any = False
                    for t in tasks:
                        # Kiểm tra task chưa làm một cách triệt để
                        has_annotations = len(t.get("annotations", [])) > 0
                        has_drafts = len(t.get("drafts", [])) > 0
                        is_labeled = t.get("is_labeled", False)
                        
                        if not is_labeled and not has_annotations and not has_drafts:
                            task_obj = response_parser(t)
                            
                            cached_task = None
                            if 'db_pool' in globals() and db_pool:
                                try:
                                    async with db_pool.acquire() as conn:
                                        row = await conn.fetchrow('SELECT * FROM gemini_cache WHERE task_id = $1 AND project_id = $2', int(task_obj.task_id), int(project_id))
                                        if row:
                                            if row['status'] == 'ready':
                                                cached_task = row
                                            elif row['status'] in ('submitted', 'skipped'):
                                                continue # Bỏ qua luôn vì đã làm xong (nhưng API HumanSignal chưa kịp cập nhật)
                                except Exception as e:
                                    print(f"[ERROR] DB fetch lỗi: {e}")

                            found_any = True
                            if cached_task:
                                from schemas import AnnotationResponse
                                cached_resp = AnnotationResponse(
                                    transcript=cached_task['transcript'],
                                    gender=cached_task['gender'],
                                    topic=cached_task['topic'],
                                    mc=cached_task['mc'],
                                    error_alert=cached_task['error_alert'] or ""
                                )
                                await prefetch_queue.put((task_obj, cached_resp))
                                print(f"[DEBUG-PAGINATION] Đã chèn task {task_obj.task_id} (TỪ DB CACHE) vào Prefetch Queue", flush=True)
                            else:
                                await prefetch_queue.put((task_obj, None))
                                print(f"[DEBUG-PAGINATION] Đã chèn task {task_obj.task_id} vào Prefetch Queue", flush=True)
                    
                    if not found_any:
                        print(f"[DEBUG-PAGINATION] Trang {current_page} không có task nào mới, nhảy trang tiếp theo...", flush=True)
                    current_page += 1
                else:
                    print(f"[ERROR] Pagination lỗi {resp.status}: {await resp.text()}", flush=True)
                    await asyncio.sleep(5)
        except Exception as e:
            print(f"[ERROR] Lỗi gọi API lấy danh sách task: {e}")
            await asyncio.sleep(5)


async def background_worker_loop(session: aiohttp.ClientSession, prefetch_queue: asyncio.Queue, ready_queue: asyncio.Queue):
    while True:
        try:
            task_data, cached_resp = await prefetch_queue.get()
            print(f"[DEBUG-WORKER] Bắt đầu xử lý trước task {task_data.task_id}...", flush=True)
            
            # Tải audio
            async with session.get(task_data.audio_url_path, timeout=15) as audio_resp:
                if audio_resp.status != 200:
                    raise Exception(f"HTTP {audio_resp.status}")
                raw_audio_bytes = await audio_resp.read()
            
            # Xử lý nhanh audio (resample mono 16k)
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
            
            if cached_resp:
                annotation_resp = cached_resp
                print(f"[DEBUG-WORKER] Dùng kết quả Cache cho task {task_data.task_id}!", flush=True)
            else:
                # Gọi Gemini xử lý
                from gemini import get_response_async
                annotation_resp = await get_response_async(task_data.task_id, final_audio_bytes, task_data.prediction)
                
                # Lưu vào DB
                if 'db_pool' in globals() and db_pool:
                    try:
                        async with db_pool.acquire() as conn:
                            await conn.execute('''
                                INSERT INTO gemini_cache (task_id, project_id, transcript, gender, topic, mc, error_alert, status)
                                VALUES ($1, $2, $3, $4, $5, $6, $7, 'ready')
                                ON CONFLICT (task_id) DO UPDATE SET 
                                    transcript = EXCLUDED.transcript,
                                    gender = EXCLUDED.gender,
                                    topic = EXCLUDED.topic,
                                    mc = EXCLUDED.mc,
                                    error_alert = EXCLUDED.error_alert,
                                    status = 'ready'
                            ''', int(task_data.task_id), int(task_data.project_id) if task_data.project_id else int(os.environ.get("PROJECT_ID", "213452")), annotation_resp.transcript, annotation_resp.gender, annotation_resp.topic, annotation_resp.mc, annotation_resp.error_alert)
                    except Exception as e:
                        print(f"[ERROR] DB insert lỗi: {e}")
            
            # Đưa vào Ready Queue
            await ready_queue.put((task_data, final_audio_bytes, annotation_resp))
            print(f"[DEBUG-WORKER] Đã xử lý và đóng gói xong task {task_data.task_id}! Đẩy vào Ready Queue.", flush=True)
        except Exception as e:
            print(f"[ERROR] Worker loop lỗi: {e}", flush=True)

async def api_polling_loop():
    global action_queue
    action_queue = asyncio.Queue()
    
    log_memory("TRƯỚC KHI MỞ PHIÊN AIOHTTP")
    cookie_dict = await init_session()
    if not cookie_dict:
        return
        
    global global_task_state
    prefetch_queue = asyncio.Queue(maxsize=5)
    ready_queue = asyncio.Queue(maxsize=5)
    
    import os
    project_id = os.environ.get("PROJECT_ID", "213452")
    
    async with aiohttp.ClientSession(cookies=cookie_dict) as session:
        # Chạy ngầm 2 luồng
        asyncio.create_task(pagination_loop(session, project_id, prefetch_queue))
        asyncio.create_task(background_worker_loop(session, prefetch_queue, ready_queue))
        
        # Lệnh đầu tiên: bốc task cho UI
        await action_queue.put(("load_next_task", None))
        
        current_task_start_time = time.time()
        
        while True:
            try:
                action, data = await action_queue.get()
                if action == "load_next_task":
                    # Rút từ Ready Queue, nếu chưa có thì sẽ đợi (UI sẽ loading)
                    task_data, final_audio_bytes, annotation_resp = await ready_queue.get()
                    
                    import os
                    current_port = os.environ.get("PORT", "8000")
                    task_data.audio_data = f"http://127.0.0.1:{current_port}/api/audio/{task_data.task_id}"
                    
                    global_task_state.task = task_data
                    global_task_state.gemini_response = annotation_resp
                    audio_cache[task_data.task_id] = final_audio_bytes
                    current_task_start_time = time.time()
                    
                    # Báo hiệu UI cập nhật (chỉ cần YIELD 1 lần)
                    task_received_event.set()
                    
                elif action == "submit":
                    print(f"[DEBUG] Gửi lệnh Submit ngầm...", flush=True)
                    current_task = global_task_state.task
                    
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
                    
                    async def do_post(url, payload, task_id):
                        try:
                            async with session.post(url, json=payload) as response:
                                resp_text = await response.text()
                                if response.status in (200, 201) or (response.status == 400 and ("OVERLAP_REACHED" in resp_text or "already have an annotation" in resp_text)):
                                    print(f"[DEBUG] Submit API nền thành công (Task {task_id} đã được lưu hoặc trùng lặp)!", flush=True)
                                    if 'db_pool' in globals() and db_pool:
                                        try:
                                            async with db_pool.acquire() as conn:
                                                await conn.execute("UPDATE gemini_cache SET status = 'submitted' WHERE task_id = $1", int(task_id))
                                        except Exception as db_e:
                                            print(f"[ERROR] Lỗi update DB sau submit: {db_e}")
                                else:
                                    print(f"[ERROR] Submit API thất bại, status = {response.status} | Chi tiết: {resp_text}", flush=True)
                        except Exception as e:
                            print(f"[ERROR] Lỗi submit: {e}")
                            
                    asyncio.create_task(do_post(url, payload, current_task.task_id))
                    
                    global_task_state = TaskState() # Reset UI state
                    await action_queue.put(("load_next_task", None))
                    
                elif action == "skip":
                    print(f"[DEBUG] Gửi lệnh Skip ngầm...", flush=True)
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
                    
                    async def do_skip_post(url, payload, task_id):
                        try:
                            async with session.post(url, json=payload) as response:
                                if response.status in (200, 201):
                                    print("[DEBUG] Skip API nền thành công!", flush=True)
                                    if 'db_pool' in globals() and db_pool:
                                        try:
                                            async with db_pool.acquire() as conn:
                                                await conn.execute("UPDATE gemini_cache SET status = 'skipped' WHERE task_id = $1", int(task_id))
                                        except Exception as db_e:
                                            print(f"[ERROR] Lỗi update DB sau skip: {db_e}")
                                else:
                                    print(f"[ERROR] Skip API thất bại, status = {response.status}", flush=True)
                        except Exception as e:
                            print(f"[ERROR] Lỗi skip: {e}")
                            
                    asyncio.create_task(do_skip_post(url, payload, current_task.task_id))
                    
                    global_task_state = TaskState()
                    await action_queue.put(("load_next_task", None))
                    
            except Exception as e:
                print(f"[ERROR] aiohttp loop error: {e}", flush=True)

    print("[FATAL ERROR] Vòng lặp AIOHTTP đã bị thoát!!!", flush=True)

from contextlib import asynccontextmanager

db_pool = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global action_queue
    global task_ready_event
    global task_received_event
    global db_pool
    action_queue = asyncio.Queue()
    task_ready_event = asyncio.Event()
    task_received_event = asyncio.Event()
    
    import os
    db_uri = os.environ.get("DATABASE_URI")
    if db_uri:
        try:
            import asyncpg
            print("[DEBUG] Đang kết nối tới PostgreSQL...")
            db_pool = await asyncpg.create_pool(db_uri, min_size=1, max_size=3)
            async with db_pool.acquire() as conn:
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS gemini_cache (
                        task_id BIGINT PRIMARY KEY,
                        project_id BIGINT,
                        transcript TEXT,
                        gender VARCHAR(10),
                        topic VARCHAR(50),
                        mc VARCHAR(50),
                        error_alert TEXT,
                        status VARCHAR(20),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
            print("[DEBUG] Khởi tạo bảng gemini_cache thành công!")
        except Exception as e:
            print(f"[ERROR] Lỗi kết nối CSDL: {e}")
            db_pool = None
    else:
        print("[WARNING] Không tìm thấy DATABASE_URI!")

    loop = asyncio.get_running_loop()
    loop_task = loop.create_task(api_polling_loop())
    yield
    loop_task.cancel()
    if db_pool:
        await db_pool.close()

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
import gradio as gr
import frontend

# Mount the gradio app onto the root path of FastAPI
demo = frontend.build_ui()
app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, loop="asyncio")
