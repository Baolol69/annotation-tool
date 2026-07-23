import os
import json
import vertexai
from vertexai.generative_models import GenerativeModel
from google.oauth2 import service_account
from dotenv import load_dotenv

# Tải biến môi trường
load_dotenv()

# --- XỬ LÝ CREDENTIALS VÀ KHỞI TẠO CLIENT CHO VERTEX AI ---
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
    
    print(f"[*] Đang khởi tạo Vertex AI client (project={GCP_PROJECT_ID}, location={LOCATION})...")
    
    # Khởi tạo thư viện vertexai
    vertexai.init(
        project=GCP_PROJECT_ID,
        location=LOCATION,
        credentials=credentials
    )
    
    # --- LẤY DANH SÁCH MODELS ---
    print("[*] Đang truy vấn danh sách các models cho phép sử dụng trên Vertex AI...\n")
    try:
        # Danh sách các model phổ biến trên Vertex AI cần kiểm tra
        models_to_test = [
            "gemini-3.1-flash",
            "gemini-3.1-flash-lite",
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.6-flash",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.0-pro-exp-02-05",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite-preview-02-05",
            "gemini-1.5-pro",
            "gemini-1.5-flash",
            "gemini-1.5-flash-8b",
            "gemini-1.0-pro"
        ]
        
        print("="*70)
        print(" KIỂM TRA QUYỀN TRUY CẬP CÁC MODEL GEMINI TRÊN VERTEX AI ")
        print("="*70)
        
        available_models = []
        
        for model_name in models_to_test:
            print(f"[*] Đang thử kết nối model: {model_name}...", end=" ")
            try:
                # Gửi 1 prompt cực nhỏ để test bằng vertexai
                model = GenerativeModel(model_name)
                response = model.generate_content("Hi")
                print(f"[OK] - Khả dụng")
                available_models.append(model_name)
            except Exception as e:
                err_msg = str(e)
                if "404" in err_msg or "not found" in err_msg.lower():
                    print(f"[-] [LỖI] Model không tồn tại hoặc chưa hỗ trợ ở location {LOCATION}.")
                elif "403" in err_msg or "permission" in err_msg.lower():
                    print(f"[-] [LỖI] Không có quyền truy cập.")
                else:
                    # In ra lỗi ngắn gọn
                    print(f"[-] [LỖI] {err_msg[:60]}...")
                    
        print("\n" + "="*70)
        print(f"[*] TỔNG KẾT: Có {len(available_models)}/{len(models_to_test)} models khả dụng để bạn dùng:")
        for m in available_models:
            print(f"    - {m}")
        print("="*70)

    except Exception as e:
        print(f"[-] [LỖI] Đã xảy ra lỗi không xác định: {e}")
else:
    print("[-] [FATAL ERROR] Thiếu biến môi trường GOOGLE_APPLICATION_CREDENTIALS_JSON!")
