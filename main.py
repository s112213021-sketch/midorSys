from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, Column, String, TIMESTAMP, func, Integer, ForeignKey, and_
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import IntegrityError
import os
from dotenv import load_dotenv # 記得安裝 python-dotenv
import requests
import threading
from datetime import datetime, timedelta
from telegram_helper import send_telegram_async

# 載入 .env 檔案
load_dotenv()

# 讀取 PI API URL（若使用 cloudflared 暴露的 Pi）
PI_API_URL = os.getenv("PI_API_URL")
PI_API_KEY = os.getenv("PI_API_KEY")

print(f"[startup] PI_API_URL={PI_API_URL}")

# 修改這裡：優先讀取環境變數，沒有才用預設值 (但強烈建議不要在 code 留真實密碼)
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("❌ 未設定 DATABASE_URL 環境變數")

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- Model 定義保持不變 ---
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


class RegistrationSession(Base):
    __tablename__ = "registration_sessions"
    student_id = Column(String(20), ForeignKey("users.student_id"), primary_key=True)
    first_uid = Column(String(50), nullable=True)
    step = Column(Integer, default=0)
    expires_at = Column(TIMESTAMP(timezone=True), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class UserCard(Base):
    __tablename__ = "user_cards"
    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(String(20), ForeignKey("users.student_id"), nullable=False)
    rfid_uid = Column(String(50), unique=True, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Telegram helper provided by telegram_helper.send_telegram_async


def notify_pi_register_bg(student_id: str):
    """Background notify Pi (via PI_API_URL) to enter register mode."""
    if not PI_API_URL:
        print("[notify_pi] PI_API_URL not set, skipping notify")
        return
    try:
        url = f"{PI_API_URL.rstrip('/')}/mode/register"
        resp = requests.post(url, json={"student_id": student_id}, timeout=5)
        print(f"[notify_pi] POST {url} -> {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[notify_pi] failed: {e}")


# --- API for Pi (scan/register interactions) ---
@app.post("/api/scan")
async def api_scan(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    rfid_uid = data.get("rfid_uid")
    if not rfid_uid:
        return JSONResponse({"error": "missing rfid_uid"}, status_code=400)
    # 先在 user_cards 中找 UID，支援一個使用者多張卡
    card = db.query(UserCard).filter(UserCard.rfid_uid == rfid_uid).first()
    if card:
        user = db.query(User).filter(User.student_id == card.student_id).first()
        if user:
            log = AccessLog(student_id=user.student_id, rfid_uid=rfid_uid, action="entry")
            db.add(log)
            db.commit()
            try:
                send_telegram_async(f"你好！{user.name} 已進入 moli ({user.student_id})")
            except Exception:
                pass
            return {"status": "allow", "student_id": user.student_id, "name": user.name}

    # 未註冊
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

    expires = datetime.utcnow() + timedelta(seconds=60)
    session = db.query(RegistrationSession).filter(RegistrationSession.student_id == student_id).first()
    if session:
        session.first_uid = None
        session.step = 0
        session.expires_at = expires
    else:
        session = RegistrationSession(student_id=student_id, first_uid=None, step=0, expires_at=expires)
        db.add(session)
    db.commit()
    return {"status": "ok", "expires_at": expires.isoformat()}


@app.post("/api/register/scan")
async def api_register_scan(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    student_id = data.get("student_id")
    rfid_uid = data.get("rfid_uid")
    if not student_id or not rfid_uid:
        return JSONResponse({"error": "missing student_id_or_rfid_uid"}, status_code=400)

    session = db.query(RegistrationSession).filter(RegistrationSession.student_id == student_id).first()
    if not session:
        return JSONResponse({"error": "no_active_session"}, status_code=400)

    # 檢查逾時
    if session.expires_at and session.expires_at < datetime.utcnow():
        # 刪除 session
        db.delete(session)
        db.commit()
        return JSONResponse({"error": "session_expired"}, status_code=400)

    # step 0 => 接受第一刷
    if session.step == 0:
        # 檢查此 UID 是否已被其他人綁定
        other = db.query(UserCard).filter(and_(UserCard.rfid_uid == rfid_uid, UserCard.student_id != student_id)).first()
        if other:
            return JSONResponse({"error": "uid_already_bound", "bound_to": other.student_id}, status_code=400)

        session.first_uid = rfid_uid
        session.step = 1
        # 延長 session（一同作為容錯）
        session.expires_at = datetime.utcnow() + timedelta(seconds=60)
        db.commit()
        # 記錄一筆 SCAN_1 Log（可被 Pi 或前端 用來檢查）
        log = AccessLog(student_id=student_id, rfid_uid=rfid_uid, action="SCAN_1")
        db.add(log)
        db.commit()
        return {"status": "first_scan_ok"}

    # step 1 => 驗證第二刷
    if session.step == 1:
        if session.first_uid == rfid_uid:
            # 進行綁定（使用 user_cards 表以支援多張卡）
            user = db.query(User).filter(User.student_id == student_id).first()
            if not user:
                return JSONResponse({"error": "user_not_found"}, status_code=404)
            # 再次檢查是否有人綁定過
            other = db.query(UserCard).filter(and_(UserCard.rfid_uid == rfid_uid, UserCard.student_id != student_id)).first()
            if other:
                db.delete(session)
                db.commit()
                return JSONResponse({"error": "uid_already_bound_after_check", "bound_to": other.student_id}, status_code=400)

            # 新增 user_card 紀錄
            new_card = UserCard(student_id=student_id, rfid_uid=rfid_uid)
            db.add(new_card)
            db.add(AccessLog(student_id=student_id, rfid_uid=rfid_uid, action="bind"))
            # 刪除 session
            db.delete(session)
            db.commit()
            # Telegram 通知：綁定成功
            try:
                send_telegram_async(f"綁定成功：{user.name} ({user.student_id}) 綁定卡號 {rfid_uid}")
            except Exception:
                pass
            return {"status": "bound"}
        else:
            # 兩次不一致，回到 step 0
            session.first_uid = None
            session.step = 0
            session.expires_at = datetime.utcnow() + timedelta(seconds=60)
            db.commit()
            return JSONResponse({"error": "mismatch_first_second"}, status_code=400)

# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def register_form(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "error": None, "pi_api_url": PI_API_URL or '', "api_key": PI_API_KEY or ''})

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
        # 如果用戶存在且已經有卡號，提示直接進門
        # 檢查是否已有綁定卡片（user_cards）
        existing_card = db.query(UserCard).filter(UserCard.student_id == student_id).first()
        if existing_card:
             return templates.TemplateResponse(
                "register.html",
                {"request": request, "error": "❌ 學號已註冊且已綁定卡片，請直接使用。"},
            )
        else:
             # 用戶存在但沒卡號 (可能是上次註冊一半)，更新名字就好，繼續流程
             existing_user.name = name
             db.commit()
             # 這裡不跳轉，而是回傳 200 讓前端 JS 處理顯示 Modal
             try:
                 send_telegram_async(f"新用戶註冊（待綁定）：{name} ({student_id})")
             except Exception:
                 pass
             # 非同步通知 Pi 進入註冊模式（若 PI_API_URL 已設定）
             if PI_API_URL:
                 threading.Thread(target=notify_pi_register_bg, args=(student_id,), daemon=True).start()
             return JSONResponse({"status": "ready_to_scan", "student_id": student_id})

    try:
        user = User(student_id=student_id, name=name)
        db.add(user)
        db.commit()
        # 回傳 JSON 讓前端 JS 處理
        try:
            send_telegram_async(f"新用戶註冊（待綁定）：{name} ({student_id})")
        except Exception:
            pass
        # 非同步通知 Pi 進入註冊模式（若 PI_API_URL 已設定）
        if PI_API_URL:
            threading.Thread(target=notify_pi_register_bg, args=(student_id,), daemon=True).start()
        return JSONResponse({"status": "ready_to_scan", "student_id": student_id})
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="註冊失敗")

# 【新增】檢查綁定狀態 API (給前端輪詢用)
@app.get("/check_status/{student_id}")
async def check_status(student_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.student_id == student_id).first()
    if not user:
        return {"bound": False}
    cards = db.query(UserCard).filter(UserCard.student_id == student_id).all()
    rfid_list = [c.rfid_uid for c in cards]
    session = db.query(RegistrationSession).filter(RegistrationSession.student_id == student_id).first()
    session_info = None
    if session:
        session_info = {"step": session.step, "expires_at": session.expires_at.isoformat() if session.expires_at else None}
    return {"bound": len(rfid_list) > 0, "rfid_uids": rfid_list, "rfid_uid": rfid_list[0] if rfid_list else None, "registration_session": session_info}

@app.get("/success", response_class=HTMLResponse)
async def success_page(request: Request, student_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.student_id == student_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用戶不存在")
    return templates.TemplateResponse("success.html", {"request": request, "user": user})

# 此 API 保留，給本地端 Python 腳本或 Postman 測試用
@app.post("/rfid_scan")
async def rfid_scan(
    student_id: str = Form(...),
    rfid_uid: str = Form(...),
    action: str = Form(default="entry"),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.student_id == student_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用戶不存在")

    # 如果 action == 'bind'，執行綁定（支援多卡）
    if action == 'bind':
        existing = db.query(UserCard).filter(UserCard.rfid_uid == rfid_uid).first()
        if existing:
            raise HTTPException(status_code=400, detail="RFID 已被其他使用者綁定")
        new_card = UserCard(student_id=student_id, rfid_uid=rfid_uid)
        db.add(new_card)
        db.add(AccessLog(student_id=student_id, rfid_uid=rfid_uid, action="bind"))
        db.commit()
        return JSONResponse({"status": "success", "message": "綁定成功"})

    # 其他 action（例如 entry）: 檢查卡號是否屬於該使用者
    card = db.query(UserCard).filter(UserCard.rfid_uid == rfid_uid).first()
    if not card or card.student_id != student_id:
        raise HTTPException(status_code=400, detail="RFID 與註冊資料不符")

    log = AccessLog(student_id=student_id, rfid_uid=rfid_uid, action=action)
    db.add(log)
    db.commit()

    return JSONResponse({"status": "success", "message": f"{action} 成功"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)