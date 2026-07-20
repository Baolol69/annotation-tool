import os

def start_ngrok(port: int, static_domain: str = "slacks-gag-exchange.ngrok-free.dev"):
    """
    Khởi động Ngrok tunnel, tự động fallback sang domain ngẫu nhiên nếu domain tĩnh bị kẹt,
    và thiết lập biến môi trường GRADIO_ALLOWED_ORIGINS để Gradio hoạt động trơn tru.
    """
    try:
        from pyngrok import ngrok
        
        ngrok_token = os.environ.get("NGROK_TOKEN")
        if ngrok_token:
            ngrok.set_auth_token(ngrok_token)
            
        # Tự động đóng tất cả tunnel cũ để reset Ngrok hoàn toàn
        ngrok.kill()
        
        # Tự động mở tunnel ngrok ở port
        try:
            public_url = ngrok.connect(port, domain=static_domain).public_url
        except Exception as e:
            if "334" in str(e) or "already online" in str(e):
                print(f"⚠️ Tên miền tĩnh '{static_domain}' bị treo trên hệ thống Ngrok Cloud!")
                print(f"⚠️ Đang tự động tạo tên miền ngẫu nhiên để bạn dùng tạm...")
                public_url = ngrok.connect(port).public_url
            else:
                raise e
                
        # Cập nhật Origin để cho phép WebSocket của Gradio 4 kết nối qua Ngrok
        domain = public_url.replace("https://", "").replace("http://", "")
        os.environ["GRADIO_ALLOWED_ORIGINS"] = domain
        print(f"\n{'='*50}")
        print(f"🌍 ỨNG DỤNG ĐÃ ONLINE TẠI: {public_url}")
        print(f"{'='*50}\n")
        
        return public_url
    except ImportError:
        print("[Chú ý] Chưa cài thư viện pyngrok. Hãy chạy 'pip install pyngrok' nếu muốn public ra internet.")
    except Exception as e:
        print(f"[Ngrok Error] Không thể khởi động Ngrok: {e}")
        
    return None
