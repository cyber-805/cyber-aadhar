import requests
import base64
import hashlib
import re
import io
import time
import json
import os
from flask import Flask, request, jsonify
from bs4 import BeautifulSoup
from PIL import Image
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from Crypto.Random import get_random_bytes

# ========== CONFIGURATION ==========
ENCRYPTION_KEY = "nic@impds#dedup05613"
USERNAME = "adminWB"
PASSWORD = "2p3MrgdgV8s9"
OCR_SPACE_API_KEY = os.environ.get("OCR_SPACE_API_KEY", "")  # Get from Vercel Env Variables
OCR_SPACE_FREE = True  # Free tier (80 requests/month)

# Optional: try to import pytesseract (will fail on Vercel)
try:
    import pytesseract
    TESSERACT_AVAILABLE = True
except ImportError:
    TESSERACT_AVAILABLE = False

app = Flask(__name__)

# ========== ENCRYPTION ==========
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

crypto = CryptoHandler(ENCRYPTION_KEY)

# ========== OCR HELPER (Local Tesseract + Fallback to OCR.space) ==========
def solve_captcha(b64_str):
    if not b64_str:
        return None

    # Decode image
    img_data = base64.b64decode(b64_str)
    image = Image.open(io.BytesIO(img_data))
    image = image.convert('L')
    image = image.point(lambda x: 0 if x < 145 else 255, '1')

    # 1) Try local pytesseract if available
    if TESSERACT_AVAILABLE:
        try:
            custom_config = r'--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
            text = pytesseract.image_to_string(image, config=custom_config)
            clean = ''.join(filter(str.isalnum, text.strip().upper()))
            if len(clean) >= 4:
                print(f"[OCR Local] Guessed: {clean}")
                return clean
        except Exception as e:
            print(f"Local OCR error: {e}")

    # 2) Fallback to OCR.space API
    if OCR_SPACE_API_KEY:
        try:
            buffered = io.BytesIO()
            image.save(buffered, format="PNG")
            img_base64 = base64.b64encode(buffered.getvalue()).decode()

            payload = {
                'apikey': OCR_SPACE_API_KEY,
                'base64Image': f'data:image/png;base64,{img_base64}',
                'language': 'eng',
                'OCREngine': 2,          # faster engine
                'isCreateSearchablePdf': False,
                'isSearchablePdfHideTextLayer': False,
                'scale': True,
                'isTable': False,
                'filetype': 'PNG'
            }
            r = requests.post('https://api.ocr.space/parse/image', data=payload, timeout=10)
            result = r.json()
            if result.get('IsErroredOnProcessing') == False:
                parsed_text = result['ParsedResults'][0]['ParsedText']
                clean = ''.join(filter(str.isalnum, parsed_text.strip().upper()))
                if len(clean) >= 4:
                    print(f"[OCR.space] Guessed: {clean}")
                    return clean
        except Exception as e:
            print(f"OCR.space error: {e}")

    print("[-] OCR failed to solve captcha")
    return None

# ========== IMPDS BOT (Stateless per request, but caches session globally) ==========
class IMPDSBot:
    def __init__(self):
        self.session = None
        self.jsessionid = None
        self.last_login_time = 0
        self.user_salt = None
        self.csrf_token = None
        self.base_url = "https://impds.nic.in/impdsdeduplication"

    def _init_session(self):
        """Fresh session with headers exactly as original"""
        s = requests.Session()
        s.headers.update({
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Accept-Encoding': 'gzip, deflate, br, zstd',
            'Accept-Language': 'en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7',
            'Connection': 'keep-alive',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'Origin': 'https://impds.nic.in',
            'Referer': 'https://impds.nic.in/impdsdeduplication/LoginPage',
            'X-Requested-With': 'XMLHttpRequest',
            'sec-ch-ua': '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Linux"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
        })
        return s

    def sha512(self, text):
        return hashlib.sha512(text.encode('utf-8')).hexdigest()

    def ensure_session(self):
        """Login if session missing or expired (>20 min)"""
        now = time.time()
        if self.session and self.jsessionid and (now - self.last_login_time < 1200):
            return True

        print("\n🔄 Logging into IMPDS...")
        self.session = self._init_session()

        # 1. Get login page (no X-Requested-With)
        page_headers = {k:v for k,v in self.session.headers.items() if k != 'X-Requested-With'}
        page_headers.update({
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'sec-fetch-dest': 'document',
            'sec-fetch-mode': 'navigate'
        })
        r = self.session.get(f"{self.base_url}/LoginPage", headers=page_headers, timeout=20)
        soup = BeautifulSoup(r.text, 'html.parser')

        csrf_input = soup.find('input', {'name': 'REQ_CSRF_TOKEN'})
        self.csrf_token = csrf_input.get('value') if csrf_input else None

        # Extract USER_SALT from JavaScript
        scripts = soup.find_all('script')
        self.user_salt = None
        for script in scripts:
            if script.string and 'USER_SALT' in script.string:
                match = re.search(r"USER_SALT\s*=\s*['\"]([^'\"]+)['\"]", script.string)
                if match:
                    self.user_salt = match.group(1)
                    break

        if not self.csrf_token or not self.user_salt:
            print("[-] Failed to extract tokens")
            return False

        # 2. Get captcha
        c_res = self.session.post(f"{self.base_url}/ReloadCaptcha", timeout=10)
        captcha_b64 = c_res.json().get('captchaBase64')
        captcha_text = solve_captcha(captcha_b64)
        if not captcha_text:
            return False

        # 3. Hash password
        salted_pass = self.sha512(self.sha512(self.user_salt) + self.sha512(PASSWORD))

        # 4. Login (AJAX)
        payload = {
            'userName': USERNAME,
            'password': salted_pass,
            'captcha': captcha_text,
            'REQ_CSRF_TOKEN': self.csrf_token
        }
        l_res = self.session.post(f"{self.base_url}/UserLogin", data=payload, timeout=20)

        if l_res.status_code == 200:
            try:
                resp_json = l_res.json()
                if resp_json.get('authenticationError') or resp_json.get('athenticationError'):
                    print(f"[-] Login failed: {resp_json}")
                    return False
            except:
                # Non-JSON response – check for dashboard text
                if "Welcome" in l_res.text or "Dashboard" in l_res.text:
                    pass
                else:
                    print("[-] Unexpected login response")
                    return False
            self.jsessionid = self.session.cookies.get('JSESSIONID')
            self.last_login_time = time.time()
            print(f"✅ Login OK. JSESSIONID={self.jsessionid}")
            return True
        else:
            print(f"[-] HTTP {l_res.status_code}")
            return False

    def search_aadhaar(self, search_term, encrypted_aadhaar):
        if not self.ensure_session():
            return {"error": "Authentication failed"}

        headers = self.session.headers.copy()
        headers['Referer'] = f"{self.base_url}/search"
        data = {'search': search_term, 'aadhar': encrypted_aadhaar}

        try:
            res = self.session.post(f"{self.base_url}/search", data=data, headers=headers, timeout=30)
            if "LoginPage" in res.text or "UserLogin" in res.text:
                # Session expired during request
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
        tbody = main_table.find('tbody')
        if not tbody:
            return {"error": "No data in table"}
        rows = tbody.find_all('tr')
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

        # Additional info from second table
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

# Global bot instance (shared across warm invocations)
bot = IMPDSBot()

# ========== FLASK ROUTES ==========
@app.route('/search-aadhaar', methods=['GET'])
def api_search():
    search_type = request.args.get('search', 'A')
    aadhaar = request.args.get('aadhaar')
    if not aadhaar:
        return jsonify({"success": False, "error": "Missing aadhaar"}), 400

    # Decrypt if needed, otherwise encrypt plain aadhaar
    decrypted = crypto.decrypt(aadhaar)
    encrypted_val = aadhaar if decrypted else crypto.encrypt(aadhaar)

    result = bot.search_aadhaar(search_type, encrypted_val)

    if isinstance(result, dict) and "error" in result:
        status = 404 if "No records" in result["error"] else 500
        return jsonify({"success": False, "error": result["error"]}), status

    return jsonify({"success": True, "count": len(result), "results": result})

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "success": True,
        "service": "IMPDS Python API (Vercel)",
        "session_active": bool(bot.jsessionid)
    })

# For local testing only – Vercel will ignore this
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=3000, debug=False)
