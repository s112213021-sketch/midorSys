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
import logging
from dotenv import load_dotenv
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# 載入環境變數
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
PI_API_URL = os.getenv("PI_API_URL")
TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")
API_KEY = os.getenv("API_KEY")

# 門禁設定：換氣提醒門檻 (1小時內進入人數)
CROWD_THRESHOLD = 10 

if not DATABASE_URL:
    raise ValueError("❌ 未設定 DATABASE_URL 環境變數")

# 資料庫設定
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- 資料庫模型 ---
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
    action = Column(String(20), nullable=False)  # ENTRY, SCAN_1, BIND
    timestamp = Column(TIMESTAMP(timezone=True), server_default=func.now())

# 建立資料表
Base.metadata.create_all(bind=engine)

# DB Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- TG 發送小幫手 ---
def send_tg_message(text):
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=3)
    except Exception as e:
        print(f"TG 發送失敗: {e}")

# --- 統計與提醒邏輯 ---
def check_crowd_alert(db: Session):
    one_hour_ago = datetime.utcnow() + timedelta(hours=8) - timedelta(hours=1)
    count = db.query(AccessLog).filter(AccessLog.timestamp >= one_hour_ago, AccessLog.action == "ENTRY").count()
    if count >= CROWD_THRESHOLD:
        send_tg_message(f"💨 <b>空氣品質提醒</b>\n一小時內已有 {count} 人次進入，請記得開窗！")

async def scheduled_daily_report():
    print("📊 執行每日報告統計...")
    pass

# --- App 初始化與排程 ---
scheduler = AsyncIOScheduler()
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("⏰ 排程系統啟動中...")
    scheduler.add_job(scheduled_daily_report, 'cron', hour=18, minute=0)
    scheduler.start()
    yield
    pass

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ================= 頁面路由 =================

@app.get("/", response_class=HTMLResponse)
async def register_form(request: Request):
    return templates.TemplateResponse("register.html", {
        "request": request, 
        "pi_api_url": PI_API_URL, 
        "api_key": API_KEY
    })

@app.get("/success", response_class=HTMLResponse)
async def success_page(request: Request, student_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.student_id == student_id).first()
    if not user: raise HTTPException(status_code=404)
    return templates.TemplateResponse("success.html", {"request": request, "user": user})

# ================= API 邏輯路由 =================

@app.post("/register")
async def register_post(
    request: Request, 
    student_id: str = Form(...), 
    name: str = Form(...), 
    db: Session = Depends(get_db)
):
    student_id = student_id.strip()
    name = name.strip()
    existing = db.query(User).filter(User.student_id == student_id).first()

    try:
        if existing:
            if existing.rfid_uid:
                return JSONResponse(status_code=400, content={"detail": "❌ 此學號已綁定，請直接刷卡。"})
            else:
                existing.name = name
        else:
            new_user = User(student_id=student_id, name=name)
            db.add(new_user)
        
        db.commit()
        
        # 通知 Pi 切換到註冊模式 (關鍵除錯點)
        if PI_API_URL:
            pi_target = f"{PI_API_URL}/mode/register"
            print(f"📡 正在嘗試連線到 Pi: {pi_target}") # Log 1
            try:
                resp = requests.post(
                    pi_target, 
                    json={"student_id": student_id}, 
                    headers={"X-API-KEY": API_KEY}, 
                    timeout=5 # 增加 timeout 避免網路波動
                )
                if resp.status_code == 200:
                    print("✅ Pi 已成功切換至註冊模式")
                else:
                    print(f"⚠️ Pi 回傳錯誤代碼: {resp.status_code}, 內容: {resp.text}")
            except Exception as e:
                print(f"❌ 無法連線到 Pi (請檢查 PI_API_URL 與 Tunnel): {e}")
        else:
            print("⚠️ 未設定 PI_API_URL，略過 Pi 通知")
        
        return JSONResponse({"status": "ready_to_scan", "student_id": student_id})
    except Exception as e:
        db.rollback()
        print(f"System Error: {e}")
        raise HTTPException(status_code=500, detail="系統錯誤")

@app.post("/cancel_register")
async def cancel_register(student_id: str = Form(...), db: Session = Depends(get_db)):
    """【功能】逾時自動刪除無效資料"""
    user = db.query(User).filter(User.student_id == student_id).first()
    
    # 符合流程：只刪除「還沒綁定 UID」的 user
    if user and not user.rfid_uid:
        db.delete(user)
        db.query(AccessLog).filter(AccessLog.student_id == student_id, AccessLog.action == "SCAN_1").delete()
        db.commit()
        print(f"♻️ [逾時清理] 已刪除未完成註冊資料：{student_id}")
        return {"status": "cancelled", "message": "已清除無效資料"}
    
    return {"status": "ignored"}

@app.get("/check_status/{student_id}")
async def check_status(student_id: str, db: Session = Depends(get_db)):
    """【功能】前端輪詢用"""
    user = db.query(User).filter(User.student_id == student_id).first()
    
    if not user:
        return JSONResponse(status_code=404, content={"status": "error", "message": "User deleted (timeout)"})

    # 1. 綁定完成
    if user.rfid_uid: 
        return {"status": "bound", "rfid_uid": user.rfid_uid}
    
    # 2. 檢查是否有第一次刷卡紀錄 (SCAN_1)
    recent_scan = db.query(AccessLog).filter(
        AccessLog.student_id == student_id, 
        AccessLog.action == "SCAN_1",
        AccessLog.timestamp > datetime.utcnow() + timedelta(hours=8) - timedelta(seconds=60)
    ).first()
    
    if recent_scan: 
        return {"status": "step_1"}
    
    # 3. 等待中
    return {"status": "waiting"}

@app.post("/rfid_scan")
async def rfid_scan(
    rfid_uid: str = Form(...),
    student_id: str = Form(None), 
    action: str = Form(default="ENTRY"),
    db: Session = Depends(get_db),
):
    rfid_uid = rfid_uid.strip()

    # ================= 註冊模式邏輯 =================
    if student_id:
        pending_user = db.query(User).filter(User.student_id == student_id).first()
        
        if not pending_user:
            return JSONResponse(status_code=404, content={"status": "error", "message": "找不到申請資料"})
        
        if pending_user.rfid_uid:
             return JSONResponse(status_code=400, content={"status": "error", "message": "此學號已綁定卡片"})

        # 檢查卡片是否已被其他人佔用
        card_owner = db.query(User).filter(User.rfid_uid == rfid_uid).first()
        if card_owner:
             return JSONResponse(status_code=400, content={"status": "error", "message": f"卡片已被 {card_owner.name} 使用"})

        # 檢查是否為第 2 次刷卡
        last_scan = db.query(AccessLog).filter(
            AccessLog.student_id == student_id,
            AccessLog.action == "SCAN_1",
            AccessLog.timestamp > datetime.utcnow() + timedelta(hours=8) - timedelta(seconds=60)
        ).order_by(desc(AccessLog.timestamp)).first()

        if not last_scan:
            # --- 步驟 1：第一次刷卡 ---
            log = AccessLog(student_id=student_id, rfid_uid=rfid_uid, action="SCAN_1")
            db.add(log); db.commit()
            return JSONResponse({"status": "step_1", "message": "讀取成功！請「再次刷卡」確認..."})
        else:
            # --- 步驟 2：第二次刷卡比對 ---
            if last_scan.rfid_uid == rfid_uid:
                pending_user.rfid_uid = rfid_uid
                log_bind = AccessLog(student_id=student_id, rfid_uid=rfid_uid, action="BIND")
                db.add(log_bind); db.commit()
                send_tg_message(f"✅ <b>新成員註冊成功</b>\n姓名：{pending_user.name}\n學號：{student_id}")
                return JSONResponse({"status": "bound", "message": "綁定成功"})
            else:
                return JSONResponse(status_code=400, content={"status": "error", "message": "兩次卡片不符，請重試"})

    # ================= 一般進門邏輯 =================
    if action == "ENTRY":
        user = db.query(User).filter(User.rfid_uid == rfid_uid).first()
        if user:
            log = AccessLog(student_id=user.student_id, rfid_uid=rfid_uid, action="ENTRY")
            db.add(log); db.commit()
            check_crowd_alert(db)
            return {"status": "logged", "message": f"Welcome {user.name}"}
        else:
            return {"status": "error", "message": "未知卡片"}
    
    if action == "ERROR":
        send_tg_message(f"⚠️ <b>陌生卡片刷入警告</b>\nUID: {rfid_uid}")
        return {"status": "alerted"}

    return JSONResponse(status_code=400, content={"message": "Invalid request"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
