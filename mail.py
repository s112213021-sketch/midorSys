#!/usr/bin/env python3
from fastapi import FastAPI, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from email.message import EmailMessage
import smtplib
import time
import hashlib
import os
from typing import Dict
import requests

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")

# 只保留一次性註冊 token（不再需要 pending_verifications）
register_tokens: Dict[str, Dict] = {}  # {token: {"student_id": str, "name": str, "created_at": float}}

# 設定
SECRET_SALT = "moli_monkey_golden_age"
PI_API_URL = os.getenv("http://192.168.1.41:8000")  # Optional: 用來通知樹莓派進入註冊模式
MAX_TOKEN_AGE = int(os.getenv("TOKEN_TTL", 24 * 3600))  # token 有效期（預設 24 小時）

def generate_register_token(student_id: str, name: str) -> str:
    timestamp = str(int(time.time()))
    raw = student_id + timestamp + SECRET_SALT
    token = hashlib.sha256(raw.encode()).hexdigest()
    register_tokens[token] = {
        "student_id": student_id,
        "name": name,
        "created_at": time.time()
    }
    link = f"{PI_API_URL}/activate-register-mode/{token}"
    return link

def send_register_link_email(student_id: str, name: str, link: str):
    recipient = f"s{student_id}@mail1.ncnu.edu.tw"
    msg = EmailMessage()
    msg["From"] = "moli-lab@localhost"
    msg["To"] = recipient
    msg["Subject"] = "MOLi 實驗室 門禁註冊連結（點擊啟動刷卡模式）"
    msg.set_content(
        f"{name} 您好，\n\n"
        f"感謝您註冊 MOLi 實驗室門禁系統！\n\n"
        f"當您準備好到實驗室刷學生證時，請點擊以下連結啟動註冊模式：\n\n"
        f"{link}\n\n"
        f"此連結僅能使用一次，不限時間。\n\n"
        f"MOLi 團隊"
    )
    try:
        with smtplib.SMTP("localhost", 2530) as server:  # Mailpit 或學校 SMTP
            server.send_message(msg)
        print(f"✅ 註冊連結信已寄給 {recipient}")
    except Exception as e:
        print(f"❌ 寄信失敗：{e}")
        raise HTTPException(status_code=500, detail="寄信失敗，請稍後再試或聯絡管理員")

@app.get("/", response_class=HTMLResponse)
async def home():
    with open("static/register.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

# 直接一步驟：提交學號姓名 → 寄一次性連結
@app.post("/register")
async def register(student_id: str = Form(...), name: str = Form(...)):
    student_id = student_id.strip()
    name = name.strip()

    if not student_id or not name:
        raise HTTPException(status_code=400, detail="學號與姓名必填")

    # # 可選：簡單防刷（同學號短時間內重複註冊）
    # # 這裡不檢查也沒關係，因為連結是一次性的

    link = generate_register_token(student_id, name)
    send_register_link_email(student_id, name, link)

    return JSONResponse({
        "message": "註冊成功！一次性註冊連結已寄至您的學校信箱",
        "hint": "請在準備好到實驗室刷卡時，點擊信中連結啟動註冊模式"
    })

# 同學點擊信中連結 → 通知樹莓派進入註冊模式並回傳引導頁面
@app.get("/activate-register-mode/{token}")
async def activate_register_mode(token: str):
        # 驗證 token
        if token not in register_tokens:
                raise HTTPException(status_code=400, detail="無效或已使用的連結")

        data = register_tokens.pop(token)
        student_id = data["student_id"]
        name = data["name"]
        created_at = data.get("created_at", 0)

        # # 檢查 TTL
        # if time.time() - created_at > MAX_TOKEN_AGE:
        #         raise HTTPException(status_code=400, detail="連結已過期，請重新申請")

        # # 嘗試通知樹莓派進入註冊模式
        # if PI_API_URL:
        #         try:
        #                 headers = {"Content-Type": "application/json"}
        #                 if PI_API_KEY:
        #                         headers["X-API-KEY"] = PI_API_KEY
        #                 resp = requests.post(f"{PI_API_URL}/mode/register", json={"student_id": student_id}, headers=headers, timeout=5)
        #                 if not resp.ok:
        #                         print(f"⚠️ 通知 Pi 進入註冊模式失敗: {resp.status_code} {resp.text}")
        #         except Exception as e:
        #                 print(f"⚠️ 與 PI 通訊失敗: {e}")

        # # 回傳引導刷卡頁面（前端會輪詢 /check_status/<student_id>）
        # html = """
        # <!doctype html>
        # <html lang=\"zh-Hant\">
        # <head>
        #     <meta charset=\"utf-8\"> 
        #     <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"> 
        #     <title>註冊模式啟動</title>
        #     <link rel=\"stylesheet\" href=\"/static/style.css\"> 
        # </head>
        # <body>
        #     <div class=\"page-wrap\">
        #         <header class=\"moli-header\"> 
        #             <div class=\"brand\"><span class=\"brand-mark\"></span><span>MOLI 門禁系統</span></div>
        #         </header>
        #         <main class=\"main-center\">
        #             <div class=\"card card-center\">
        #                 <h2 class=\"card-title\">註冊模式已啟動</h2>
        #                 <p>親愛的 {NAME_PLACEHOLDER}（學號 {STUDENT_PLACEHOLDER}），請於 60 秒內到實驗室刷學生證完成雙次刷卡確認。</p>

        #                 <div id=\"scanStatus\" style=\"margin-top:16px; font-weight:bold;\">等待刷卡...（請刷學生證以完成註冊）</div>
        #                 <div id=\"spinner\" style=\"margin-top:12px;\">⏳</div>
        #                 <div id=\"successMsg\" style=\"display:none; margin-top:10px; text-align:center;\"></div>
        #             </div>
        #         </main>
        #     </div>

        #     <script>
        #         let attempts = 0;
        #         const maxAttempts = 30;
        #         const scanStatus = document.getElementById('scanStatus');
        #         const spinner = document.getElementById('spinner');
        #         const successMsg = document.getElementById('successMsg');

        #         const studentId = "{STUDENT_PLACEHOLDER}";
        #         const intervalId = setInterval(async () => {
        #             attempts++;
        #             try {
        #                 const res = await fetch(`/check_status/{STUDENT_PLACEHOLDER}`);
        #                 if (!res.ok) throw new Error('status check failed');
        #                 const data = await res.json();
        #                 if (data.bound) {
        #                     clearInterval(intervalId);
        #                     spinner.style.display = 'none';
        #                     scanStatus.style.color = '#22c55e';
        #                     scanStatus.textContent = '綁定完成！歡迎進入 MOLI 實驗室';
        #                       setTimeout(() => { window.location.href = '/success?student_id=' + encodeURIComponent(studentId); }, 1500);
        #                 } else if (data.status === 'step_1') {
        #                     scanStatus.textContent = '✅ 讀取成功！請使用此進行“第二次刷卡”確認...';
        #                     scanStatus.style.color = '#2563eb';
        #                 } else if (data.status === 'step_2' || data.currentStep === 2) {
        #                     successMsg.innerHTML = `<strong style=\"color:green;\">${{data.message || ''}}</strong><br>${{data.hint || ''}}<br><br>請準備好後打開學校信箱，點擊「註冊連結」啟動讀卡機註冊模式。`;
        #                     successMsg.style.display = 'block';
        #                 }
        #                 if (attempts >= maxAttempts) {
        #                     clearInterval(intervalId);
        #                     spinner.style.display = 'none';
        #                     scanStatus.textContent = '❌ 刷卡逾時，請重新整理頁面再試。';
        #                     scanStatus.style.color = 'red';
        #                 }
        #             } catch (e) {
        #                 console.error(e);
        #                 clearInterval(intervalId);
        #                 spinner.style.display = 'none';
        #                 scanStatus.textContent = '❌ 連線錯誤，請重新整理頁面再試。';
        #                 scanStatus.style.color = 'red';
        #             }
        #         }, 2000);
        #     </script>
        # </body>
        # </html>
        #     """

        # # 將佔位符替換為實際的 student_id 與 name（使用 replace 可以避免 f-string 與 JS template literal 的衝突）
        # html = html.replace('{STUDENT_PLACEHOLDER}', student_id).replace('{NAME_PLACEHOLDER}', name)

        # return HTMLResponse(content=html)
