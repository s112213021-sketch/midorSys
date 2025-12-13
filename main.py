from fastapi import FastAPI, Request, Form, HTTPException, Depends, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, Column, String, TIMESTAMP, func, Integer, ForeignKey, desc
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import IntegrityError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta
import os
import httpx
import asyncio
from dotenv import load_dotenv

load_dotenv()

# --- 設定變數 ---
DATABASE_URL = os.getenv("DATABASE_URL")
PI_API_URL = os.getenv("PI_API_URL")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not DATABASE_URL:
    raise ValueError("❌ 未設定 DATABASE_URL 環境變數")

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- Scheduler (排程器) ---
scheduler = AsyncIOScheduler()

# --- Telegram Helper ---
async def send_telegram_message(message: str):
    """發送 Telegram 訊息的 Helper Function"""
    if not TG_TOKEN or not TG_CHAT_ID:
        print(f"⚠️ Telegram 未設定，略過發送: {message}")
        return
    
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload)
    except Exception as e:
        print(f"❌ Telegram 發送失敗: {e}")

# --- Models ---
class User(Base):
    __tablename__ = "users"
    student_id = Column(String(20), primary_key=True, index=True)
    name = Column(String(50), nullable=False)
    rfid_uid = Column(String(50), unique=True, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

class AccessLog(Base):
    __tablename__ = "access_logs"
    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(String(20), ForeignKey("users.student_id"), nullable=True) # 允許空值以記錄陌生卡片
    rfid_uid = Column(String(50), nullable=False)
    action = Column(String(20), nullable=False) # 擴充長度以容納不同狀態
    timestamp = Column(TIMESTAMP(timezone=True), server_default=func.now())

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- 背景任務邏輯 ---

async def monitor_registration_timeout(student_id: str):
    """註冊 60 秒超時監控"""
    await asyncio.sleep(60) # 等待 60 秒
    
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.student_id == student_id).first()
        # 如果 60 秒後，該用戶還沒有 RFID UID，視為超時
        if user and not user.rfid_uid:
            db.delete(user) # 刪除資料
            db.commit()
            print(f"⏳ 用戶 {student_id} 註冊逾時，已刪除。")
            await send_telegram_message(f"⏳ <b>註冊逾時</b>\n學號：{student_id}\n狀態：系統已自動取消申請。")
    except Exception as e:
        print(f"Monitor error: {e}")
    finally:
        db.close()

# --- 定時任務邏輯 (Scheduler Tasks) ---

async def hourly_ventilation_check():
    """每小時檢查：若過去一小時進出 >= 10 人次，發送換氣提醒"""
    db = SessionLocal()
    try:
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)
        # 統計過去一小時的 entry 次數
        count = db.query(AccessLog).filter(
            AccessLog.timestamp >= one_hour_ago,
            AccessLog.action == "ENTRY"
        ).count()

        if count >= 10:
            await send_telegram_message(f"🌬️ <b>換氣提醒</b>\n過去一小時進場人次：{count} 人\n室內人數眾多，建議開啟窗戶保持通風！")
    finally:
        db.close()

async def daily_report():
    """每日 18:00 報告"""
    db = SessionLocal()
    try:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        
        # 1. 找出今日最常來的人
        # SQL: SELECT student_id, COUNT(*) FROM logs WHERE ... GROUP BY student_id ORDER BY COUNT DESC
        most_frequent = db.query(
            AccessLog.student_id, func.count(AccessLog.student_id).label('total')
        ).filter(
            AccessLog.timestamp >= today_start,
            AccessLog.action == "ENTRY",
            AccessLog.student_id != None
        ).group_by(AccessLog.student_id).order_by(desc('total')).first()

        # 2. 找出最晚離開的人 (這裡假設最後一筆 Log 是最晚，實際應判斷 EXIT，這裡簡化為最後一筆活動)
        last_person = db.query(AccessLog).filter(
            AccessLog.timestamp >= today_start,
            AccessLog.student_id != None
        ).order_by(AccessLog.timestamp.desc()).first()

        report_msg = f"📊 <b>每日門禁報告 ({datetime.now().strftime('%Y-%m-%d')})</b>\n"
        
        if most_frequent:
            report_msg += f"🏆 今日最活躍：{most_frequent.student_id} ({most_frequent.total} 次)\n"
        else:
            report_msg += "🏆 今日最活躍：無資料\n"
            
        if last_person:
            # 轉換時區顯示
            local_time = last_person.timestamp + timedelta(hours=8) # 假設台灣時間
            report_msg += f"🌙 最後活動：{last_person.student_id} ({local_time.strftime('%H:%M')})"
        else:
            report_msg += "🌙 最後活動：無資料"

        await send_telegram_message(report_msg)
    finally:
        db.close()

# --- Routes ---

@app.on_event("startup")
async def startup_event():
    # 啟動排程
    scheduler.add_job(hourly_ventilation_check, 'interval', hours=1)
    scheduler.add_job(daily_report, 'cron', hour=18, minute=0) # 每天 18:00
    scheduler.start()
    print("✅ Scheduler started.")

@app.get("/", response_class=HTMLResponse)
async def register_form(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "error": None, "pi_api_url": PI_API_URL})

@app.post("/register")
async def register_post(
    request: Request,
    background_tasks: BackgroundTasks,
    student_id: str = Form(...),
    name: str = Form(...),
    db: Session = Depends(get_db),
):
    """使用者掃 QRCode -> 填寫資料 -> 觸發此 API"""
    student_id = student_id.strip()
    name = name.strip()

    existing_user = db.query(User).filter(User.student_id == student_id).first()
    if existing_user:
        if existing_user.rfid_uid:
             return templates.TemplateResponse(
                "register.html",
                {"request": request, "error": "❌ 學號已註冊且已綁定卡片，請直接使用。"},
            )
        else:
             existing_user.name = name
             db.commit()
             # 重新啟動超時監控
             background_tasks.add_task(monitor_registration_timeout, student_id)
             return JSONResponse({"status": "ready_to_scan", "student_id": student_id})

    try:
        user = User(student_id=student_id, name=name)
        db.add(user)
        db.commit()
        
        # 1. 發送 TG 通知：新用戶申請
        background_tasks.add_task(send_telegram_message, f"📝 <b>新用戶申請</b>\n姓名：{name}\n學號：{student_id}\n狀態：等待靠卡綁定...")
        
        # 2. 啟動 60 秒逾時監控
        background_tasks.add_task(monitor_registration_timeout, student_id)

        return JSONResponse({"status": "ready_to_scan", "student_id": student_id})
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="註冊失敗")

@app.get("/check_status/{student_id}")
async def check_status(student_id: str, db: Session = Depends(get_db)):
    """前端輪詢狀態"""
    user = db.query(User).filter(User.student_id == student_id).first()
    
    if not user:
        return {"status": "cancelled"} # 可能因為超時被刪除了

    if user.rfid_uid:
        return {"status": "bound", "rfid_uid": user.rfid_uid}
    
    # 檢查是否有 SCAN_1 紀錄 (60秒內的)
    recent_log = db.query(AccessLog).filter(
        AccessLog.student_id == student_id, 
        AccessLog.action == "SCAN_1"
    ).order_by(AccessLog.timestamp.desc()).first()
    
    if recent_log:
        return {"status": "step_1"} 

    return {"status": "waiting"}

@app.get("/success", response_class=HTMLResponse)
async def success_page(request: Request, student_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.student_id == student_id).first()
    if not user:
        # 如果用戶在成功頁面刷新時已經被刪除（雖然不常見），導回首頁
        return RedirectResponse(url="/")
    return templates.TemplateResponse("success.html", {"request": request, "user": user})

@app.post("/rfid_scan")
async def rfid_scan(
    background_tasks: BackgroundTasks,
    rfid_uid: str = Form(...),
    student_id: str = Form(None), # 設為 Optional，因為一般刷卡可能不帶 student_id (視你前端/Pi實作而定)
    action: str = Form(default="entry"),
    db: Session = Depends(get_db),
):
    """
    核心邏輯：
    1. 判斷是否為「陌生卡片」
    2. 判斷是否為「註冊流程中」的卡片 (雙重驗證)
    3. 判斷是否為「正常進出」
    """
    try:
        # --- 情境 A：一般進出檢查 (如果沒傳 student_id，或 student_id 是空的) ---
        # Pi 在一般模式下可能只讀到 UID，不知道 student_id
        if not student_id:
            # 反查 User
            user = db.query(User).filter(User.rfid_uid == rfid_uid).first()
            if user:
                # 正常進門
                log = AccessLog(student_id=user.student_id, rfid_uid=rfid_uid, action="ENTRY")
                db.add(log)
                db.commit()
                
                # 發送歡迎訊息 + 換氣提醒(若需要)
                msg = f"🟢 <b>歡迎進場</b>\n姓名：{user.name} ({user.student_id})"
                background_tasks.add_task(send_telegram_message, msg)
                
                return JSONResponse({"status": "success", "message": "Access Granted"})
            else:
                # 陌生卡片
                log = AccessLog(student_id=None, rfid_uid=rfid_uid, action="UNKNOWN")
                db.add(log)
                db.commit()
                
                # 發送警告
                background_tasks.add_task(send_telegram_message, f"⚠️ <b>陌生卡片警告</b>\nUID：{rfid_uid}\n有人試圖使用未註冊卡片刷卡！")
                raise HTTPException(status_code=403, detail="陌生卡片")

        # --- 情境 B：註冊/綁定流程 (前端有傳 student_id) ---
        user = db.query(User).filter(User.student_id == student_id).first()
        
        if not user:
             # 學號不存在
             background_tasks.add_task(send_telegram_message, f"❌ <b>錯誤警報</b>\n收到不存在的學號請求：{student_id}")
             raise HTTPException(status_code=404, detail="用戶不存在")

        # 1. 用戶已綁定完成 -> 視為一般刷卡 (防止重複綁定流程)
        if user.rfid_uid:
            if user.rfid_uid == rfid_uid:
                log = AccessLog(student_id=student_id, rfid_uid=rfid_uid, action="ENTRY")
                db.add(log)
                db.commit()
                background_tasks.add_task(send_telegram_message, f"🟢 <b>歡迎進場</b>\n姓名：{user.name}")
                return JSONResponse({"status": "success", "message": "已綁定，直接開門"})
            else:
                background_tasks.add_task(send_telegram_message, f"⚠️ <b>卡片不符</b>\n學號：{student_id}\n刷了非綁定的卡！")
                raise HTTPException(status_code=400, detail="卡片與身份不符")

        # 2. 用戶未綁定 -> 執行雙重刷卡邏輯
        
        # 檢查是否有 "SCAN_1" 紀錄
        recent_scan = db.query(AccessLog).filter(
            AccessLog.student_id == student_id,
            AccessLog.action == "SCAN_1"
        ).order_by(AccessLog.timestamp.desc()).first()

        # [第一次刷卡] 或 [上次 SCAN_1 太久以前(超過60秒視為無效)]
        # 這裡簡單判斷是否有記錄，嚴謹一點可以加時間判斷
        is_first_scan = True
        if recent_scan:
            time_diff = datetime.utcnow() - recent_scan.timestamp
            if time_diff.total_seconds() < 60:
                is_first_scan = False

        if is_first_scan:
            # 記錄第一次刷卡
            log = AccessLog(student_id=student_id, rfid_uid=rfid_uid, action="SCAN_1")
            db.add(log)
            db.commit()
            return JSONResponse({"status": "step_1", "message": "請再次刷卡確認"})
        
        else:
            # [第二次刷卡] 比對 UID
            if recent_scan.rfid_uid == rfid_uid:
                # 一致 -> 綁定成功
                user.rfid_uid = rfid_uid
                
                # 記錄綁定成功
                log = AccessLog(student_id=student_id, rfid_uid=rfid_uid, action="BIND_OK")
                db.add(log)
                db.commit()
                
                # 發送 TG 通知：綁定成功
                background_tasks.add_task(send_telegram_message, f"✅ <b>綁定成功</b>\n姓名：{user.name}\n卡號：{rfid_uid}\n歡迎加入！")
                
                return JSONResponse({"status": "success", "message": "綁定完成"})
            else:
                # 不一致 -> 清除暫存 (透過刪除 SCAN_1 log 或單純報錯讓使用者重來)
                # 這裡選擇記錄一個 error log，並不真正刪除 SCAN_1，但前端會收到 error
                log = AccessLog(student_id=student_id, rfid_uid=rfid_uid, action="BIND_FAIL")
                db.add(log)
                db.commit()
                
                return JSONResponse({"status": "error", "message": "兩次卡片不一致，請重新開始"}, status_code=400)

    except Exception as e:
        # 全局錯誤捕獲與通知
        print(f"Server Error: {e}")
        background_tasks.add_task(send_telegram_message, f"🔥 <b>系統錯誤</b>\n路徑：/rfid_scan\n錯誤：{str(e)}")
        raise e

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
