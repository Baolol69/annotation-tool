import array
import json
import os
import wave
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from playwright.sync_api import Page, Response, sync_playwright

from gemini import get_response

# ==========================================
# 1. CẤU HÌNH & HẰNG SỐ
# ==========================================
load_dotenv()

EMAIL = os.getenv("EMAIL", "")
PASSWORD = os.getenv("PASSWORD", "")

HUMANSIGNAL_LOGIN_URL = "https://app.humansignal.com/user/login/"
HUMANSIGNAL_PROJECT_URL = "https://app.humansignal.com/projects/213452/labeling"
HUMANSIGNAL_BASE_URL = "https://app.humansignal.com"

# Bảng ánh xạ giá trị nhãn sang name/value trên form Playwright
DIALECT_MAPPING = {
    "North": "North",
    "South": "South",
    "Central": "Central",
}

GENDER_MAPPING = {
    "Male": "M",
    "Female": "F",
    "Unknown": "N/A",
}

TOPIC_MAPPING = {
    "News": "News",
    "Sport": "Sport",
    "Podcast": "Podcast",
    "Speech": "Others",
    "Others": "Others",
}


# ==========================================
# 2. HÀM TIỀN XỬ LÝ & TRÍCH XUẤT DỮ LIỆU
# ==========================================
def extract_initial_transcript(response_body: Dict[str, Any]) -> str:
    """Trích xuất chuỗi transcript ban đầu từ JSON payload của HumanSignal an toàn, tránh KeyError/IndexError."""
    try:
        predictions = response_body.get("predictions")
        if not predictions or not isinstance(predictions, list):
            return ""
        result = predictions[0].get("result")
        if not result or not isinstance(result, list):
            return ""
        value = result[0].get("value", {})
        text_list = value.get("text")
        if not text_list or not isinstance(text_list, list):
            return ""
        return str(text_list[0]).strip()
    except (IndexError, AttributeError, TypeError):
        return ""


def audio_preprocessing(file_path: str = "temp_audio.wav", target_sr: int = 16000) -> None:
    """
    Tiền xử lý file âm thanh WAV trước khi gửi lên Gemini:
    - Chuyển từ Stereo (2 kênh) sang Mono (1 kênh).
    - Giảm tần số lấy mẫu (Downsample) xuống 16kHz (chuẩn nhận dạng giọng nói).
    - Giúp giảm tới 80-85% dung lượng file, upload cực nhanh và tiết kiệm tài nguyên.
    """
    try:
        if not os.path.exists(file_path):
            return
        orig_size = os.path.getsize(file_path)

        with wave.open(file_path, "rb") as wf:
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            n_frames = wf.getnframes()

            # Chỉ xử lý nhanh nếu là định dạng chuẩn PCM 16-bit
            if sampwidth != 2:
                return
            # Nếu audio đã là Mono và tần số lấy mẫu <= target_sr thì không cần nén thêm
            if n_channels == 1 and framerate <= target_sr:
                return

            raw_data = wf.readframes(n_frames)
            arr = array.array("h", raw_data)

        # 1. Chuyển đổi Stereo (hoặc đa kênh) -> Mono
        if n_channels == 2:
            left = arr[0::2]
            right = arr[1::2]
            mono_arr = array.array("h", ((l + r) // 2 for l, r in zip(left, right)))
        elif n_channels > 2:
            mono_arr = array.array("h", arr[0::n_channels])
        else:
            mono_arr = arr

        # 2. Giảm tần số lấy mẫu (Downsample) xuống target_sr (16000Hz)
        if framerate > target_sr:
            ratio = framerate / target_sr
            new_len = int(len(mono_arr) / ratio)
            resampled_arr = array.array("h", (mono_arr[int(i * ratio)] for i in range(new_len)))
            new_framerate = target_sr
        else:
            resampled_arr = mono_arr
            new_framerate = framerate

        # Ghi lại file WAV đã được nén tối ưu
        temp_out = file_path + ".proc.wav"
        with wave.open(temp_out, "wb") as wf_out:
            wf_out.setnchannels(1)  # Mono (1 kênh)
            wf_out.setsampwidth(2)  # 16-bit PCM
            wf_out.setframerate(new_framerate)
            wf_out.writeframes(resampled_arr.tobytes())

        if os.path.exists(temp_out):
            os.replace(temp_out, file_path)
            new_size = os.path.getsize(file_path)
            percent = (1 - new_size / orig_size) * 100 if orig_size > 0 else 0
            print(f"-> [Preprocess] Đã tối ưu audio: {orig_size/1024:.1f} KB -> {new_size/1024:.1f} KB (giảm {percent:.0f}%) | Mono {new_framerate}Hz")
    except Exception as e:
        # Nếu có lỗi (file nén đặc biệt, phi chuẩn...), giữ nguyên audio gốc để không ngắt quãng tiến trình
        print(f"-> [Preprocess] Giữ nguyên audio gốc (bỏ qua nén do: {e})")


# ==========================================
# 3. THAO TÁC UI & GÁN NHÃN TRÊN PLAYWRIGHT
# ==========================================
def fill_form(
    page: Page,
    transcript: str,
    dialect: str,
    gender: str,
    topic: str,
    issues: str,
    mc: str,
) -> None:
    """Điền kết quả gán nhãn (transcript, vùng miền, giới tính, chủ đề, giọng MC) vào form trên trang web."""
    try:
        # Nhập transcript và nhấn nút thêm/cập nhật
        page.fill("textarea[name='transcript']", transcript)
        #page.get_by_test_id("textarea-add-button").click()

        # Chọn vùng miền (Dialect)
        if dialect in DIALECT_MAPPING:
            page.locator(f'input[name="{DIALECT_MAPPING[dialect]}"]').check()

        # Chọn giới tính (Gender)
        if gender in GENDER_MAPPING:
            page.locator(f'input[name="{GENDER_MAPPING[gender]}"]').check()

        # Chọn chủ đề (Topic)
        if topic in TOPIC_MAPPING:
            page.locator(f'input[name="{TOPIC_MAPPING[topic]}"]').check()

        # Chọn tùy chọn MC Voice nếu có
        if mc == "MC":
            page.locator('input[name="MC Voice"]').check()

    except Exception as e:
        print(f"[-] [fill_form] Lỗi khi thao tác form trên trình duyệt: {e}")


def play_audio(page: Page) -> None:
    """Đợi giao diện tải xong thời gian audio và tự động kích hoạt nút Play."""
    try:
        page.wait_for_function(
            """
            () => {
                const endTimeInput = document.querySelector('[data-testid="timebox-end-time"] input');
                if (!endTimeInput) return false;
                const val = endTimeInput.value || "";
                return val !== "" && val !== "00:00:00:000" && !val.includes("_");
            }
            """,
            timeout=20000,
        )
        print("[+] Đang phát audio...")
        page.locator("[data-testid='playback-button:play']").click()
    except Exception as e:
        print(f"[-] [play_audio] Không thể tự động phát audio do timeout hoặc lỗi: {e}")


def handle_response(page: Page, response: Response) -> None:
    """Lắng nghe và xử lý các phản hồi mạng từ server HumanSignal để tải audio và gửi cho Gemini."""
    is_next_task = "api/dm/actions?id=next_task" in response.url
    is_get_task = "api/tasks/" in response.url and response.request.method == "GET"

    if not (is_next_task or is_get_task) or response.status != 200:
        return

    temp_file = f"temp_audio_{os.getpid()}.wav"
    try:
        response_body = response.json()
        task_id = response_body.get("id")
        task_data = response_body.get("data", {})
        transcript = extract_initial_transcript(response_body)
        region = task_data.get("region", "Unknown")
        audio_url_path = task_data.get("audio", "")

        if not transcript or not audio_url_path:
            return

        print(f"\n[+] TASK MỚI: {task_id} | Vùng miền: {region}")
        print(f"[+] Bản text AI cũ: {transcript}")
        print("-> Đang tải audio từ hệ thống...")

        full_audio_url = f"{HUMANSIGNAL_BASE_URL}{audio_url_path}"

        # Dùng session của Playwright để tải file vượt tường lửa đăng nhập
        audio_response = page.context.request.get(full_audio_url)
        audio_bytes = audio_response.body()

        # Lưu tạm ra máy
        with open(temp_file, "wb") as f:
            f.write(audio_bytes)
        print("Download audio thành công.")

        # Tiền xử lý audio (mono, 16kHz)
        audio_preprocessing(temp_file)

        # Gửi sang Gemini và xử lý JSON trả về
        gemini_response = get_response(temp_file, transcript)
        response_obj = json.loads(gemini_response)

        transcript_ai = response_obj.get("transcript", transcript)
        gender_ai = response_obj.get("gender", "Unknown")
        topic_ai = response_obj.get("topic", "Others")
        error_alert_ai = response_obj.get("error_alert", "")
        mc_ai = response_obj.get("mc", "No MC")

        if error_alert_ai:
            print(f"[!] Ghi chú từ AI: {error_alert_ai}")

        fill_form(
            page,
            transcript_ai,
            region,
            gender_ai,
            topic_ai,
            error_alert_ai,
            mc_ai,
        )
        play_audio(page)

    except Exception as e:
        print(f"[-] Lỗi xử lý dữ liệu: {e}")
    finally:
        # Đảm bảo luôn dọn dẹp file âm thanh tạm trên máy
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass


# ==========================================
# 4. KHỞI TẠO BROWSERLESS & FASTAPI WEBAPP
# ==========================================
# 4. KHỞI TẠO TRÌNH DUYỆT CỤC BỘ & FASTAPI
# ==========================================
def start_local_browser(p: Any) -> Page:
    """Khởi chạy trình duyệt Chromium cục bộ ngay trên máy (headless=False)."""
    print("[+] Đang mở trình duyệt Chromium cục bộ trên màn hình...")
    browser = p.chromium.launch(
        headless=False,
        args=[
            "--start-maximized",
            "--disable-blink-features=AutomationControlled",
        ]
    )
    context = browser.new_context(no_viewport=True)
    page = context.new_page()
    print("[+] Đã khởi tạo trình duyệt thành công!")
    return page


def main() -> None:
    """Luồng chính: Khởi tạo trình duyệt cục bộ, đăng nhập HumanSignal và chờ thao tác."""
    with sync_playwright() as p:
        page = start_local_browser(p)
        # Đăng ký lắng nghe mạng
        page.on("response", lambda x: handle_response(page, x))

        # Chạy kịch bản UI
        print("Đang đăng nhập...")
        page.goto(HUMANSIGNAL_LOGIN_URL)
        page.fill("input[name='email']", EMAIL)
        page.fill("input[name='password']", PASSWORD)

        with page.expect_navigation():
            page.click("button[type='submit']")

        print("Đăng nhập thành công! Đang vào dự án...")
        page.goto(HUMANSIGNAL_PROJECT_URL)

        print(">>> Hệ thống đang chạy. Hãy thao tác trực tiếp trên cửa sổ trình duyệt Chromium...")
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass


if __name__ == "__main__":
    print("\n" + "=" * 65)
    print(">>> KHỞI ĐỘNG CÔNG CỤ GÁN NHÃN TỰ ĐỘNG (PLAYWRIGHT CLI) <<<")
    print("=" * 65 + "\n")
    main()
