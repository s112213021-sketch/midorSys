from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, Column, String, TIMESTAMP, func, Integer, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import IntegrityError
import os
import requests  # 【新增 1】記得在 requirements.txt 加入 requests
from dotenv import load_dotenv 

# 載入 .env 檔案
load_dotenv()

# 修改這裡：優先讀取環境變數
DATABASE_URL = os.getenv("DATABASE_URL")
PI_API_URL = os.getenv("PI_API_URL")

# 【新增 2】讀取 Telegram 設定
TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

if not DATABASE_URL:
    raise ValueError("❌ 未設定 DATABASE_URL 環境變數")

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

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
    action = Column(String(10), nullable=False)
    timestamp = Column(TIMESTAMP(timezone=True), server_default=func.now())

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ================= 【新增 3】TG 發送小幫手 =================
def send_tg_message(text):
    """發送訊息到 Telegram"""
    if not TG_TOKEN or not TG_CHAT_ID:
        print("⚠️ TG 設定未完成，跳過發送")
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML" # 支援粗體語法
        }
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"TG 發送失敗: {e}")

# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def register_form(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "error": None, "pi_api_url": PI_API_URL})

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

    # 檢查是否已存在
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
             return JSONResponse({"status": "ready_to_scan", "student_id": student_id})

    try:
        user = User(student_id=student_id, name=name)
        db.add(user)
        db.commit()
        
        # --- TG 通知邏輯 ---
        msg = (
            f"📝 <b>新用戶註冊申請</b>\n"
            f"------------------\n"
            f"姓名：{name}\n"
            f"學號：{student_id}\n"
            f"狀態：等待刷卡綁定中..."
        )
        send_tg_message(msg)
        # ------------------

        return JSONResponse({"status": "ready_to_scan", "student_id": student_id})
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="註冊失敗")

@app.get("/check_status/{student_id}")
async def check_status(student_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.student_id == student_id).first()
    
    if user and user.rfid_uid:
        return {"status": "bound", "rfid_uid": user.rfid_uid}
    
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
        raise HTTPException(status_code=404, detail="用戶不存在")
    return templates.TemplateResponse("success.html", {"request": request, "user": user})

# 【重寫 /rfid_scan】整合進門通知與綁定通知
@app.post("/rfid_scan")
async def rfid_scan(
    # 這裡將 student_id 設為非必填(Optional)，因為一般進門時 Pi 可能只送 UID
    rfid_uid: str = Form(...),
    student_id: str = Form(None), 
    action: str = Form(default="entry"),
    db: Session = Depends(get_db),
):
    # 1. 先用 UID 找人 (進門邏輯)
    existing_user = db.query(User).filter(User.rfid_uid == rfid_uid).first()

    if existing_user:
        # --- 已綁定用戶：進門 ---
        log = AccessLog(student_id=existing_user.student_id, rfid_uid=rfid_uid, action=action)
        db.add(log)
        db.commit()

        # 發送 TG 進門通知
        msg = f"👋 <b>你好！{existing_user.name} 已進入 MOLI</b>"
        send_tg_message(msg)

        return JSONResponse({"status": "success", "message": f"歡迎 {existing_user.name}"})

    # 2. 如果 UID 找不到人，檢查是否為綁定流程 (需要 student_id)
    if student_id:
        pending_user = db.query(User).filter(User.student_id == student_id).first()
        
        # 如果用戶存在，且還沒綁定卡片
        if pending_user and not pending_user.rfid_uid:
            pending_user.rfid_uid = rfid_uid
            db.commit()

            # 寫入 Log (標記為綁定的第一刷或確認刷)
            log = AccessLog(student_id=student_id, rfid_uid=rfid_uid, action="bind")
            db.add(log)
            db.commit()

            # 發送 TG 綁定通知
            msg = (
                f"✅ <b>綁定成功！</b>\n"
                f"------------------\n"
                f"用戶：{pending_user.name}\n"
                f"學號：{pending_user.student_id}\n"
                f"卡號：{rfid_uid}"
            )
            send_tg_message(msg)

            return JSONResponse({"status": "success", "message": "綁定成功"})

    # 3. 既不是舊生，也不是綁定流程 -> 陌生卡
    send_tg_message(f"⚠️ <b>警告：陌生卡片刷卡</b>\n卡號：{rfid_uid}")
    raise HTTPException(status_code=400, detail="未知卡片或綁定失敗")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)