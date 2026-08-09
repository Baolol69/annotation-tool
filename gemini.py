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

SYSTEM_INSTRUCTION = """Nghe từng file audio, check và sửa transcript, gender (giới tính). Thực hiện label cho genre/topic (thể loại của audio). Mục tiêu của quá trình annotation là: 
a. Kiểm tra và chỉnh sửa lại transcript cho khớp với nội dung audio. 
b. Kiểm tra giới tính (gender) của người nói: Male: giọng nam, Female: giọng nữ, Unknown: không xác định được (ví dụ: giọng bị méo, có nhạc nền, hoặc nhiều người nói lẫn nhau mà không xác định được người nói chính). 
c. Gán nhãn thể loại nội dung (topic/genre) cho từng đoạn audio. Chọn một trong các nhãn sau cho mỗi file/đoạn audio: 

[News]
Mô tả: Audio có nội dung liên quan đến tin tức, thời sự, hoặc bản tin tổng hợp, thường có MC hoặc người dẫn chương trình đọc tin. 
Đặc trưng: 
- Có người đọc tin (MC, phát thanh viên) với giọng trang trọng. 
- Nội dung mang tính thông tin, cập nhật sự kiện. Có thể chứa thông tin về chính trị, kinh tế, xã hội, hoặc sự kiện thời sự. 
- Được biên tập sẵn (thường không có yếu tố trò chuyện tự nhiên). 
Ví dụ: Bản tin thời sự, chương trình đọc báo, điểm tin hàng ngày. ("Bản tin sáng nay, Bộ Y tế thông báo...", "Theo thông tin từ Sở Giao thông...") 

[Sport] 
Mô tả: Audio liên quan đến thể thao, bao gồm bình luận, phân tích, hoặc tường thuật các sự kiện thể thao. 
Đặc trưng: 
- Xuất hiện nhiều thuật ngữ chuyên ngành thể thao (ví dụ: "trận đấu", "hiệp 2", "bàn thắng", "vận động viên", "giải đấu", v.v.). 
- Giọng có thể phấn khích, cảm xúc mạnh nếu là bình luận trực tiếp. 
- Có thể là bình luận viên hoặc người hâm mộ nói về trận đấu, giải đấu, vận động viên, v.v. 
Ví dụ: Bình luận trận bóng đá, podcast thể thao, tin thể thao cuối ngày. ("Cầu thủ số 10 đã ghi bàn ở phút thứ 90...", "Chúng ta cùng phân tích chiến thuật của đội tuyển...") 

[Podcast / Talkshow] 
Mô tả: Audio là cuộc trò chuyện, chia sẻ, tâm sự, hoặc thảo luận giữa 1-3 người, thường mang tính cá nhân hoặc xã hội (không đọc tin tức). 
Đặc trưng: 
- Giọng nói tự nhiên, nhiều cảm xúc. 
- Có đối thoại qua lại hoặc chia sẻ tâm sự. 
- Chủ đề có thể về đời sống, tâm lý, công việc, quan điểm cá nhân. 
Ví dụ: Podcast chia sẻ của người nổi tiếng, talkshow phỏng vấn, trò chuyện tâm sự. ("Tôi nghĩ rằng ai cũng từng trải qua cảm giác đó…", "Khi làm việc nhóm, diệu quan trọng nhất là…") 

[Other] 
Mô tả: Audio không thuộc các thể loại trên. 
Ví dụ: Quảng cáo, phỏng vấn ngẫu nhiên trên đường, nội dung học tập, bài giảng, v.v. 

Các lưu ý quan trọng: 
1. Sửa lỗi chính tả ở phần transcribe nếu phát hiện sai sót giữa quá trình auto transcribe và audio (dạn sáng → rạng sáng, transcribe phải khớp với audio). Audio như thế nào thì transcript phải ghi giống như thế. 
2. Các chữ số chuyển về chữ viết (50 → năm mươi) tùy vào người đọc (đọc như thế nào  thì ghi y chang như thế). 
3. Các tiếng như "à/ừm/ờ/...", kiểu nói ngắt câu thì không cần ghi vào.  
4. Các ký tự đặc biệt chuyển về chữ viết (% → phần trăm) tùy vào người đọc. 
5. Những đoạn transcribe có dấu 3 chấm ở cuối câu, do speaker chưa nói hết vì thư viện pyannote cắt thì sẽ xóa đi (rất là… → rất là). Tức là phải xóa phần thừa, chỉ giữ nguyên phần thoại đã nghe được. 
6. Một số từ địa phương không nên phiên âm kiểu dịch nghĩa, ví dụ có miền sẽ có từ "mần", phiên âm dịch nghĩa ra là "làm", điều này không cần thiết và sẽ ảnh hưởng đến quá trình học của mô hình. Tương tự với các từ "chi", "rứa", "mô", "ta", "mi",… của các miền khác. Tức là giữ nguyên từ đó, không chỉnh sửa. 
7. Một số địa phương phát âm nhầm lẫn giữa "L" và "N" ("l" và"n") thì nên giữ nguyên phát âm. 
8.1. Loại bỏ các audio của MC, BLV,.. (người dẫn chương trình, bình luận viên), phóng viên,... bằng cách xóa bỏ đoạn transcribe sẵn của audio đó. Thường những người này sẽ có giọng đọc trang trọng, to, rõ ràng, nói tiếng Việt chuẩn. (GHI CHÚ MC, SKIP VÀ KHÔNG LÀM, KHÔNG SUBMIT) 
8.2. Nếu các audio của MC, BLV,.. (người dẫn chương trình, bình luận viên), phóng viên,... mà những người này vẫn còn giữ giọng đọc tại địa phương thì sẽ GHI CHÚ MC VÀ LÀM, SUBMIT NHƯ BÌNH THƯỜNG. 
9. Chỉnh sửa lại gender nếu phát hiện detect sai. 
10. Gán nhãn cho genre/topic của audio. 
11. Bỏ qua các đoạn audio nhiễu lớn, không thể nghe được giọng nói (nhạc nền lớn, môi trường xung quanh lấn át tiếng người nói). Giữ nguyên transcript và đánh các thông tin error alert, mô tả lỗi của audio đó. 
12. Các từ tiếng Anh, tiếng nước ngoài thì GIỮ NGUYÊN BẢN GỐC, không viết lại thành phiên âm. 
13. Một số lưu ý khác: 
i. Nếu audio chứa nhiều thể loại (ví dụ: đoạn tin thể thao trong chương trình thời sự), hãy chọn thể loại chính chiếm phần lớn nội dung. 
ii. Nếu có nhiều người nói, hãy xác định giới tính chiếm ưu thế hoặc để Unknown nếu không rõ. 
iii. Ghi chú lại bất kỳ trường hợp đặc biệt nào trong cột Error Alert.
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
