# updater.py
import requests
import zipfile
import os
import shutil
import subprocess
import sys
import time

# ⚠️ THAY ĐỔI 2 DÒNG NÀY
GITHUB_REPO = "huunam374/Crypto-Bot" # Ví dụ: "nguyenvana/cryptobot"
CURRENT_VERSION = "1.0.2" # Sẽ được đọc từ version.txt

def get_latest_release():
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    try:
        r = requests.get(url, headers={"User-Agent": "CryptoBotUpdater"}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"Lỗi khi kiểm tra bản cập nhật: {e}")
        return None

def download_and_extract(zip_url, target_dir):
    try:
        print(f"Đang tải bản cập nhật từ: {zip_url}")
        r = requests.get(zip_url, stream=True, timeout=60)
        r.raise_for_status()
        
        zip_path = "update_temp.zip"
        with open(zip_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        
        print("Đang giải nén...")
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(target_dir)
        
        os.remove(zip_path)
        return True
    except Exception as e:
        print(f"Lỗi tải/giải nén: {e}")
        return False

def main():
    # Đọc version hiện tại
    try:
        with open("version.txt", "r") as f:
            current_ver = f.read().strip()
    except:
        current_ver = CURRENT_VERSION
    
    print(f"Phiên bản hiện tại: {current_ver}")
    print("Đang kiểm tra bản cập nhật...")
    
    release = get_latest_release()
    if not release:
        print("Không thể kiểm tra bản cập nhật. Thoát.")
        return

    latest_tag = release['tag_name'].replace('v', '')
    print(f"Phiên bản mới nhất trên GitHub: {latest_tag}")

    if latest_tag > current_ver:
        # Tìm file .zip trong release assets
        zip_asset = next((a for a in release['assets'] if a['name'].endswith('.zip')), None)
        if not zip_asset:
            print("Không tìm thấy file .zip trong bản release mới.")
            return
        
        print(f"Có bản cập nhật mới: {latest_tag}. Bắt đầu tải...")
        
        # Thư mục hiện tại của app (nơi chứa updater.exe)
        app_dir = os.getcwd() 
        old_dir = app_dir + "_old"
        temp_extract_dir = "temp_update"

        # Dọn dẹp thư mục tạm cũ (nếu có)
        if os.path.exists(temp_extract_dir):
            shutil.rmtree(temp_extract_dir)
        os.makedirs(temp_extract_dir)

        # Tải và giải nén vào thư mục tạm
        if download_and_extract(zip_asset['browser_download_url'], temp_extract_dir):
            print("Đang thay thế file...")
            
            # Đổi tên thư mục app cũ để nhường chỗ
            if os.path.exists(old_dir):
                shutil.rmtree(old_dir)
            os.rename(app_dir, old_dir)
            
            # Di chuyển thư mục mới vào vị trí cũ
            shutil.move(temp_extract_dir, app_dir)
            
            print("Cập nhật thành công! Đang khởi động lại app...")
            # Khởi động lại app chính (giả sử file exe tên là CryptoBot.exe)
            subprocess.Popen([os.path.join(app_dir, "CryptoBot.exe")])
            
            # Xóa thư mục cũ (có thể thất bại nếu file đang bị khóa, không sao cả)
            try:
                shutil.rmtree(old_dir)
            except:
                pass
        else:
            print("Tải thất bại.")
    else:
        print("Bạn đang dùng phiên bản mới nhất.")

if __name__ == "__main__":
    main()