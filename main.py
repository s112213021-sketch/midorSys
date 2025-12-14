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
API_KEY = os.getenv("API_KEY")  # 新增：用於前端傳到 Pi

# 設定：換氣提醒門檻 (過去 1 小時內超過 10 人次進入)
CROWD_THRESHOLD = 10 

if not DATABASE_URL:
    raise ValueError("❌ 未設定 DATABASE_URL 環境變數")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

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
    action = Column(String(20), nullable=False) 
    timestamp = Column(TIMESTAMP(timezone=True), server_default=func.now())

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- TG 發送小幫手 (加 retry) ---
def send_tg_message(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    for attempt in range(3):  # retry 3 次
        try:
            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
            payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
            requests.post(url, json=payload, timeout=5)
            return
        except Exception as e:
            print(f"TG 發送失敗 (嘗試 {attempt+1}): {e}")
            time.sleep(1)  # 延遲重試

# ================= 📊 統計與報告邏輯 =================

def check_crowd_alert(db: Session):
    """【功能 1】人流提醒"""
    one_hour_ago = datetime.utcnow() + timedelta(hours=8) - timedelta(hours=1)  # 加時區 (台灣 UTC+8)
    count = db.query(AccessLog).filter(
        AccessLog.timestamp >= one_hour_ago,
        AccessLog.action == "ENTRY"
    ).count()
    
    if count >= CROWD_THRESHOLD:
        send_tg_message(f"💨 <b>空氣品質提醒</b>\n過去一小時已有 {count} 人次進入，請大家記得開窗換氣！")

async def scheduled_daily_report():
    """【功能 2】每日 18:00 報告"""
    print("📊 執行每日報告統計...")
    db = SessionLocal()
    try:
        today_start = (datetime.utcnow() + timedelta(hours=8)).replace(hour=0, minute=0, second=0, microsecond=0)
        logs = db.query(AccessLog).filter(
            AccessLog.timestamp >= today_start,
            AccessLog.action == "ENTRY"
        ).all()
        
        if not logs:
            send_tg_message("📊 <b>今日實驗室觀察報告</b>\n今日無訪客記錄。")
            return

        visit_counts = {}
        for log in logs:
            visit_counts[log.student_id] = visit_counts.get(log.student_id, 0) + 1
        
        # 取前 3 名 (處理並列)
        sorted_visits = sorted(visit_counts.items(), key=lambda x: x[1], reverse=True)[:3]
        top_msg = "\n".join([f"🏆 第 {i+1} 名：<b>{db.query(User).filter(User.student_id == sid).first().name or sid}</b> (第 {cnt} 次)" for i, (sid, cnt) in enumerate(sorted_visits)])
        
        last_log = max(logs, key=lambda x: x.timestamp)
        user_last = db.query(User).filter(User.student_id == last_log.student_id).first()
        last_name = user_last.name if user_last else last_log.student_id

        msg = (
            f"📊 <b>今日實驗室觀察報告</b>\n"
            f"--------------------\n"
            f"{top_msg}\n"
            f"🌙 最晚進入：<b>{last_name}</b>\n"
        )
        send_tg_message(msg)
    except Exception as e:
        print(f"每日報告錯誤: {e}")
    finally:
        db.close()

async def scheduled_weekly_leaderboard():
    """【功能 3】每週排行榜"""
    print("📊 執行週排行榜...")
    db = SessionLocal()
    try:
        week_start = (datetime.utcnow() + timedelta(hours=8)) - timedelta(days=7)
        logs = db.query(AccessLog).filter(
            AccessLog.timestamp >= week_start,
            AccessLog.action == "ENTRY"
        ).all()
        
        if not logs:
            send_tg_message("📊 <b>本週實驗室排行榜</b>\n本週無訪客記錄。")
            return

        visit_counts = {}
        for log in logs:
            visit_counts[log.student_id] = visit_counts.get(log.student_id, 0) + 1
        
        sorted_visits = sorted(visit_counts.items(), key=lambda x: x[1], reverse=True)[:3]
        top_msg = "\n".join([f"🏆 第 {i+1} 名：<b>{db.query(User).filter(User.student_id == sid).first().name or sid}</b> (第 {cnt} 次)" for i, (sid, cnt) in enumerate(sorted_visits)])
        
        last_log = max(logs, key=lambda x: x.timestamp)
        user_last = db.query(User).filter(User.student_id == last_log.student_id).first()
        last_name = user_last.name if user_last else last_log.student_id

        msg = (
            f"📊 <b>本週實驗室排行榜</b>\n"
            f"--------------------\n"
            f"{top_msg}\n"
            f"🌙 本週最晚進入：<b>{last_name}</b>\n"
        )
        send_tg_message(msg)
    except Exception as e:
        print(f"週排行錯誤: {e}")
    finally:
        db.close()

# --- 排程器 ---
scheduler = AsyncIOScheduler()
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("⏰ 排程系統啟動中...")
    scheduler.add_job(scheduled_daily_report, 'cron', hour=18, minute=0)
    scheduler.add_job(scheduled_weekly_leaderboard, 'cron', day_of_week='sun', hour=20, minute=0)  # 啟用並實作
    scheduler.start()
    yield
    pass

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def register_form(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "pi_api_url": PI_API_URL, "api_key": API_KEY})  # 新增：傳 API key 給前端

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
                return JSONResponse(status_code=400, content={"message": "❌ 此學號已綁定卡片，請直接刷卡進門。"})
            else:
                existing_user.name = name
                db.commit()
        else:
            new_user = User(student_id=student_id, name=name)
            db.add(new_user)
            db.commit()
        
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
            f"狀態：等待刷卡驗證 (60s)..."
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
    user = db.query(User).filter(User.student_id == student_id).first()
    if user and not user.rfid_uid:
        db.delete(user)
        # 新增：刪相關 logs
        db.query(AccessLog).filter(AccessLog.student_id == student_id, AccessLog.action.in_(["SCAN_1", "BIND"])).delete()
        db.commit()
        send_tg_message(f"❌ <b>綁定逾時</b>\n學號：{student_id}\n資料已清除")
        return {"status": "cancelled"}
    return {"status": "ignored"}

@app.get("/check_status/{student_id}")
async def check_status(student_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.student_id == student_id).first()
    if user and user.rfid_uid: return {"status": "bound", "rfid_uid": user.rfid_uid}
    
    recent = db.query(AccessLog).filter(AccessLog.student_id == student_id, AccessLog.action == "SCAN_1").order_by(desc(AccessLog.timestamp)).first()
    if recent: return {"status": "step_1"}
    return {"status": "waiting"}

@app.get("/success", response_class=HTMLResponse)
async def success_page(request: Request, student_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.student_id == student_id).first()
    if not user: raise HTTPException(status_code=404)
    return templates.TemplateResponse("success.html", {"request": request, "user": user})

@app.post("/rfid_scan")
async def rfid_scan(
    rfid_uid: str = Form(...),
    student_id: str = Form(None), 
    action: str = Form(default="ENTRY"),
    db: Session = Depends(get_db),
):
    if action == "ENTRY":
        user = db.query(User).filter(User.rfid_uid == rfid_uid).first()
        if user:
            log = AccessLog(student_id=user.student_id, rfid_uid=rfid_uid, action="ENTRY")
            db.add(log); db.commit()
            
            send_tg_message(f"👋 <b>你好！{user.name} 已進入 MOLI</b>")
            
            check_crowd_alert(db)
            return {"status": "logged", "message": "Entry logged"}
        else:
            return {"status": "error", "message": "User not found in cloud DB"}

    if action == "ERROR":
        send_tg_message(f"⚠️ <b>警告：陌生卡片刷卡</b>\n卡號：{rfid_uid}")
        return {"status": "alerted", "message": "Stranger alert sent"}

    if student_id:
        pending_user = db.query(User).filter(User.student_id == student_id).first()
        
        if pending_user and not pending_user.rfid_uid:
            # 檢查卡片是否被占用 (移到 step_1)
            if db.query(User).filter(User.rfid_uid == rfid_uid).first():
                return JSONResponse(status_code=400, content={"message": "此卡片已被他人使用"})

            last_log = db.query(AccessLog).filter(
                AccessLog.student_id == student_id,
                AccessLog.action == "SCAN_1",
                AccessLog.timestamp > datetime.utcnow() + timedelta(hours=8) - timedelta(minutes=2)
            ).order_by(desc(AccessLog.timestamp)).first()

            if not last_log:
                # [Step 1] (已查占用)
                log = AccessLog(student_id=student_id, rfid_uid=rfid_uid, action="SCAN_1")
                db.add(log); db.commit()
                return JSONResponse({"status": "step_1", "message": "請再次刷卡以確認綁定"})
            else:
                # [Step 2]
                if last_log.rfid_uid == rfid_uid:
                    pending_user.rfid_uid = rfid_uid
                    log_bind = AccessLog(student_id=student_id, rfid_uid=rfid_uid, action="BIND")
                    db.add(log_bind); db.commit()
                    send_tg_message(f"✅ <b>綁定成功！</b>\n用戶：{pending_user.name}\n卡號：{rfid_uid}")
                    return JSONResponse({"status": "bound", "message": "綁定成功"})
                else:
                    return JSONResponse(status_code=400, content={"message": "兩次卡片不一致"})

    return JSONResponse(status_code=400, content={"message": "Invalid request"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
