import os
import json
import hashlib
import re
import asyncio

from google import genai
from google.genai import types
from google.oauth2 import service_account
from dotenv import load_dotenv
from schemas import AnnotationResponse

# Tải biến môi trường
load_dotenv()

# --- 1. XỬ LÝ CREDENTIALS VÀ KHỞI TẠO CLIENT CHO VERTEX AI ---
# Sử dụng GCP_PROJECT_ID để tránh trùng lặp với PROJECT_ID của HumanSignal
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID")
LOCATION = os.getenv("GCP_LOCATION", "us-central1")
credentials_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")

if credentials_json:
    # Xóa dấu nháy đơn nếu đọc từ file .env cục bộ
    if credentials_json.startswith("'") and credentials_json.endswith("'"):
        credentials_json = credentials_json[1:-1]

    # Tải thông tin JSON và ép buộc scope Cloud Platform
    service_account_info = json.loads(credentials_json)
    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )

    client = genai.Client(
        vertexai=True, 
        project=GCP_PROJECT_ID, 
        location=LOCATION,
        credentials=credentials
    )
else:
    raise RuntimeError("[-] [FATAL ERROR] Thiếu biến môi trường GOOGLE_APPLICATION_CREDENTIALS_JSON trong .env!")

# --- 2. CẤU HÌNH MODEL VÀ PROMPT ---
MODEL = os.getenv("GEMINI_MODEL")

SYSTEM_INSTRUCTION = """Nghe audio và sửa transcript nháp theo luật:
1. TRANSCRIPT:
- Khớp 100% audio. Sửa lỗi chính tả.
- Số (50) & ký tự (%) -> chữ viết (năm mươi, phần trăm).
- Bỏ từ đệm (à, ừm, ờ).
- Xóa dấu "..." ở cuối, chỉ giữ phần thoại nghe được.
- GIỮ NGUYÊN: Từ địa phương (mần, rứa, chi, mô), lỗi ngọng (L/N), tiếng nước ngoài (không phiên âm, đặc biệt từ Ukraine KHÔNG GHI Ukraina).
- Vấp lặp: Giữ 1 từ nếu vô nghĩa (thì thì->thì), giữ nguyên nếu có nghĩa (năm năm).
- Nhiễu quá lớn/Không nghe được: Giữ nguyên transcript nháp, ghi lỗi vào error_alert.

2. MC/BLV/PHÓNG VIÊN: Đánh dấu MC

3. GENDER: M(nam), F(nữ), N/A(méo tiếng/không rõ người chính).
4. TOPIC:
- News: Tin tức, thời sự (thường có MC).
- Sport: Thể thao, bình luận, phấn khích.
- Podcast: Trò chuyện 1-3 người, tâm sự đời sống.
- Others: Quảng cáo, phỏng vấn đường phố, bài giảng...
"""

PROMPT_TEMPLATE = SYSTEM_INSTRUCTION

# Pre-created Config (Cached tại bộ nhớ khi khởi tạo module):
CACHED_CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM_INSTRUCTION,
    response_mime_type="application/json",
    response_schema=AnnotationResponse,
    temperature=0.1
)

# Local Memory Cache: Lưu kết quả JSON đã xử lý
LOCAL_RESPONSE_CACHE = {}

def get_response(task_id, audio_bytes, transcript) -> AnnotationResponse:
    # Hàm đồng bộ (nếu cần dùng ở chỗ khác)
    pass

# --- 3. HÀM GỌI API XỬ LÝ CHÍNH ---
async def get_response_async(task_id, audio_bytes, transcript) -> AnnotationResponse:
    cache_key = hashlib.md5(f"{task_id}".encode('utf-8')).hexdigest()
    if cache_key in LOCAL_RESPONSE_CACHE:
        print(f"-> [Gemini Cache] Trả về kết quả lập tức từ bộ nhớ đệm cho task {task_id}!")
        cached_text = LOCAL_RESPONSE_CACHE[cache_key]
        return AnnotationResponse(**json.loads(cached_text))

    audio_part = types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav")
    prompt = f'Hãy nghe file âm thanh đính kèm và rà soát đoạn transcript nháp sau đây:\n"{transcript}"'
    print(f"-> [Vertex AI] Đang gửi yêu cầu xử lý tới model {MODEL} cho task {task_id}...", flush=True)

    try:
        max_retries = 4
        for attempt in range(max_retries):
            try:
                # Chạy bằng synchronous client bên trong to_thread để chống treo Event Loop
                def sync_call():
                    return client.models.generate_content(
                        model=MODEL,
                        contents=[audio_part, prompt],
                        config=CACHED_CONFIG
                    )

                response_gemini = await asyncio.wait_for(
                    asyncio.to_thread(sync_call),
                    timeout=30.0 # Tăng timeout lên 30s phòng trường hợp file audio nặng
                )
                break  # Thành công thì thoát vòng lặp retry
            except Exception as api_err:
                err_str = str(api_err)
                is_overloaded = any(code in err_str for code in ["503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "timeout", "Connection", "TimeoutError"])
                if is_overloaded or isinstance(api_err, asyncio.TimeoutError):
                    wait_time = (2 ** attempt) * 3 
                    print(f"[-] [Vertex AI] Server đang tải cao hoặc bận ({err_str[:60]}...). Tự động thử lại lần {attempt + 1}/{max_retries - 1} sau {wait_time}s...", flush=True)
                    await asyncio.sleep(wait_time)
                else:
                    raise api_err

        print(f"-> [Vertex AI] Nhận kết quả thành công từ AI cho task {task_id}!", flush=True)
        if hasattr(response_gemini, "usage_metadata") and response_gemini.usage_metadata:
            usage = response_gemini.usage_metadata
            print(f"-> [Token Usage] Prompt: {usage.prompt_token_count} | Output: {usage.candidates_token_count} | Total: {usage.total_token_count}", flush=True)
            
        text = response_gemini.text.strip()

        # Loại bỏ markdown code block nếu model trả về thừa
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        # Trích xuất chính xác đối tượng JSON
        start_brace = text.find("{")
        end_brace = text.rfind("}")
        if start_brace != -1 and end_brace != -1 and end_brace > start_brace:
            text = text[start_brace:end_brace+1]

        # Kiểm tra JSON hợp lệ và parse
        try:
            json.loads(text)
        except Exception as e:
            print(f"[-] [Vertex AI] Cảnh báo JSON trả về bị lỗi cú pháp ({e}). Tự động fallback để bảo vệ luồng trình duyệt...", flush=True)
            fallback_obj = {
                "transcript": transcript,
                "gender": "Unknown",
                "topic": "Others",
                "mc": "No MC",
                "error_alert": "AI trả về JSON không hợp lệ"
            }
            text = json.dumps(fallback_obj, ensure_ascii=False)

        # Lưu vào Local Cache
        LOCAL_RESPONSE_CACHE[cache_key] = text
        print(f"[Vertex AI JSON Kết quả]:\n{text}", flush=True)
        return AnnotationResponse(**json.loads(text))
        
    except Exception as total_err:
        # Bảo vệ tối đa: Nếu gọi API thất bại hết các lần thử lại
        print(f"[-] [Vertex AI] Không thể lấy kết quả AI sau các lần thử lại ({total_err}). Tự động fallback để không gián đoạn Playwright...", flush=True)
        fallback_obj = {
            "transcript": transcript,
            "gender": "Unknown",
            "topic": "Others",
            "mc": "No MC",
            "error_alert": f"Lỗi kết nối AI: {str(total_err)[:50]}"
        }
        return AnnotationResponse(**fallback_obj)

# --- 4. TEST SCRIPT (CHẠY ĐỘC LẬP) ---
if __name__ == "__main__":
    async def test_main():
        print("="*60)
        print("[*] ĐANG CHẠY TEST VERTEX AI (GEMINI)")
        print(f"[*] Model cấu hình: {MODEL}")
        print("="*60)
        
        # 1. Tạo một file audio giả (1 giây im lặng) để test kết nối
        import io
        import wave
        
        print("[1] Đang tạo audio giả (1 giây im lặng)...")
        out_buffer = io.BytesIO()
        with wave.open(out_buffer, 'wb') as wav_out:
            wav_out.setnchannels(1)
            wav_out.setsampwidth(2)
            wav_out.setframerate(16000)
            wav_out.writeframes(b'\x00\x00' * 16000)
        dummy_audio = out_buffer.getvalue()
        
        # 2. Tạo một đoạn transcript nháp
        transcript = "trận đấu này blv thấy quá hay"
        print(f"[2] Transcript nháp đầu vào: '{transcript}'")
        print("\n[3] Gửi yêu cầu lên Vertex AI...")
        
        # 3. Gọi hàm xử lý
        try:
            resp = await get_response_async("TEST_LOCAL_001", dummy_audio, transcript)
            
            print("\n" + "="*60)
            print("[KẾT QUẢ TỪ AI]")
            print(f"- Transcript: {resp.transcript}")
            print(f"- Gender    : {resp.gender}")
            print(f"- Topic     : {resp.topic}")
            print(f"- MC        : {resp.mc}")
            print(f"- Error/Alert: {resp.error_alert}")
            print("="*60)
        except Exception as e:
            print(f"\n[-] [LỖI KHÔNG MONG MUỐN]: {e}")

    asyncio.run(test_main())
