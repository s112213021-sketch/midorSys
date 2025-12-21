from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy import (
    create_engine,
    Column,
    String,
    TIMESTAMP,
    func,
    Integer,
    ForeignKey,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import IntegrityError
from dotenv import load_dotenv
from pathlib import Path
import os
import sqlite3
import requests
import threading
from datetime import datetime, timedelta

# ----------------- 基本設定 -----------------

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "moli_door.db"          # 真正使用的 SQLite 檔案
INIT_SQL_PATH = BASE_DIR / "sql" / "init_db.sql"

PI_API_URL = os.getenv("PI_API_URL")
PI_API_KEY = os.getenv("PI_API_KEY")

print(f"[startup] PI_API_URL={PI_API_URL}")
print(f"[startup] DB_PATH={DB_PATH}")

# ----------------- 用 init_db.sql 建立 / 更新 SQLite -----------------


def init_sqlite_db():
    # 若 DB 檔不存在，或你想確保 schema 一致，就執行 init_db.sql
    conn = sqlite3.connect(DB_PATH)
    try:
        with open(INIT_SQL_PATH, "r", encoding="utf-8") as f:
            sql_script = f.read()
        conn.executescript(sql_script)
        conn.commit()
        print("[db] init_db.sql executed OK")
    finally:
        conn.close()


init_sqlite_db()

DATABASE_URL = f"sqlite:///{DB_PATH}"

# ----------------- SQLAlchemy 設定 -----------------

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # SQLite 多執行緒需要
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ----------------- ORM Models：對應 init_db.sql -----------------


class User(Base):
    __tablename__ = "users"
    student_id = Column(String(20), primary_key=True, index=True)
    name = Column(String(50), nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())


class UserCard(Base):
    __tablename__ = "user_cards"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    student_id = Column(String(20), ForeignKey("users.student_id"), nullable=False)
    rfid_uid = Column(String(50), unique=True, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())


class AccessLog(Base):
    __tablename__ = "access_logs"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    student_id = Column(String(20), ForeignKey("users.student_id"), nullable=False)
    rfid_uid = Column(String(50), nullable=False)
    action = Column(String(10), nullable=False)
    timestamp = Column(TIMESTAMP, server_default=func.now())


class RegistrationSession(Base):
    __tablename__ = "registration_sessions"
    student_id = Column(
        String(20), ForeignKey("users.student_id"), primary_key=True
    )
    first_uid = Column(String(50), nullable=True)
    step = Column(Integer, default=0)
    expires_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())


# 注意：schema 已由 init_db.sql 建立，這裡不再 create_all
# Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ----------------- Telegram helper -----------------

TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")


def send_telegram(text: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("[tg] TG_TOKEN or TG_CHAT_ID not set, skipping message")
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        resp = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text})
        if resp.status_code != 200:
            print(f"[tg] send failed: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[tg] exception: {e}")


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


# ----------------- FastAPI 基本設定 -----------------

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ----------------- API for Pi -----------------


@app.post("/api/scan")
async def api_scan(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    rfid_uid = data.get("rfid_uid")
    if not rfid_uid:
        return JSONResponse({"error": "missing rfid_uid"}, status_code=400)

    # 在 user_cards 找卡 → join user
    card = db.query(UserCard).filter(UserCard.rfid_uid == rfid_uid).first()
    if card:
        user = db.query(User).filter(User.student_id == card.student_id).first()
        log = AccessLog(
            student_id=card.student_id, rfid_uid=rfid_uid, action="entry"
        )
        db.add(log)
        db.commit()
        try:
            send_telegram(f"你好！{user.name} 已進入 moli ({user.student_id})")
        except Exception:
            pass
        return {
            "status": "allow",
            "student_id": user.student_id,
            "name": user.name,
        }

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
    session = (
        db.query(RegistrationSession)
        .filter(RegistrationSession.student_id == student_id)
        .first()
    )
    if session:
        session.first_uid = None
        session.step = 0
        session.expires_at = expires
    else:
        session = RegistrationSession(
            student_id=student_id, first_uid=None, step=0, expires_at=expires
        )
        db.add(session)
    db.commit()
    return {"status": "ok", "expires_at": expires.isoformat()}


@app.post("/api/register/scan")
async def api_register_scan(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    student_id = data.get("student_id")
    rfid_uid = data.get("rfid_uid")
    if not student_id or not rfid_uid:
        return JSONResponse(
            {"error": "missing student_id_or_rfid_uid"}, status_code=400
        )

    session = (
        db.query(RegistrationSession)
        .filter(RegistrationSession.student_id == student_id)
        .first()
    )
    if not session:
        return JSONResponse({"error": "no_active_session"}, status_code=400)

    # 檢查逾時
    if session.expires_at and session.expires_at < datetime.utcnow():
        db.delete(session)
        db.commit()
        return JSONResponse({"error": "session_expired"}, status_code=400)

    # step 0 => 接受第一刷
    if session.step == 0:
        # 檢查此 UID 是否已被其他人綁定
        other = db.query(UserCard).filter(UserCard.rfid_uid == rfid_uid).first()
        if other:
            return JSONResponse(
                {"error": "uid_already_bound", "bound_to": other.student_id},
                status_code=400,
            )

        session.first_uid = rfid_uid
        session.step = 1
        session.expires_at = datetime.utcnow() + timedelta(seconds=60)
        db.commit()
        log = AccessLog(
            student_id=student_id, rfid_uid=rfid_uid, action="SCAN_1"
        )
        db.add(log)
        db.commit()
        return {"status": "first_scan_ok"}

    # step 1 => 驗證第二刷
    if session.step == 1:
        if session.first_uid == rfid_uid:
            user = (
                db.query(User).filter(User.student_id == student_id).first()
            )
            if not user:
                return JSONResponse(
                    {"error": "user_not_found"}, status_code=404
                )

            other = db.query(UserCard).filter(
                UserCard.rfid_uid == rfid_uid,
                UserCard.student_id != student_id,
            ).first()
            if other:
                db.delete(session)
                db.commit()
                return JSONResponse(
                    {
                        "error": "uid_already_bound_after_check",
                        "bound_to": other.student_id,
                    },
                    status_code=400,
                )

            # 寫入 user_cards 一筆綁定
            new_card = UserCard(
                student_id=student_id, rfid_uid=rfid_uid
            )
            db.add(new_card)
            db.add(
                AccessLog(
                    student_id=student_id,
                    rfid_uid=rfid_uid,
                    action="bind",
                )
            )
            db.delete(session)
            db.commit()
            try:
                send_telegram(
                    f"綁定成功：{user.name} ({user.student_id}) 綁定卡號 {rfid_uid}"
                )
            except Exception:
                pass
            return {"status": "bound"}
        else:
            session.first_uid = None
            session.step = 0
            session.expires_at = datetime.utcnow() + timedelta(seconds=60)
            db.commit()
            return JSONResponse(
                {"error": "mismatch_first_second"}, status_code=400
            )


# ----------------- Web Routes -----------------


@app.get("/", response_class=HTMLResponse)
async def register_form(request: Request):
    return templates.TemplateResponse(
        "register.html",
        {
            "request": request,
            "error": None,
            "pi_api_url": PI_API_URL or "",
            "api_key": PI_API_KEY or "",
        },
    )


@app.post("/register")
async def register_post(
    request: Request,
    student_id: str = Form(...),
    name: str = Form(...),
    db: Session = Depends(get_db),
):
    student_id = student_id.strip()
    name = name.strip()

    existing_user = (
        db.query(User).filter(User.student_id == student_id).first()
    )
    if existing_user:
        # 判斷是否已經有任何卡片
        has_card = (
            db.query(UserCard)
            .filter(UserCard.student_id == student_id)
            .first()
            is not None
        )
        if has_card:
            return templates.TemplateResponse(
                "register.html",
                {
                    "request": request,
                    "error": "❌ 學號已註冊且已綁定卡片，請直接使用。",
                },
            )
        else:
            existing_user.name = name
            db.commit()
            try:
                send_telegram(
                    f"新用戶註冊（待綁定）：{name} ({student_id})"
                )
            except Exception:
                pass
            if PI_API_URL:
                threading.Thread(
                    target=notify_pi_register_bg,
                    args=(student_id,),
                    daemon=True,
                ).start()
            return JSONResponse(
                {"status": "ready_to_scan", "student_id": student_id}
            )

    try:
        user = User(student_id=student_id, name=name)
        db.add(user)
        db.commit()
        try:
            send_telegram(
                f"新用戶註冊（待綁定）：{name} ({student_id})"
            )
        except Exception:
            pass
        if PI_API_URL:
            threading.Thread(
                target=notify_pi_register_bg,
                args=(student_id,),
                daemon=True,
            ).start()
        return JSONResponse(
            {"status": "ready_to_scan", "student_id": student_id}
        )
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="註冊失敗")


@app.get("/check_status/{student_id}")
async def check_status(student_id: str, db: Session = Depends(get_db)):
    has_card = (
        db.query(UserCard)
        .filter(UserCard.student_id == student_id)
        .first()
        is not None
    )
    if has_card:
        card = (
            db.query(UserCard)
            .filter(UserCard.student_id == student_id)
            .first()
        )
        return {"bound": True, "rfid_uid": card.rfid_uid}
    return {"bound": False}


@app.get("/success", response_class=HTMLResponse)
async def success_page(
    request: Request, student_id: str, db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.student_id == student_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用戶不存在")
    return templates.TemplateResponse(
        "success.html", {"request": request, "user": user}
    )


# 測試 /rfid_scan：直接綁定或進出記錄
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

    card = (
        db.query(UserCard)
        .filter(UserCard.rfid_uid == rfid_uid)
        .first()
    )

    if not card:
        # 尚未綁定 → 這次視為綁定
        new_card = UserCard(student_id=student_id, rfid_uid=rfid_uid)
        db.add(new_card)
        db.commit()
        db.add(
            AccessLog(
                student_id=student_id, rfid_uid=rfid_uid, action="bind"
            )
        )
        db.commit()
        return JSONResponse(
            {"status": "success", "message": "綁定成功"}
        )

    if card.student_id != student_id:
        raise HTTPException(
            status_code=400, detail="RFID 與註冊資料不符"
        )

    log = AccessLog(
        student_id=student_id, rfid_uid=rfid_uid, action=action
    )
    db.add(log)
    db.commit()
    return JSONResponse(
        {"status": "success", "message": f"{action} 成功"}
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
