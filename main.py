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

# ================= 📊 統計與報告邏輯 =================

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
    # (省略重複邏輯，與之前相同)
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

# --- Routes (註冊與查詢保持不變) ---

@app.get("/", response_class=HTMLResponse)
async def register_form(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "pi_api_url": PI_API_URL})

# 【修改 /register】加入註冊通知
@app.post("/register")
async def register_post(
    request: Request,
    student_id: str = Form(...),
    name: str = Form(...),
    db: Session = Depends(get_db),
):
    student_id = student_id.strip()
    name = name.strip()

    # 1. 先檢查資料庫狀態
    existing_user = db.query(User).filter(User.student_id == student_id).first()

    try:
        # --- 資料庫操作區 ---
        if existing_user:
            # 情況 A: 用戶存在
            if existing_user.rfid_uid:
                # 已經綁定過卡片 -> 禁止重複註冊
                return JSONResponse(status_code=400, content={"message": "❌ 此學號已綁定卡片，請直接刷卡進門。"})
            else:
                # 有學號但沒卡片 (上次註冊一半) -> 更新名字，準備繼續綁定
                existing_user.name = name
                db.commit()
        else:
            # 情況 B: 完全的新用戶 -> 建立資料
            new_user = User(student_id=student_id, name=name)
            db.add(new_user)
            db.commit()
        
        # --- 樹莓派連動區 (成功寫入 DB 後才執行) ---
        # 不管是情況 A 或 B，只要沒報錯，都要叫樹莓派準備掃描
        if PI_API_URL:
            try:
                # 呼叫樹莓派的 Cloudflare 網址
                # 注意：這裡 timeout 設短一點，不要讓網頁等太久
                requests.post(
                    f"{PI_API_URL}/mode/register",
                    json={"student_id": student_id},
                    timeout=3 
                )
                print(f"✅ 已通知 Pi 切換模式: {student_id}")
            except Exception as e:
                print(f"⚠️ 無法連線到 Pi (可能網路不穩): {e}")
                # Pi 連線失敗不影響註冊流程，讓前端繼續跑倒數
        
        # --- Telegram 通知區 ---
        msg = (
            f"📝 <b>新用戶註冊申請</b>\n"
            f"------------------\n"
            f"姓名：{name}\n"
            f"學號：{student_id}\n"
            f"狀態：等待刷卡驗證 (60s)..."
        )
        send_tg_message(msg)

        # --- 回傳給前端 ---
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

# ================= 核心 API 修改重點 =================

@app.post("/rfid_scan")
async def rfid_scan(
    rfid_uid: str = Form(...),
    student_id: str = Form(None), 
    action: str = Form(default="ENTRY"), # 接收讀卡機傳來的動作 (ENTRY / ERROR / BIND)
    db: Session = Depends(get_db),
):
    # --- 狀況 1: 讀卡機已經開門 (Normal Mode) ---
    # 讀卡機傳來 action="ENTRY"，代表它是舊生且 Pi 已經驗證過了
    if action == "ENTRY":
        user = db.query(User).filter(User.rfid_uid == rfid_uid).first()
        if user:
            # 補寫 Log
            log = AccessLog(student_id=user.student_id, rfid_uid=rfid_uid, action="ENTRY")
            db.add(log); db.commit()
            
            # 發送 TG 通知
            send_tg_message(f"👋 <b>你好！{user.name} 已進入 MOLI</b>")
            
            # 觸發人流偵測
            check_crowd_alert(db)
            return {"status": "logged", "message": "Entry logged"}
        else:
            # 理論上 Pi 查得到 user 才會送 ENTRY，若這邊查不到代表 DB 不同步
            return {"status": "error", "message": "User not found in cloud DB"}

    # --- 狀況 2: 讀卡機拒絕進入 (Normal Mode) ---
    if action == "ERROR":
        send_tg_message(f"⚠️ <b>警告：陌生卡片刷卡</b>\n卡號：{rfid_uid}")
        return {"status": "alerted", "message": "Stranger alert sent"}

    # --- 狀況 3: 註冊綁定模式 (Register Mode) ---
    # 讀卡機傳來 student_id，代表正在進行綁定
    if student_id:
        pending_user = db.query(User).filter(User.student_id == student_id).first()
        
        if pending_user and not pending_user.rfid_uid:
            # 檢查卡片是否被占用
            if db.query(User).filter(User.rfid_uid == rfid_uid).first():
                 return JSONResponse(status_code=400, content={"message": "此卡片已被他人使用"})

            # 檢查是否為第二刷
            last_log = db.query(AccessLog).filter(
                AccessLog.student_id == student_id,
                AccessLog.action == "SCAN_1",
                AccessLog.timestamp > datetime.now() - timedelta(minutes=2)
            ).order_by(desc(AccessLog.timestamp)).first()

            if not last_log:
                # [Step 1]
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
