#!/usr/bin/env python3
from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, Column, String, TIMESTAMP, func, Integer, ForeignKey, and_
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import IntegrityError
import os
from dotenv import load_dotenv
import requests
import threading
from datetime import datetime, timedelta
import secrets
import sys
import logging
import asyncio

# 引入郵件服務模組
from mail import send_verification_email, is_smtp_configured

# RFID 讀取相關 (可選)
try:
    from evdev import InputDevice, ecodes, list_devices
    EVDEV_AVAILABLE = True
except ImportError:
    EVDEV_AVAILABLE = False
    InputDevice = None
    ecodes = None
    list_devices = None

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
BOT_TOKEN = os.getenv("BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")
PI_API_URL = os.getenv("PI_API_URL")
PI_API_KEY = os.getenv("PI_API_KEY")

# SMTP 郵件設定
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SERVER_URL = os.getenv("SERVER_URL", "http://localhost:8000")

# RFID 裝置設定
RFID_DEVICE_PATH = os.getenv("RFID_DEVICE_PATH", "/dev/input/event0")  # 根據實際裝置調整
RFID_ENABLED = os.getenv("RFID_ENABLED", "false").lower() == "true"

app = FastAPI()

# Logging 設定
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    student_id = Column(String(20), primary_key=True)
    name = Column(String(50), nullable=False)
    rfid_uid = Column(String(50), unique=True, nullable=True)
    email_verified = Column(Integer, default=0)
    verification_token = Column(String(100), nullable=True)
    token_expires_at = Column(TIMESTAMP(timezone=True), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

class AccessLog(Base):
    __tablename__ = "access_logs"
    id = Column(Integer, primary_key=True)
    student_id = Column(String(20), ForeignKey("users.student_id"))
    rfid_uid = Column(String(50))
    action = Column(String(10))
    timestamp = Column(TIMESTAMP(timezone=True), server_default=func.now())

class RegistrationSession(Base):
    __tablename__ = "registration_sessions"
    student_id = Column(String(20), ForeignKey("users.student_id"), primary_key=True)
    first_uid = Column(String(50), nullable=True)
    step = Column(Integer, default=0)
    expires_at = Column(TIMESTAMP(timezone=True), nullable=True)

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def send_telegram(text: str):
    if not BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": text}, timeout=5)
    except:
        pass

# ================= Pi 通知函數 =================
def notify_pi_register_bg(student_id: str):
    if not PI_API_URL:
        return
    try:
        headers = {"Content-Type": "application/json"}
        if PI_API_KEY:
            headers["X-API-KEY"] = PI_API_KEY
        requests.post(f"{PI_API_URL.rstrip('/')}/mode/register",
    xjson={"student_id": student_id}, headers=headers, timeout=5)
    except Exception as e:
        print(f"[notify_pi] error: {e}")

# === Pi 呼叫的 API（保持不變）===
@app.post("/api/scan")
async def api_scan(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    rfid_uid = data.get("rfid_uid")
    if not rfid_uid:
        return JSONResponse({"error": "missing rfid_uid"}, status_code=400)

    user = db.query(User).filter(User.rfid_uid == rfid_uid).first()
    if user:
        db.add(AccessLog(student_id=user.student_id, rfid_uid=rfid_uid, action="entry"))
        db.commit()
        send_telegram(f"歡迎！{user.name} ({user.student_id}) 已進入實驗室")
        return {"status": "allow", "student_id": user.student_id, "name": user.name}
    return {"status": "deny"}

@app.post("/api/register/start")
async def api_register_start(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    student_id = data.get("student_id")
    if not student_id:
        return JSONResponse({"error": "missing student_id"}, status_code=400)
    user = db.query(User).filter(User.student_id == student_id).first()
    if not user:
        return JSONResponse({"error": "user_not_found"}, status_code=404)

    expires = datetime.utcnow() + timedelta(seconds=90)
    session = db.query(RegistrationSession).filter(RegistrationSession.student_id == student_id).first()
    if session:
        session.first_uid = None
        session.step = 0
        session.expires_at = expires
    else:
        session = RegistrationSession(student_id=student_id, expires_at=expires)
        db.add(session)
    db.commit()
    return {"status": "ok"}

@app.post("/api/register/scan")
async def api_register_scan(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    student_id = data.get("student_id")
    rfid_uid = data.get("rfid_uid")
    if not student_id or not rfid_uid:
        return JSONResponse({"error": "missing data"}, status_code=400)

    session = db.query(RegistrationSession).filter(RegistrationSession.student_id == student_id).first()
    if not session or (session.expires_at and session.expires_at < datetime.utcnow()):
        return JSONResponse({"error": "no session or expired"}, status_code=400)

    # 第一次刷卡 (step 0)
    if session.step == 0:
        # 檢查此 UID 是否已被其他人綁定
        if db.query(User).filter(and_(User.rfid_uid == rfid_uid, User.student_id != student_id)).first():
            return JSONResponse({"error": "uid_already_bound"}, status_code=400)
        
        session.first_uid = rfid_uid
        session.step = 1
        session.expires_at = datetime.utcnow() + timedelta(seconds=90)
        db.commit()
        
        # 記錄第一次刷卡
        db.add(AccessLog(student_id=student_id, rfid_uid=rfid_uid, action="SCAN_1"))
        db.commit()
        
        return {"status": "first_scan_ok", "message": "第一次刷卡成功，請再刷一次相同的卡"}

    # 第二次刷卡 (step 1)
    if session.step == 1:
        if session.first_uid == rfid_uid:
            # 兩次刷卡一致，進行綁定
            user = db.query(User).filter(User.student_id == student_id).first()
            if not user:
                return JSONResponse({"error": "user_not_found"}, status_code=404)
            
            # 再次檢查是否有人已綁定此卡
            other = db.query(User).filter(and_(User.rfid_uid == rfid_uid, User.student_id != student_id)).first()
            if other:
                db.delete(session)
                db.commit()
                return JSONResponse({"error": "uid_already_bound_by_other"}, status_code=400)
            
            # 綁定卡號到用戶
            user.rfid_uid = rfid_uid
            db.add(AccessLog(student_id=student_id, rfid_uid=rfid_uid, action="bind"))
            db.delete(session)
            db.commit()
            
            send_telegram(f"綁定成功：{user.name} ({student_id}) 已綁定卡號")
            return {"status": "bound", "message": "綁定成功"}
        else:
            # 兩次刷卡不一致，重置回 step 0
            session.first_uid = None
            session.step = 0
            session.expires_at = datetime.utcnow() + timedelta(seconds=90)
            db.commit()
            return JSONResponse({"error": "mismatch", "message": "兩次刷卡不一致，請重新開始"}, status_code=400)

# === 前端網頁（保持不變）===
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

@app.post("/register")
async def register_post(request: Request, student_id: str = Form(...), name: str = Form(...), db: Session = Depends(get_db)):
    student_id = student_id.strip()
    name = name.strip()

    existing = db.query(User).filter(User.student_id == student_id).first()
    if existing and existing.email_verified and existing.rfid_uid:
        return JSONResponse({"error": "此學號已完成註冊，請直接刷卡進門"}, status_code=400)

    # 產生驗證令牌 (一次性,不設過期時間)
    token = secrets.token_urlsafe(32)
    
    if existing:
        existing.name = name
        existing.verification_token = token
        existing.token_expires_at = None  # 不設過期時間
        existing.email_verified = 0
    else:
        existing = User(
            student_id=student_id, 
            name=name,
            verification_token=token,
            token_expires_at=None,  # 不設過期時間
            email_verified=0
        )
        db.add(existing)
    db.commit()

    # 發送驗證信
    if not SMTP_USER or not SMTP_PASSWORD:
        # SMTP 未設定，直接導到驗證頁面並顯示手動驗證連結
        print(f"[開發模式] 驗證連結: {SERVER_URL}/verify?token={token}")
        send_telegram(f"新註冊待驗證：{name} ({student_id})")
        # 開發模式：自動生成驗證連結並顯示
        return templates.TemplateResponse("verify.html", {
            "request": request, 
            "dev_mode": True,
            "verify_link": f"{SERVER_URL}/verify?token={token}",
            "student_id": student_id
        })
    
    email_sent = send_verification_email(student_id, name, token)
    
    if email_sent:
        send_telegram(f"新註冊待驗證：{name} ({student_id})")
        return RedirectResponse(url="/verify", status_code=303)
    else:
        return JSONResponse({"error": "郵件發送失敗，請稍後再試"}, status_code=500)

@app.get("/verify")
async def verify_page(request: Request, token: str = None, db: Session = Depends(get_db)):
    """顯示驗證提示頁面或處理驗證"""
    if not token:
        # 沒有 token，顯示提示頁面
        return templates.TemplateResponse("verify.html", {"request": request})
    
    # 有 token，處理驗證
    user = db.query(User).filter(User.verification_token == token).first()
    
    if not user:
        raise HTTPException(status_code=404, detail="驗證連結無效或已使用")
    
    # 移除過期檢查,改為檢查是否已經驗證過
    if user.email_verified == 1:
        raise HTTPException(status_code=400, detail="此連結已使用過,請勿重複驗證")
    
    # 標記為已驗證並清除 token (使連結失效)
    user.email_verified = 1
    user.verification_token = None
    user.token_expires_at = None
    db.commit()
    
    send_telegram(f"信箱驗證成功：{user.name} ({user.student_id})")
    
    # 重導到刷卡綁定流程
    threading.Thread(target=notify_pi_register_bg, args=(user.student_id,)).start()
    
    return RedirectResponse(url=f"/bind?student_id={user.student_id}", status_code=303)

@app.get("/bind")
async def bind_page(request: Request, student_id: str, db: Session = Depends(get_db)):
    """刷卡綁定頁面"""
    user = db.query(User).filter(User.student_id == student_id).first()
    if not user or not user.email_verified:
        raise HTTPException(status_code=403, detail="請先完成信箱驗證")
    
    # 建立註冊 session
    expires = datetime.utcnow() + timedelta(seconds=90)
    session = db.query(RegistrationSession).filter(RegistrationSession.student_id == student_id).first()
    if session:
        session.first_uid = None
        session.step = 0
        session.expires_at = expires
    else:
        session = RegistrationSession(student_id=student_id, expires_at=expires)
        db.add(session)
    db.commit()
    
    # 進入註冊模式(讓 RFID 讀取器知道)
    global current_registering_student_id
    with registration_mode_lock:
        current_registering_student_id = student_id
    logger.info(f"[註冊模式] 啟動 - 目標學號: {student_id}")
    
    return templates.TemplateResponse("bind.html", {"request": request, "user": user})

@app.get("/check_status/{student_id}")
async def check_status(student_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.student_id == student_id).first()
    
    # 檢查是否有進行中的 registration session
    session = db.query(RegistrationSession).filter(
        RegistrationSession.student_id == student_id,
        RegistrationSession.expires_at > datetime.now()
    ).first()
    
    session_info = None
    if session:
        session_info = {
            "step": session.step,
            "expires_at": session.expires_at.isoformat(),
            "first_rfid_uid": session.first_rfid_uid
        }
    
    return {
        "bound": bool(user and user.rfid_uid),
        "session": session_info
    }

@app.get("/success")
async def success(request: Request, student_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.student_id == student_id).first()
    if not user:
        raise HTTPException(404)
    return templates.TemplateResponse("success.html", {"request": request, "user": user})

# Pi 接收註冊模式通知
@app.post("/mode/register")
def enter_register_mode(data: dict):
    student_id = data.get("student_id")
    global current_registering_student_id
    with registration_mode_lock:
        current_registering_student_id = student_id
    logger.info(f"[註冊模式] 啟動 - 目標學號: {student_id}")
    return {"status": "ok"}

# ================= RFID 讀取功能 =================
SCANCODE_MAP = {2: '1', 3: '2', 4: '3', 5: '4', 6: '5',
                7: '6', 8: '7', 9: '8', 10: '9', 11: '0'}

# 全域變數:目前註冊中的學號
current_registering_student_id = None
registration_mode_lock = threading.Lock()

def find_rfid_device():
    """自動偵測 RFID 裝置"""
    if not EVDEV_AVAILABLE:
        return None
    try:
        devs = list_devices()
        for d in devs:
            try:
                dev = InputDevice(d)
                name = dev.name.lower()
                if any(k in name for k in ('rfid', 'scanner', 'keyboard', 'hid')):
                    logger.info(f"[RFID] 找到候選裝置: {dev.name} ({d})")
                    return d
            except Exception:
                continue
        if devs:
            logger.info(f"[RFID] 使用第一個輸入裝置: {devs[0]}")
            return devs[0]
    except Exception as e:
        logger.warning(f"[RFID] 偵測裝置失敗: {e}")
    return None

async def process_rfid_scan(card_uid: str):
    """處理刷卡事件 (統一接口)"""
    logger.info(f"[RFID] 偵測到卡號: {card_uid}")
    
    with registration_mode_lock:
        target_student_id = current_registering_student_id
    
    if target_student_id:
        # 註冊模式:呼叫註冊 API
        logger.info(f"[RFID] 註冊模式 - 學號 {target_student_id} 刷卡 {card_uid}")
        try:
            async with asyncio.timeout(5):
                response = await asyncio.to_thread(
                    requests.post,
                    "http://localhost:8000/api/register/scan",
                    json={"student_id": target_student_id, "rfid_uid": card_uid},
                    timeout=5
                )
                data = response.json()
                logger.info(f"[RFID] 註冊回應: {data}")
                
                if data.get("status") == "bound":
                    # 綁定成功,退出註冊模式
                    with registration_mode_lock:
                        current_registering_student_id = None
                    logger.info(f"[RFID] 綁定成功!退出註冊模式")
        except Exception as e:
            logger.error(f"[RFID] 註冊 API 呼叫失敗: {e}")
    else:
        # 正常模式:呼叫門禁驗證 API
        logger.info(f"[RFID] 正常模式 - 驗證卡號 {card_uid}")
        try:
            async with asyncio.timeout(5):
                response = await asyncio.to_thread(
                    requests.post,
                    "http://localhost:8000/api/scan",
                    json={"rfid_uid": card_uid},
                    timeout=5
                )
                data = response.json()
                if data.get("status") == "allow":
                    logger.info(f"[✅ 允許進入] {data.get('name')} ({data.get('student_id')})")
                else:
                    logger.info(f"[🔴 拒絕] 卡號未註冊")
        except Exception as e:
            logger.error(f"[RFID] 驗證 API 呼叫失敗: {e}")

def rfid_reader_loop():
    """RFID 讀取主迴圈 (背景執行緒)"""
    if not EVDEV_AVAILABLE:
        logger.warning("[RFID] evdev 不可用,無法啟動 RFID 讀取")
        return
    
    logger.info(f"[RFID] 啟動讀卡機監聽...")
    
    device_path = RFID_DEVICE_PATH
    device = None
    
    # 嘗試開啟裝置
    try:
        if os.path.exists(device_path):
            device = InputDevice(device_path)
            logger.info(f"[RFID] 使用裝置: {device.name} ({device_path})")
        else:
            # 自動偵測
            auto_path = find_rfid_device()
            if auto_path:
                device = InputDevice(auto_path)
                logger.info(f"[RFID] 自動偵測到裝置: {device.name} ({auto_path})")
    except Exception as e:
        logger.error(f"[RFID] 裝置開啟失敗: {e}")
        logger.info("[RFID] 提示: 1) 確認裝置路徑 2) 使用 sudo 執行 3) 將使用者加入 input 群組")
        return
    
    if not device:
        logger.error("[RFID] 找不到可用的 RFID 裝置")
        return
    
    current_code = ""
    logger.info("[RFID] ✅ 讀卡機就緒,等待刷卡...")
    
    try:
        for event in device.read_loop():
            if event.type == ecodes.EV_KEY and event.value == 1:  # Key down
                if event.code == 28:  # Enter 鍵
                    if current_code:
                        card_uid = current_code
                        # 使用 asyncio 處理
                        asyncio.run(process_rfid_scan(card_uid))
                        current_code = ""
                elif event.code in SCANCODE_MAP:
                    current_code += SCANCODE_MAP[event.code]
    except KeyboardInterrupt:
        logger.info("[RFID] 讀卡機監聽已停止")
    except Exception as e:
        logger.error(f"[RFID] 讀取錯誤: {e}")

def start_rfid_reader():
    """啟動 RFID 讀取背景執行緒"""
    if RFID_ENABLED and EVDEV_AVAILABLE:
        thread = threading.Thread(target=rfid_reader_loop, daemon=True)
        thread.start()
        logger.info("[RFID] 背景讀卡執行緒已啟動")
    elif RFID_ENABLED and not EVDEV_AVAILABLE:
        logger.warning("[RFID] RFID_ENABLED=true 但 evdev 未安裝,請執行: pip install evdev")
    else:
        logger.info("[RFID] RFID 讀取功能未啟用 (設定 RFID_ENABLED=true 啟用)")
