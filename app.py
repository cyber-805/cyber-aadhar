import requests
import base64
import hashlib
import re
import io
import time
import json
import threading
import sys
import os
from flask import Flask, request, jsonify
from bs4 import BeautifulSoup
from PIL import Image
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from Crypto.Random import get_random_bytes

# --- CONFIGURATION ---
PORT = 3000
ENCRYPTION_KEY = "nic@impds#dedup05613"
USERNAME = "adminWB"
PASSWORD = "2p3MrgdgV8s9"

# Check OCR availability
try:
    import pytesseract
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False
    print("[-] Warning: 'pytesseract' library not found.")

app = Flask(__name__)

# --- ENCRYPTION HELPER ---
class CryptoHandler:
    def __init__(self, passphrase):
        self.passphrase = passphrase.encode('utf-8')

    def _derive_key_and_iv(self, salt, key_length=32, iv_length=16):
        d = d_i = b''
        while len(d) < key_length + iv_length:
            d_i = hashlib.md5(d_i + self.passphrase + salt).digest()
            d += d_i
        return d[:key_length], d[key_length:key_length+iv_length]

    def encrypt(self, plain_text):
        salt = get_random_bytes(8)
        key, iv = self._derive_key_and_iv(salt)
        cipher = AES.new(key, AES.MODE_CBC, iv)
        encrypted_bytes = cipher.encrypt(pad(plain_text.encode('utf-8'), AES.block_size))
        return base64.b64encode(b"Salted__" + salt + encrypted_bytes).decode('utf-8')

    def decrypt(self, encrypted_b64):
        try:
            encrypted_data = base64.b64decode(encrypted_b64)
            if encrypted_data[:8] != b'Salted__':
                return None
            salt = encrypted_data[8:16]
            cipher_bytes = encrypted_data[16:]
            key, iv = self._derive_key_and_iv(salt)
            cipher = AES.new(key, AES.MODE_CBC, iv)
            decrypted_bytes = unpad(cipher.decrypt(cipher_bytes), AES.block_size)
            return decrypted_bytes.decode('utf-8')
        except Exception:
            return None

crypto_engine = CryptoHandler(ENCRYPTION_KEY)

# --- AUTOMATION & BOT LOGIC ---
class IMPDSBot:
    def __init__(self):
        self.init_session()
        self.lock = threading.Lock()
        self.jsessionid = None
        self.last_login_time = 0
        self.user_salt = None
        self.csrf_token = None
        self.base_url = "https://impds.nic.in/impdsdeduplication"

    def init_session(self):
        """Sets up the session with headers exactly matching your CURL command"""
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Accept-Encoding': 'gzip, deflate, br, zstd',
            'Accept-Language': 'en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7',
            'Connection': 'keep-alive',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'Origin': 'https://impds.nic.in',
            'Referer': 'https://impds.nic.in/impdsdeduplication/LoginPage',
            'X-Requested-With': 'XMLHttpRequest', # <--- VERY IMPORTANT (AJAX HEADER)
            'sec-ch-ua': '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Linux"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'priority': 'u=1, i'
        })

    def sha512(self, text):
        return hashlib.sha512(text.encode('utf-8')).hexdigest()

    def ensure_session(self):
        with self.lock:
            # Check validity (20 mins to be safe)
            if self.jsessionid and (time.time() - self.last_login_time < 1200):
                return True

            print("\n🔄 Session expired or missing. Starting Login Sequence...")
            
            max_retries = 5
            for attempt in range(1, max_retries + 1):
                print(f"🔹 Login Attempt {attempt}/{max_retries}...")
                
                # Retry par session fresh karo
                if attempt > 1:
                    print("🧹 Cleaning session for retry...")
                    self.init_session()
                    time.sleep(2)

                if self.perform_login():
                    return True
                else:
                    if attempt < max_retries:
                        print("⚠️ Retrying in 2 seconds...")
                        time.sleep(2)
            
            print("❌ All login attempts failed.")
            return False

    def perform_login(self):
        try:
            # 1. Access Login Page to get Cookies and Initial Tokens
            # Note: Is request ke liye 'X-Requested-With' hatana pad sakta hai kyuki ye Page Load hai, AJAX nahi.
            # Temporary header modify kar rahe hain
            page_headers = self.session.headers.copy()
            if 'X-Requested-With' in page_headers: del page_headers['X-Requested-With']
            page_headers['Accept'] = 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
            page_headers['sec-fetch-dest'] = 'document'
            page_headers['sec-fetch-mode'] = 'navigate'

            r = self.session.get(f"{self.base_url}/LoginPage", headers=page_headers, timeout=20)
            
            soup = BeautifulSoup(r.text, 'html.parser')
            
            csrf_input = soup.find('input', {'name': 'REQ_CSRF_TOKEN'})
            self.csrf_token = csrf_input.get('value') if csrf_input else None
            
            scripts = soup.find_all('script')
            for script in scripts:
                if script.string and 'USER_SALT' in script.string:
                    match = re.search(r"USER_SALT\s*=\s*['\"]([^'\"]+)['\"]", script.string)
                    if match:
                        self.user_salt = match.group(1)

            if not self.csrf_token or not self.user_salt:
                print(f"[-] Failed to scrape tokens. Status: {r.status_code}")
                return False

            print(f"[+] Tokens Found. CSRF: {self.csrf_token[:10]}...")

            # 2. Get Captcha
            c_res = self.session.post(f"{self.base_url}/ReloadCaptcha", timeout=10)
            captcha_b64 = c_res.json().get('captchaBase64')
            
            # 3. Solve Captcha
            captcha_text = self.solve_captcha(captcha_b64)
            if not captcha_text:
                return False

            # 4. Hash Password (SHA512 Logic matches your Curl hash length)
            salted_pass = self.sha512(self.sha512(self.user_salt) + self.sha512(PASSWORD))

            # 5. Submit Login (AJAX Request - Headers automatically used from init_session)
            payload = {
                'userName': USERNAME,
                'password': salted_pass,
                'captcha': captcha_text,
                'REQ_CSRF_TOKEN': self.csrf_token
            }
            
            # Explicitly adding headers for the POST request to match CURL exactly
            post_headers = self.session.headers.copy() # Uses the AJAX headers set in init
            
            print("[*] Sending Login Request...")
            l_res = self.session.post(
                f"{self.base_url}/UserLogin", 
                data=payload, 
                headers=post_headers,
                timeout=20
            )
            
            # Check response
            if l_res.status_code == 200:
                try:
                    resp_json = l_res.json()
                    if resp_json.get('athenticationError'): # Notice the spelling in API usually
                        print(f"[-] Login Failed: {resp_json.get('athenticationError')}")
                        return False
                    else:
                        self.jsessionid = self.session.cookies.get('JSESSIONID')
                        self.last_login_time = time.time()
                        print(f"✅ Login Successful! JSESSIONID: {self.jsessionid}")
                        return True
                except json.JSONDecodeError:
                    # Agar JSON nahi aaya, matlab shayad success redirect hai ya HTML error
                    if "Welcome" in l_res.text or "Dashboard" in l_res.text:
                         self.jsessionid = self.session.cookies.get('JSESSIONID')
                         print(f"✅ Login Successful (HTML check)! JSESSIONID: {self.jsessionid}")
                         return True
                    print(f"[-] Login response not JSON. Text snippet: {l_res.text[:100]}")
                    return False
            else:
                print(f"[-] HTTP Error: {l_res.status_code}")
                return False

        except Exception as e:
            print(f"[-] Exception during login: {e}")
            return False

    def solve_captcha(self, b64_str):
        if not b64_str: return None
        try:
            img_data = base64.b64decode(b64_str)
            image = Image.open(io.BytesIO(img_data))
            
            # --- Image Processing ---
            image = image.convert('L')
            # Thoda threshold adjust kiya hai 145 par
            image = image.point(lambda x: 0 if x < 145 else 255, '1')

            if OCR_AVAILABLE:
                # Whitelist A-Z and 0-9
                custom_config = r'--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
                text = pytesseract.image_to_string(image, config=custom_config)
                
                clean = ''.join(filter(str.isalnum, text.strip().upper()))
                
                if len(clean) >= 4:
                    print(f"[?] OCR Guessed: {clean}")
                    return clean
                else:
                    print(f"[-] OCR Read unclear: '{clean}'")
                    return None
            else:
                return None
                
        except Exception:
            return None

    def search_aadhaar(self, search_term, encrypted_aadhaar):
        if not self.ensure_session():
            return {"error": "Authentication Failed"}

        # Search headers - mixing AJAX headers with correct Referer
        headers = self.session.headers.copy()
        headers['Referer'] = f"{self.base_url}/search"
        
        data = {'search': search_term, 'aadhar': encrypted_aadhaar}
        
        try:
            res = self.session.post(f"{self.base_url}/search", data=data, headers=headers, timeout=30)
            
            if "LoginPage" in res.text or "UserLogin" in res.text:
                print("⚠️ Session expired during search. Re-logging...")
                self.jsessionid = None
                if self.ensure_session():
                    res = self.session.post(f"{self.base_url}/search", data=data, headers=headers, timeout=30)
                else:
                    return {"error": "Re-login failed"}

            return self.parse_html(res.text)
        except Exception as e:
            return {"error": str(e)}

    def parse_html(self, html):
        soup = BeautifulSoup(html, 'html.parser')
        tables = soup.find_all('table', class_='table-striped')
        
        if len(tables) < 2:
            return {"error": "No records found"}

        main_table = tables[0]
        rows = main_table.find('tbody').find_all('tr')
        
        if not rows:
            return {"error": "No records found"}

        ration_map = {}
        for row in rows:
            cols = row.find_all('td')
            if len(cols) >= 8:
                rc_no = cols[3].get_text(strip=True)
                if rc_no not in ration_map:
                    ration_map[rc_no] = {
                        "ration_card_details": {
                            "state_name": cols[1].get_text(strip=True),
                            "district_name": cols[2].get_text(strip=True),
                            "ration_card_no": rc_no,
                            "scheme_name": cols[4].get_text(strip=True)
                        },
                        "members": []
                    }
                ration_map[rc_no]["members"].append({
                    "s_no": cols[0].get_text(strip=True),
                    "member_id": cols[5].get_text(strip=True),
                    "member_name": cols[6].get_text(strip=True),
                    "remark": cols[7].get_text(strip=True)
                })

        info_data = {
            "fps_category": "Unknown",
            "impds_transaction_allowed": False,
            "exists_in_central_repository": False,
            "duplicate_aadhaar_beneficiary": False
        }
        try:
            info_rows = tables[1].find('tbody').find_all('tr')
            for row in info_rows:
                cols = row.find_all('td')
                if len(cols) >= 2:
                    label = cols[0].get_text(strip=True)
                    val = cols[1].get_text(strip=True).lower()
                    if 'FPS category' in label:
                        info_data['fps_category'] = 'Online FPS' if val == 'yes' else 'Offline FPS'
                    elif 'IMPDS transaction' in label:
                        info_data['impds_transaction_allowed'] = (val == 'yes')
                    elif 'Central Repository' in label:
                        info_data['exists_in_central_repository'] = (val == 'yes')
                    elif 'duplicate Aadaar' in label:
                        info_data['duplicate_aadhaar_beneficiary'] = (val == 'yes')
        except:
            pass

        results = []
        for card in ration_map.values():
            card['additional_info'] = info_data
            results.append(card)
        return results

bot = IMPDSBot()

@app.route('/search-aadhaar', methods=['GET'])
def api_search():
    search_type = request.args.get('search', 'A')
    aadhaar = request.args.get('aadhaar')
    if not aadhaar:
        return jsonify({"success": False, "error": "Missing aadhaar"}), 400

    decrypted_check = crypto_engine.decrypt(aadhaar)
    encrypted_val = aadhaar if decrypted_check else crypto_engine.encrypt(aadhaar)

    result = bot.search_aadhaar(search_type, encrypted_val)
    
    status = 404 if "No records" in str(result) else 500
    if "error" in result:
        return jsonify({"success": False, "error": result["error"]}), status
    
    return jsonify({"success": True, "count": len(result), "results": result})

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "success": True, 
        "service": "IMPDS Python API", 
        "session_active": bool(bot.jsessionid)
    })

def start_server():
    print("🚀 Initializing IMPDS Bot...")
    bot.ensure_session()
    print(f"🌍 Server running on port {PORT}...")
    app.run(host='0.0.0.0', port=PORT, debug=False)

if __name__ == "__main__":
    start_server()