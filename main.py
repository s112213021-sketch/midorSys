from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, Column, String, TIMESTAMP, func, Integer, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import IntegrityError
import os
from dotenv import load_dotenv # 記得安裝 python-dotenv

# 載入 .env 檔案
load_dotenv()

# 修改這裡：優先讀取環境變數，沒有才用預設值 (但強烈建議不要在 code 留真實密碼)
DATABASE_URL = os.getenv("DATABASE_URL")
PI_API_URL = os.getenv("PI_API_URL")

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

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def register_form(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "error": None, "pi_api_url": PI_API_URL})

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
        if existing_user.rfid_uid:
             return templates.TemplateResponse(
                "register.html",
                {"request": request, "error": "❌ 學號已註冊且已綁定卡片，請直接使用。"},
            )
        else:
             # 用戶存在但沒卡號 (可能是上次註冊一半)，更新名字就好，繼續流程
             existing_user.name = name
             db.commit()
             # 這裡不跳轉，而是回傳 200 讓前端 JS 處理顯示 Modal
             return JSONResponse({"status": "ready_to_scan", "student_id": student_id})

    try:
        user = User(student_id=student_id, name=name)
        db.add(user)
        db.commit()
        # 回傳 JSON 讓前端 JS 處理
        return JSONResponse({"status": "ready_to_scan", "student_id": student_id})
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="註冊失敗")

# 【新增】檢查綁定狀態 API (給前端輪詢用)
@app.get("/check_status/{student_id}")
async def check_status(student_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.student_id == student_id).first()
    if user and user.rfid_uid:
        return {"bound": True, "rfid_uid": user.rfid_uid}
    return {"bound": False}

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

    # 如果是用戶還沒綁定，這一次刷卡就是綁定
    if not user.rfid_uid:
        user.rfid_uid = rfid_uid
        db.commit()
        # 寫入 Log
        log = AccessLog(student_id=student_id, rfid_uid=rfid_uid, action="bind")
        db.add(log)
        db.commit()
        return JSONResponse({"status": "success", "message": "綁定成功"})
    
    # 如果已綁定，檢查卡號是否相符
    if user.rfid_uid != rfid_uid:
         raise HTTPException(status_code=400, detail="RFID 與註冊資料不符")

    log = AccessLog(student_id=student_id, rfid_uid=rfid_uid, action=action)
    db.add(log)
    db.commit()

    return JSONResponse({"status": "success", "message": f"{action} 成功"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)