import gradio as gr
import requests
import time
from typing import List
import tempfile
import os

PORT = os.environ.get("PORT", "8000")
BACKEND_URL = f"http://127.0.0.1:{PORT}"

current_task_id = None

def wait_for_next_task():
    # Block and poll until a new task is found
    while True:
        try:
            # Long-polling: wait up to 60s for the backend event to trigger
            resp = requests.get(f"{BACKEND_URL}/api/task", timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("task") and data.get("gemini_response"):
                    audio_url = data["task"]["audio_data"]
                    try:
                        audio_resp = requests.get(audio_url, timeout=5)
                        temp_audio = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
                        temp_audio.write(audio_resp.content)
                        temp_audio.close()
                        audio_filepath = temp_audio.name
                    except Exception as e:
                        print(f"[ERROR] Failed to download audio: {e}")
                        audio_filepath = None

                    return (
                        audio_filepath,
                        data["task"]["prediction"],
                        data["gemini_response"]["transcript"],
                        data["task"]["region"],
                        data["gemini_response"]["gender"],
                        data["gemini_response"]["topic"]
                    )
        except Exception as e:
            pass
        time.sleep(1)

def build_ui():
    def submit(transcript:str, dialect:str, gender:str, topic:str, audio_issues:List[str]):
        global current_task_id
        payload = {
            "transcript": transcript,
            "gender": gender,
            "topic": topic,
            "audio_issues": audio_issues
        }
        try:
            requests.post(f"{BACKEND_URL}/api/submit", json=payload, timeout=5)
            current_task_id = None # Reset so we wait for the next one
        except Exception as e:
            print(f"[ERROR] Submit failed: {e}")
            return gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip()
        
        return wait_for_next_task()

    def skip():
        global current_task_id
        try:
            requests.post(f"{BACKEND_URL}/api/skip", timeout=5)
            current_task_id = None
        except Exception as e:
            print(f"[ERROR] Skip failed: {e}")
            return gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip()
            
        return wait_for_next_task()

    with gr.Blocks() as demo:
        audio_player = gr.Audio(label="Audio Player", interactive=False)
        zip_label = gr.Textbox(
            label="Zip Label", 
            value="ZIP_Vidu_Khong_Cho_Sua.zip",
            interactive=False 
        )

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

        outputs_list = [audio_player, zip_label, transcript, dialect, gender, topic]

        # Trigger on load
        demo.load(fn=wait_for_next_task, inputs=None, outputs=outputs_list)
        
        # Trigger on click
        submit_button.click(fn=submit, inputs=[transcript, dialect, gender, topic, quality_issues], outputs=outputs_list)
        skip_button.click(fn=skip, inputs=[], outputs=outputs_list)

        return demo

if __name__ == "__main__":
    demo = build_ui()
    demo.launch(theme=gr.themes.Soft(), share=True)