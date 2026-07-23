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
    print("[-] [FATAL ERROR] Thiếu biến môi trường GOOGLE_APPLICATION_CREDENTIALS_JSON trong gemini.py!", flush=True)
    # Khởi tạo mặc định để tránh lỗi syntax, dù có thể sẽ lỗi 401/403 khi chạy thực tế
    client = genai.Client(vertexai=True, project=GCP_PROJECT_ID, location=LOCATION)

# --- 2. CẤU HÌNH MODEL VÀ PROMPT ---
MODEL = 'gemini-3.5-flash-lite'

# Tách toàn bộ bộ quy tắc cố định sang SYSTEM_INSTRUCTION để tận dụng Automatic Prefix Caching
SYSTEM_INSTRUCTION = """
Bạn là một chuyên gia gán nhãn và kiểm định dữ liệu giọng nói (Audio Annotator/QA). Hãy nghe file âm thanh đính kèm và rà soát đoạn transcript nháp theo bộ quy tắc sau:

1. CHỈNH SỬA TRANSCRIPT (VĂN BẢN GỠ BĂNG):
- Khớp với audio: Sửa lỗi chính tả để transcript khớp hoàn toàn với những gì phát ra trong audio.
- Xử lý số và ký tự: Chuyển các chữ số (ví dụ: 50 -> năm mươi) và ký tự đặc biệt (ví dụ: % -> phần trăm) thành chữ viết tùy theo cách người nói đọc.
- Bỏ từ đệm: Không ghi vào transcript các tiếng ngắt câu, ậm ừ như “à, ùm, ờ, ...”.
- Xóa dấu cắt câu: Nếu cuối đoạn có dấu 3 chấm "..." do bị cắt ngang, hãy xóa phần thừa đó đi và chỉ giữ lại đúng phần thoại nghe được.
- Từ địa phương & Sai ngọng: Tuyệt đối giữ nguyên các từ địa phương (như mần, chỉ, rứa, mô, ta, mi) mà không dịch nghĩa. Nếu người nói phát âm nhầm lẫn giữa “L” và “N”, phải giữ nguyên cách phát âm đó.
- Từ nước ngoài: Giữ nguyên bản gốc của từ tiếng Anh/nước ngoài, không được viết thành phiên âm tiếng Việt.
- Nói lặp từ do vấp: Nếu người nói bị vấp, lặp lại một từ vô nghĩa (ví dụ: "thì thì thì...") thì chỉ ghi nhận 1 từ ("thì"). Chú ý phân biệt và giữ nguyên nếu việc lặp từ có ý nghĩa ngữ pháp hoặc cấu trúc câu (ví dụ: "chuẩn bị kỹ kỹ thuật...").
- BẢO TỒN TỪ Ở MÉP CÂU: Nếu bản nháp (prediction) dự đoán được các từ ở phần đầu hoặc phần đuôi của audio, hãy đặc biệt chú ý lắng nghe để giữ lại các từ này trong transcript (chỉ xóa khi chắc chắn đó là tiếng ồn hoặc không có phát âm). Tuyệt đối không tự ý cắt xén nếu audio có âm thanh mấp máy môi hoặc bị lướt.

2. KIỂM TRA GIỚI TÍNH (GENDER):
- Xác định giới tính là M (giọng nam), F (giọng nữ), hoặc N/A (Unknown). 
- Chọn Unknown nếu giọng bị méo, có nhạc nền lấn át, hoặc nhiều người nói mà không xác định được người chính. 
- Nếu có nhiều người nói, hãy xác định giới tính chiếm ưu thế.

3. GÁN NHÃN THỂ LOẠI (TOPIC/GENRE):
- Nếu audio chứa nhiều thể loại, chọn thể loại chính chiếm phần lớn nội dung. CHỈ ĐƯỢC CHỌN 1 TRONG 4 NHÃN SAU:
  + News: Tin tức, thời sự có người đọc tin (MC) với giọng trang trọng, biên tập sẵn.
  + Sport: Bình luận, phân tích thể thao chứa thuật ngữ chuyên ngành (trận đấu, bàn thắng) và giọng phấn khích.
  + Podcast: Cuộc trò chuyện tự nhiên, chia sẻ tâm sự giữa 1-3 người.
  + Others: Quảng cáo, bài phát biểu, phỏng vấn ngẫu nhiên, và các nội dung khác.

4. ĐÁNH GIÁ CHẤT LƯỢNG AUDIO:
- Nếu các audio của MC, BLV,.. (người dẫn chương trình, bình luận viên), phóng viên,... : GHI CHÚ MC.
- Nếu audio ngôn ngữ khác hoặc nhạc: Ghi chú trong transcript và không xử lý.

VUI LÒNG TRẢ VỀ KẾT QUẢ ĐÚNG THEO ĐỊNH DẠNG JSON SAU (Không kèm markdown code block, chỉ xuất JSON thô):
{
  "transcript": "Đoạn text đã được sửa chuẩn theo luật",
  "gender": "M / F / N/A",
  "topic": "News / Sport / Podcast / Others",
  "mc": "MC / No MC",
  "error_alert": "Ghi chú các lỗi nhiễu âm thanh (Nếu không có gì đặc biệt thì để rỗng)."
}
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
        text = response_gemini.text.strip()

        # Loại bỏ markdown code block nếu model trả về thừa
        if "```" in text:
            start = text.find("```")
            first_newline = text.find("\n", start)
            if first_newline != -1:
                end = text.rfind("```")
                if end > first_newline:
                    text = text[first_newline+1:end].strip()

        # Trích xuất chính xác đối tượng JSON
        start_brace = text.find("{")
        if start_brace != -1:
            try:
                decoder = json.JSONDecoder()
                obj, end_idx = decoder.raw_decode(text, start_brace)
                text = text[start_brace:end_idx]
            except Exception as e:
                cleaned_text = re.sub(r'"\s*\n+\s*"(\s*[,}])', r'"\1', text[start_brace:])
                try:
                    obj, end_idx = decoder.raw_decode(cleaned_text, 0)
                    text = cleaned_text[:end_idx]
                except Exception:
                    end_brace = text.rfind("}")
                    if end_brace > start_brace:
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
