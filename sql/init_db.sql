-- MOLi 實驗室門禁系統 SQLite 建表腳本
-- 執行方式：sqlite3 data/moli_door.db < sql/init_db.sql
-- 1. 使用者基本資料表（學生）
CREATE TABLE IF NOT EXISTS users (
    student_id TEXT(20) PRIMARY KEY,
    name TEXT(50) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 為 student_id 額外建立索引（PRIMARY KEY 已自動有索引，此處明確寫出便於閱讀）
CREATE INDEX IF NOT EXISTS idx_users_student_id ON users(student_id);

-- 2. 使用者卡片綁定表（支援一人多卡、換卡、補卡）
CREATE TABLE IF NOT EXISTS user_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT(20) NOT NULL,
    rfid_uid TEXT(50) NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (student_id) REFERENCES users(student_id) ON DELETE CASCADE
);

-- 索引優化：快速根據卡號或學號查詢
CREATE INDEX IF NOT EXISTS idx_user_cards_rfid_uid ON user_cards(rfid_uid);
CREATE INDEX IF NOT EXISTS idx_user_cards_student_id ON user_cards(student_id);

-- 3. 門禁刷卡記錄表
CREATE TABLE IF NOT EXISTS access_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT(20) NOT NULL,
    rfid_uid TEXT(50) NOT NULL,
    action TEXT(10) NOT NULL, -- entry / exit / deny / bind 等
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (student_id) REFERENCES users(student_id) ON DELETE CASCADE
);

-- 索引優化：常按時間、學生或卡號查詢記錄
CREATE INDEX IF NOT EXISTS idx_access_logs_timestamp ON access_logs(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_access_logs_student_id ON access_logs(student_id);
CREATE INDEX IF NOT EXISTS idx_access_logs_rfid_uid ON access_logs(rfid_uid);

-- 4. 註冊暫存 session 表（處理「刷兩次卡」確認綁定邏輯）
CREATE TABLE IF NOT EXISTS registration_sessions (
    student_id TEXT(20) PRIMARY KEY,
    first_uid TEXT(50),
    step INTEGER DEFAULT 0, -- 0=等待第一刷, 1=等待第二刷
    expires_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (student_id) REFERENCES users(student_id) ON DELETE CASCADE
);
(base) beta@betaMacBook-Pro mm %   timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (student_id) REFERENCES users(student_id) ON DELETE CASCADE
);

-- 索引優化：常按時間、學生或卡號查詢記錄
CREATE INDEX IF NOT EXISTS idx_access_logs_timestamp ON access_logs(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_access_logs_student_id ON access_logs(student_id);
CREATE INDEX IF NOT EXISTS idx_access_logs_rfid_uid ON access_logs(rfid_uid);

-- 4. 註冊暫存 session 表（處理「刷兩次卡」確認綁定邏輯）
CREATE TABLE IF NOT EXISTS registration_sessions (
    student_id TEXT(20) PRIMARY KEY,
    first_uid TEXT(50),
    step INTEGER DEFAULT 0, -- 0=等待第一刷, 1=等待第二刷
    expires_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (student_id) REFERENCES users(student_id) ON DELETE CASCADE
);

(base) beta@betaMacBook-Pro mm % cat init_db.sql
-- 1. 使用者基本資料表（學生）
CREATE TABLE IF NOT EXISTS users (
    student_id TEXT(20) PRIMARY KEY,
    name TEXT(50) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 為 student_id 額外建立索引（PRIMARY KEY 已自動有索引，此處明確寫出便於閱讀）
CREATE INDEX IF NOT EXISTS idx_users_student_id ON users(student_id);

-- 2. 使用者卡片綁定表（支援一人多卡、換卡、補卡）
CREATE TABLE IF NOT EXISTS user_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT(20) NOT NULL,
    rfid_uid TEXT(50) NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (student_id) REFERENCES users(student_id) ON DELETE CASCADE
);

-- 索引優化：快速根據卡號或學號查詢
CREATE INDEX IF NOT EXISTS idx_user_cards_rfid_uid ON user_cards(rfid_uid);
CREATE INDEX IF NOT EXISTS idx_user_cards_student_id ON user_cards(student_id);

-- 3. 門禁刷卡記錄表
CREATE TABLE IF NOT EXISTS access_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT(20) NOT NULL,
    rfid_uid TEXT(50) NOT NULL,
    action TEXT(10) NOT NULL, -- entry / exit / deny / bind 等
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (student_id) REFERENCES users(student_id) ON DELETE CASCADE
);

-- 索引優化：常按時間、學生或卡號查詢記錄
CREATE INDEX IF NOT EXISTS idx_access_logs_timestamp ON access_logs(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_access_logs_student_id ON access_logs(student_id);
CREATE INDEX IF NOT EXISTS idx_access_logs_rfid_uid ON access_logs(rfid_uid);

-- 4. 註冊暫存 session 表（處理「刷兩次卡」確認綁定邏輯）
CREATE TABLE IF NOT EXISTS registration_sessions (
    student_id TEXT(20) PRIMARY KEY,
    first_uid TEXT(50),
    step INTEGER DEFAULT 0, -- 0=等待第一刷, 1=等待第二刷
    expires_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (student_id) REFERENCES users(student_id) ON DELETE CASCADE
);
