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
    # Action: ENTRY (已進門), ERROR (被拒絕), SCAN_1 (綁定第一步), BIND (綁定完成)
    action = Column(String(20), nullable=False) 
    timestamp = Column(TIMESTAMP(timezone=True), server_default=func.now())

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- [新增] 全域變數：暫存第一次刷卡紀錄 ---
# 格式: { "student_id": "RFID_UID" }
# 用於雙重驗證，暫存在記憶體中比寫入 DB 快且乾淨
temp_scans = {}

# --- TG 發送小幫手 ---
def send_tg_message(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"TG 發送失敗: {e}")

# --- TG 發送圖片小幫手 ---
def send_tg_photo(photo_path, caption):
    if not TG_TOKEN or not TG_CHAT_ID: return
    
    # 檢查圖片是否存在，不在就只傳文字
    if not os.path.exists(photo_path):
        print(f"❌ 找不到圖片: {photo_path}")
        send_tg_message(caption)
        return

    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto"
        with open(photo_path, 'rb') as f:
            files = {'photo': f}
            # parse_mode='HTML' 讓文字支援粗體
            data = {'chat_id': TG_CHAT_ID, 'caption': caption, 'parse_mode': 'HTML'}
            requests.post(url, data=data, files=files, timeout=10)
    except Exception as e:
        print(f"TG 發送圖片失敗: {e}")

# ================= 📊 統計與報告邏輯 (保持原樣) =================

def check_crowd_alert(db: Session):
    """【功能 1】人流提醒"""
    one_hour_ago = datetime.now() - timedelta(hours=1)
    count = db.query(AccessLog).filter(
        AccessLog.timestamp >= one_hour_ago,
        AccessLog.action == "ENTRY"
    ).count()
    
    if count >= CROWD_THRESHOLD:
        send_tg_message(f"💨 <b>空氣品質提醒</b>\n過去一小時已有 {count} 人次進出，請大家記得開窗換氣！")

async def scheduled_daily_report():
    """【功能 2】每日 18:00 報告"""
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

async def scheduled_weekly_leaderboard():
    """【功能 3】每週排行榜"""
    # (省略重複邏輯，與之前相同，請保留您的實作)
    pass 

# --- 排程器 ---
scheduler = AsyncIOScheduler()
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("⏰ 排程系統啟動中...")
    scheduler.add_job(scheduled_daily_report, 'cron', hour=18, minute=0)
    # scheduler.add_job(scheduled_weekly_leaderboard, 'cron', day_of_week='sun', hour=20, minute=0)
    scheduler.start()
    yield
    pass

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ================= Routes (整合雙重驗證流程) =================

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

    # 1. 檢查並建立 User (UID=None)
    existing_user = db.query(User).filter(User.student_id == student_id).first()

    try:
        if existing_user:
            if existing_user.rfid_uid:
                return JSONResponse(status_code=400, content={"message": "❌ 此學號已綁定卡片，請直接刷卡進門。"})
            else:
                existing_user.name = name
                db.commit()
        else:
            new_user = User(student_id=student_id, name=name)
            db.add(new_user)
            db.commit()
        
        # [修改] 清除舊的暫存，確保流程重置
        if student_id in temp_scans:
            del temp_scans[student_id]

        # 2. 通知樹莓派切換模式
        if PI_API_URL:
            try:
                requests.post(
                    f"{PI_API_URL}/mode/register",
                    json={"student_id": student_id},
                    timeout=3 
                )
                print(f"✅ 已通知 Pi 切換模式: {student_id}")
            except Exception as e:
                print(f"⚠️ 無法連線到 Pi: {e}")
        
        msg = (
            f"📝 <b>新用戶註冊申請</b>\n"
            f"------------------\n"
            f"姓名：{name}\n"
            f"學號：{student_id}\n"
            f"狀態：等待雙重刷卡驗證..."
        )
        send_tg_message(msg)

        return JSONResponse({"status": "ready_to_scan", "student_id": student_id})

    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="註冊失敗 (資料庫錯誤)")
    except Exception as e:
        print(f"系統錯誤: {e}")
        raise HTTPException(status_code=500, detail="伺服器內部錯誤")

@app.post("/cancel_register")
async def cancel_register(student_id: str = Form(...), db: Session = Depends(get_db)):
    # [修改] 清除暫存
    if student_id in temp_scans:
        del temp_scans[student_id]

    user = db.query(User).filter(User.student_id == student_id).first()
    if user and not user.rfid_uid:
        db.delete(user)
        db.commit()
        send_tg_message(f"❌ <b>綁定逾時</b>\n學號：{student_id}\n資料已清除")
        return {"status": "cancelled"}
    return {"status": "ignored"}

@app.get("/check_status/{student_id}")
async def check_status(student_id: str, db: Session = Depends(get_db)):
    """[修改] 前端輪詢邏輯：增加 step_1 判斷"""
    user = db.query(User).filter(User.student_id == student_id).first()
    
    # 狀態 3: 綁定完成
    if user and user.rfid_uid:
        return {"status": "bound", "rfid_uid": user.rfid_uid}
    
    # 狀態 2: 記憶體中有第一次刷卡紀錄 -> Step 1 完成
    if student_id in temp_scans:
        return {"status": "step_1"}
    
    # 狀態 1: 等待中
    return {"status": "waiting"}

@app.get("/success", response_class=HTMLResponse)
async def success_page(request: Request, student_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.student_id == student_id).first()
    if not user: raise HTTPException(status_code=404)
    return templates.TemplateResponse("success.html", {"request": request, "user": user})

# ================= 核心 API：雙重刷卡邏輯 =================

@app.post("/rfid_scan")
async def rfid_scan(
    rfid_uid: str = Form(...),
    student_id: str = Form(None), 
    action: str = Form(default="ENTRY"), 
    db: Session = Depends(get_db),
):
    # --- 狀況 1: 讀卡機已經開門 (Normal Mode) ---
    if action == "ENTRY":
        user = db.query(User).filter(User.rfid_uid == rfid_uid).first()
        if user:
            log = AccessLog(student_id=user.student_id, rfid_uid=rfid_uid, action="ENTRY")
            db.add(log); db.commit()
            
            # 【修改點】改為發送圖片 + 歡迎詞 (拿掉卡號)
            photo_path = "static/welcome.jpeg"
            caption = f"👋 <b>歡迎！{user.name} 已進入 MOLI</b>" # 這裡不含 rfid_uid
            
            send_tg_photo(photo_path, caption)
            
            check_crowd_alert(db) 
            return {"status": "logged", "message": "Entry logged"}
        else:
            return {"status": "error", "message": "User not found in cloud DB"}

    # --- 狀況 2: 讀卡機拒絕進入 (Normal Mode) ---
    if action == "ERROR":
        send_tg_message(f"⚠️ <b>警告：陌生卡片刷卡</b>\n卡號：{rfid_uid}")
        return {"status": "alerted", "message": "Stranger alert sent"}

    # --- 狀況 3: 註冊綁定模式 (Register Mode) ---
    # Pi 傳來 student_id，代表正在進行綁定
    if student_id:
        pending_user = db.query(User).filter(User.student_id == student_id).first()
        
        if not pending_user:
            return JSONResponse(status_code=400, content={"message": "用戶資料不存在，請重新填表"})

        # 防呆：檢查卡片是否已被其他人綁定
        if db.query(User).filter(User.rfid_uid == rfid_uid).first():
             return JSONResponse(status_code=400, content={"message": "❌ 此卡片已被他人使用！"})

        # [雙重驗證邏輯]
        
        # A. 檢查是否為「第二刷」(記憶體有暫存)
        if student_id in temp_scans:
            first_uid = temp_scans[student_id]
            
            if first_uid == rfid_uid:
                # --- 配對成功：執行綁定 ---
                pending_user.rfid_uid = rfid_uid
                db.commit()
                
                # 寫入綁定 Log
                db.add(AccessLog(student_id=student_id, rfid_uid=rfid_uid, action="BIND"))
                db.commit()
                
                # 清除暫存
                del temp_scans[student_id]
                
                send_tg_message(f"✅ <b>綁定成功！</b>\n用戶：{pending_user.name}\n卡號：{rfid_uid}")
                return JSONResponse({"status": "bound", "message": "綁定成功"})
            else:
                # --- 配對失敗：卡號不一致 ---
                del temp_scans[student_id] # 清除，強迫重來
                return JSONResponse(status_code=400, content={"message": "❌ 兩次卡片不一致，請重刷"})

        # B. 這是「第一刷」
        else:
            temp_scans[student_id] = rfid_uid
            # 寫入 SCAN_1 Log (可選，這裡寫入是為了留紀錄)
            db.add(AccessLog(student_id=student_id, rfid_uid=rfid_uid, action="SCAN_1"))
            db.commit()
            
            return JSONResponse({"status": "step_1", "message": "請再次刷卡以確認綁定"})

    return JSONResponse(status_code=400, content={"message": "Invalid request"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
