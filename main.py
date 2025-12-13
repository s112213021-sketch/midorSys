from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, Column, String, TIMESTAMP, func, Integer, ForeignKey, desc
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import IntegrityError
import os
import requests
from dotenv import load_dotenv
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# 載入 .env
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
PI_API_URL = os.getenv("PI_API_URL")
TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

# 設定：換氣提醒門檻 (過去 1 小時內超過 10 人次刷卡)
CROWD_THRESHOLD = 10 

if not DATABASE_URL:
    raise ValueError("❌ 未設定 DATABASE_URL 環境變數")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- Model 定義 ---
class User(Base):
    __tablename__ = "users"
    student_id = Column(String(20), primary_key=True, index=True)
    name = Column(String(50), nullable=False)
    rfid_uid = Column(String(50), unique=True, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

class AccessLog(Base):
    __tablename__ = "access_logs"
    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(String(20), ForeignKey("users.student_id"), nullable=False)
    rfid_uid = Column(String(50), nullable=False)
    # Action: ENTRY, ERROR, SCAN_1, BIND
    # 這裡 String(20) 足夠容納所有大寫動作
    action = Column(String(20), nullable=False) 
    timestamp = Column(TIMESTAMP(timezone=True), server_default=func.now())

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- 全域變數：暫存第一次刷卡紀錄 ---
temp_scans = {}

# --- TG 發送小幫手 ---
def send_tg_message(text):
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"TG 發送失敗: {e}")

# --- TG 發送圖片小幫手 ---
def send_tg_photo(photo_path, caption):
    if not TG_TOKEN or not TG_CHAT_ID: return
    
    if not os.path.exists(photo_path):
        # 找不到圖就傳文字
        send_tg_message(caption)
        return

    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto"
        with open(photo_path, 'rb') as f:
            files = {'photo': f}
            data = {'chat_id': TG_CHAT_ID, 'caption': caption, 'parse_mode': 'HTML'}
            requests.post(url, data=data, files=files, timeout=10)
    except Exception as e:
        print(f"TG 發送圖片失敗: {e}")

# ================= 📊 統計邏輯 =================

def check_crowd_alert(db: Session):
    one_hour_ago = datetime.now() - timedelta(hours=1)
    count = db.query(AccessLog).filter(
        AccessLog.timestamp >= one_hour_ago,
        AccessLog.action == "ENTRY"
    ).count()
    if count >= CROWD_THRESHOLD:
        send_tg_message(f"💨 <b>空氣品質提醒</b>\n過去一小時已有 {count} 人次進出，請大家記得開窗換氣！")

async def scheduled_daily_report():
    print("📊 執行每日報告統計...")
    db = SessionLocal()
    try:
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        logs = db.query(AccessLog).filter(
            AccessLog.timestamp >= today_start,
            AccessLog.action == "ENTRY"
        ).all()
        
        if not logs: return

        visit_counts = {}
        for log in logs:
            visit_counts[log.student_id] = visit_counts.get(log.student_id, 0) + 1
        
        top_student = max(visit_counts, key=visit_counts.get)
        max_visits = visit_counts[top_student]
        
        user_top = db.query(User).filter(User.student_id == top_student).first()
        top_name = user_top.name if user_top else top_student

        last_log = max(logs, key=lambda x: x.timestamp)
        user_last = db.query(User).filter(User.student_id == last_log.student_id).first()
        last_name = user_last.name if user_last else last_log.student_id

        msg = (
            f"📊 <b>今日實驗室觀察報告</b>\n"
            f"--------------------\n"
            f"🏆 最常來的：<b>{top_name}</b> (第 {max_visits} 次)\n"
            f"🌙 最晚離開：<b>{last_name}</b>\n"
        )
        send_tg_message(msg)
    except Exception as e:
        print(f"每日報告錯誤: {e}")
    finally:
        db.close()

# --- 排程器 ---
scheduler = AsyncIOScheduler()
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("⏰ 排程系統啟動中...")
    scheduler.add_job(scheduled_daily_report, 'cron', hour=18, minute=0)
    scheduler.start()
    yield

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ================= Routes =================

@app.get("/", response_class=HTMLResponse)
async def register_form(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "pi_api_url": PI_API_URL, "error": None})

@app.post("/register")
async def register_post(
    request: Request,
    student_id: str = Form(...),
    name: str = Form(...),
    db: Session = Depends(get_db),
):
    student_id = student_id.strip()
    name = name.strip()

    existing_user = db.query(User).filter(User.student_id == student_id).first()
    try:
        if existing_user:
            if existing_user.rfid_uid:
                return JSONResponse(status_code=400, content={"message": "❌ 已綁定"})
            else:
                existing_user.name = name
                db.commit()
        else:
            db.add(User(student_id=student_id, name=name))
            db.commit()
        
        if student_id in temp_scans: del temp_scans[student_id]

        if PI_API_URL:
            try: requests.post(f"{PI_API_URL}/mode/register", json={"student_id": student_id}, timeout=3)
            except: pass
        
        send_tg_message(f"📝 <b>新用戶申請</b>\n姓名：{name}\n學號：{student_id}")
        return JSONResponse({"status": "ready_to_scan", "student_id": student_id})

    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="註冊失敗")
    except Exception:
        raise HTTPException(status_code=500, detail="Server Error")

@app.post("/cancel_register")
async def cancel_register(student_id: str = Form(...), db: Session = Depends(get_db)):
    if student_id in temp_scans: del temp_scans[student_id]
    user = db.query(User).filter(User.student_id == student_id).first()
    if user and not user.rfid_uid:
        db.delete(user); db.commit()
    return {"status": "cancelled"}

@app.get("/check_status/{student_id}")
async def check_status(student_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.student_id == student_id).first()
    if user and user.rfid_uid: return {"status": "bound", "rfid_uid": user.rfid_uid}
    if student_id in temp_scans: return {"status": "step_1"}
    return {"status": "waiting"}

@app.get("/success", response_class=HTMLResponse)
async def success_page(request: Request, student_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.student_id == student_id).first()
    if not user: raise HTTPException(status_code=404)
    return templates.TemplateResponse("success.html", {"request": request, "user": user})

# ================= 核心 API：雙重刷卡 =================

@app.post("/rfid_scan")
async def rfid_scan(
    rfid_uid: str = Form(...),
    student_id: str = Form(None), 
    action: str = Form(default="ENTRY"), 
    db: Session = Depends(get_db),
):
    # 1. 一般進門 (ENTRY)
    if action == "ENTRY":
        user = db.query(User).filter(User.rfid_uid == rfid_uid).first()
        if user:
            # 這裡寫入大寫 ENTRY，因為我們剛剛 reset_db 解除了限制
            log = AccessLog(student_id=user.student_id, rfid_uid=rfid_uid, action="ENTRY")
            db.add(log); db.commit()
            
            # 傳圖片
            photo_path = "static/welcome.jpeg"
            caption = f"👋 <b>歡迎！{user.name} 已進入 MOLI</b>"
            send_tg_photo(photo_path, caption)
            
            check_crowd_alert(db) 
            return {"status": "logged", "message": "Entry logged"}
        else:
            return {"status": "error", "message": "User not found"}

    # 2. 陌生人 (ERROR)
    if action == "ERROR":
        send_tg_message(f"⚠️ <b>警告：陌生卡片刷卡</b>\n卡號：{rfid_uid}")
        return {"status": "alerted", "message": "Stranger alert sent"}

    # 3. 註冊綁定 (Register Mode)
    if student_id:
        pending_user = db.query(User).filter(User.student_id == student_id).first()
        if not pending_user: return JSONResponse(status_code=400, content={"message": "用戶不存在"})
        if db.query(User).filter(User.rfid_uid == rfid_uid).first():
             return JSONResponse(status_code=400, content={"message": "❌ 此卡片已被使用"})

        if student_id in temp_scans:
            # 第二刷
            if temp_scans[student_id] == rfid_uid:
                pending_user.rfid_uid = rfid_uid
                db.commit()
                # 寫入 BIND
                db.add(AccessLog(student_id=student_id, rfid_uid=rfid_uid, action="BIND"))
                db.commit()
                del temp_scans[student_id]
                send_tg_message(f"✅ <b>綁定成功！</b>\n用戶：{pending_user.name}")
                return JSONResponse({"status": "bound", "message": "綁定成功"})
            else:
                del temp_scans[student_id]
                return JSONResponse(status_code=400, content={"message": "❌ 卡片不一致"})
        else:
            # 第一刷
            temp_scans[student_id] = rfid_uid
            # 寫入 SCAN_1
            db.add(AccessLog(student_id=student_id, rfid_uid=rfid_uid, action="SCAN_1"))
            db.commit()
            return JSONResponse({"status": "step_1", "message": "請再次刷卡"})

    return JSONResponse(status_code=400, content={"message": "Invalid request"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
    
