# 🚀 Hướng dẫn Triển khai lên Render.com (Miễn phí)

Hệ thống của bạn đã được tối ưu siêu nhẹ và chỉ dùng 1 cổng mạng duy nhất cho cả API lẫn Giao diện (FastAPI + Gradio). Điều này biến **Render.com** trở thành nền tảng hoàn hảo nhất để đẩy code lên!

Dưới đây là các bước cực kỳ đơn giản để host ứng dụng của bạn lên Render:

## 1. Đẩy code lên GitHub
Render triển khai ứng dụng thông qua GitHub. Bạn cần:
1. Tạo một tài khoản GitHub (nếu chưa có).
2. Tạo một Repository (kho lưu trữ) mới ở chế độ **Private**.
3. Upload toàn bộ code này lên Repository đó (Bạn không cần đẩy thư mục `.venv` hay file `ngrok.exe` lên).

## 2. Tạo Web Service trên Render.com
1. Truy cập [Render.com](https://render.com) và đăng nhập bằng tài khoản GitHub.
2. Bấm vào **New** -> **Web Service**.
3. Kết nối với Repository GitHub bạn vừa tạo.
4. Ở phần cài đặt Web Service, điền như sau:
   - **Name:** Tên tùy ý (ví dụ: `annotation-bot`)
   - **Environment:** Chọn **Docker** (Rất quan trọng! Render sẽ tự đọc file `Dockerfile` của bạn).
   - **Region:** Chọn vùng gần bạn nhất (ví dụ: Singapore).
   - **Instance Type:** Chọn gói **Free** (512MB RAM) hoặc Starter. Với bản update mới, 512MB RAM là ĐỦ sức chạy!

## 3. Cấu hình Biến Môi Trường (Environment Variables)
Cuộn xuống phần **Environment Variables**, bấm "Add Environment Variable" và nhập chính xác các thông tin trong file `.env` của bạn:

- `EMAIL` = `(Email đăng nhập HumanSignal)`
- `PASSWORD` = `(Mật khẩu)`
- `GEMINI_API_KEY` = `(Mã API Gemini)`
- `USE_NGROK` = `false` *(Bắt buộc phải là false vì Render đã cấp sẵn cho bạn tên miền HTTPS miễn phí cực xịn xò, ví dụ: https://annotation-bot.onrender.com)*

> **Lưu ý:** Bạn không cần điền `PORT` hay `NGROK_TOKEN` vì Render tự quản lý Port và chúng ta đã tắt Ngrok.

## 4. Chờ Render Build và Tận Hưởng!
Bấm **Create Web Service**. 

Render sẽ tự động tải các gói cài đặt (Playwright, Chromium...) từ `Dockerfile`. Quá trình này diễn ra khoảng **3 đến 5 phút** cho lần đầu tiên.
Sau khi thấy dòng chữ **"Your service is live"**, bạn có thể bấm vào link Web (vd: `https://xxx.onrender.com`) để truy cập vào siêu phẩm của mình! 

Mọi thứ chạy hoàn toàn tự động trên Cloud 24/7. Chúc bạn thành công!
