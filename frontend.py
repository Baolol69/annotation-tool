import gradio as gr
import requests
import time
from typing import List
import tempfile
import os

PORT = os.environ.get("PORT", "8000")
BACKEND_URL = f"http://127.0.0.1:{PORT}"

def log_memory(stage: str):
    import sys
    try:
        import psutil
        import os
        process = psutil.Process(os.getpid())
        mem_info = process.memory_info()
        print(f"[MEMORY] {stage} | TÀI NGUYÊN PYTHON ĐANG DÙNG: {mem_info.rss / 1024 / 1024:.2f} MB", file=sys.stderr, flush=True)
    except ImportError:
        try:
            if sys.platform == "linux":
                with open('/proc/self/status') as f:
                    for line in f:
                        if 'VmRSS' in line:
                            py_mem_mb = int(line.split()[1]) / 1024
                            print(f"[MEMORY] {stage} | TÀI NGUYÊN PYTHON ĐANG DÙNG: {py_mem_mb:.2f} MB", file=sys.stderr, flush=True)
                            break
        except Exception:
            pass
    except Exception:
        pass

current_task_id = None

def wait_for_next_task():
    # Phase 1: Chờ và tải Audio ngay lập tức
    task_data = None
    audio_filepath = None
    
    while True:
        try:
            print(f"[FRONTEND] Đang poll /api/task (Chờ Audio)...", flush=True)
            resp = requests.get(f"{BACKEND_URL}/api/task?wait_gemini=false", timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("task"):
                    task_data = data["task"]
                    audio_url = task_data["audio_data"]
                    try:
                        print(f"[FRONTEND] Bắt đầu tải audio từ {audio_url}...", flush=True)
                        audio_resp = requests.get(audio_url, timeout=5)
                        audio_bytes = audio_resp.content
                        import wave, io, numpy as np
                        with wave.open(io.BytesIO(audio_bytes), 'rb') as wf:
                            sr = wf.getframerate()
                            n_channels = wf.getnchannels()
                            sampwidth = wf.getsampwidth()
                            frames = wf.readframes(wf.getnframes())
                            dtype = np.int16 if sampwidth == 2 else np.int8
                            audio_arr = np.frombuffer(frames, dtype=dtype)
                            if n_channels == 2:
                                audio_arr = audio_arr.reshape(-1, 2)
                        audio_filepath = (sr, audio_arr)
                    except Exception as e:
                        print(f"[ERROR] Lỗi parse audio: {e}", flush=True)
                        audio_filepath = None

                    log_memory("FRONTEND: Giao diện vừa tải xong Audio")

                    info = task_data.get("task_info", {})
                    ref_id = info.get("ref_id", "N/A")
                    province = info.get("province", "N/A")
                    duration = info.get("duration", "N/A")
                    try:
                        import psutil
                        import os
                        process = psutil.Process(os.getpid())
                        mem_mb = process.memory_info().rss / 1024 / 1024
                        sys_percent = psutil.virtual_memory().percent
                        ram_str = f" | 💻 RAM: {mem_mb:.1f}MB (Sys {sys_percent}%)"
                    except Exception:
                        ram_str = ""
                        
                    info_text = f"### 📄 File: {ref_id} | 📍 Tỉnh: {province} | ⏱️ {duration}s{ram_str}"

                    gemini_resp = data.get("gemini_response") or {}
                    
                    # YIELD 1: Hiển thị Audio và kết quả AI ngay lập tức
                    print(f"[FRONTEND] YIELD: Trả Audio và AI về giao diện ngay lập tức...", flush=True)
                    yield (
                        info_text,
                        gr.Audio(value=audio_filepath, autoplay=True),
                        gemini_resp.get("transcript", ""),
                        task_data["region"],
                        gemini_resp.get("gender", "N/A"),
                        gemini_resp.get("topic", "Others")
                    )
                    break
        except requests.exceptions.ReadTimeout:
            pass
        except Exception as e:
            print(f"[ERROR] Lỗi khi poll /api/task: {e}", flush=True)
        time.sleep(1)

def build_ui():
    def submit(transcript:str, dialect:str, gender:str, topic:str, audio_issues:List[str]):
        global current_task_id
        
        log_memory("FRONTEND: Người dùng bấm Submit")
        
        payload = {
            "transcript": transcript,
            "gender": gender,
            "topic": topic,
            "audio_issues": audio_issues
        }
        try:
            print(f"[FRONTEND] Chuẩn bị gửi submit lên backend: {payload}", flush=True)
            resp = requests.post(f"{BACKEND_URL}/api/submit", json=payload, timeout=5)
            print(f"[FRONTEND] Backend trả về status: {resp.status_code}, nội dung: {resp.text}", flush=True)
            resp.raise_for_status()
            current_task_id = None # Reset so we wait for the next one
        except Exception as e:
            print(f"[ERROR] Submit failed: {e}", flush=True)
            yield gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip()
            return
        
        print(f"[FRONTEND] Bắt đầu đợi task tiếp theo...", flush=True)
        yield from wait_for_next_task()

    def skip():
        global current_task_id
        try:
            requests.post(f"{BACKEND_URL}/api/skip", timeout=5)
            current_task_id = None
        except Exception as e:
            print(f"[ERROR] Skip failed: {e}")
            yield gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip()
            return
            
        yield from wait_for_next_task()

    with gr.Blocks() as demo:
        gr.Markdown("# 🚀 PHIÊN BẢN ĐÃ CẬP NHẬT (KẾT NỐI CHỦ ĐỘNG)")
        metadata_view = gr.Markdown("### 📄 Đang chờ dữ liệu...")
        audio_player = gr.Audio(label="Audio Player", interactive=False, autoplay=True)

        transcript = gr.Textbox(
            label="Transcript", 
            lines=4, 
            placeholder="Gõ hoặc sửa đoạn hội thoại vào đây...",
            interactive=True
        )

        with gr.Row():
            dialect = gr.Radio(choices=["North", "Central", "South"], label="Dialect")
            gender = gr.Radio(choices=["M", "F", "N/A"], label="Gender")
            topic = gr.Radio(choices=["Sport", "News", "Podcast", "Others"], label="Topic")

        quality_issues = gr.CheckboxGroup(
            choices=["Too Noisy", "Low volume", "Overlapping", "Corrupted", "MC Voice"], 
            label="Audio Quality Issues"
        )

        submit_button = gr.Button("Submit", variant="primary")
        skip_button = gr.Button("Skip", variant="primary")

        outputs_list = [metadata_view, audio_player, transcript, dialect, gender, topic]

        # Trigger on load (Replaced with a manual button to prevent infinite loading on startup)
        connect_button = gr.Button("🔴 BẤM VÀO ĐÂY ĐỂ BẮT ĐẦU TẢI TASK", variant="primary")
        connect_button.click(fn=wait_for_next_task, inputs=None, outputs=outputs_list)
        
        # Trigger on click
        submit_button.click(fn=submit, inputs=[transcript, dialect, gender, topic, quality_issues], outputs=outputs_list)
        skip_button.click(fn=skip, inputs=[], outputs=outputs_list)

        return demo

if __name__ == "__main__":
    demo = build_ui()
    demo.launch(theme=gr.themes.Soft(), share=True, server_port=7861)
 