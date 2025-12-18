-- MOLi 實驗室門禁系統 SQLite 建表腳本
-- 執行方式：sqlite3 data/moli_door.db < sql/init_db.sql

CREATE TABLE IF NOT EXISTS users (
    student_id TEXT(20) PRIMARY KEY,
    name TEXT(50) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_users_student_id ON users(student_id);

CREATE TABLE IF NOT EXISTS user_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT(20) NOT NULL,
    rfid_uid TEXT(50) NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (student_id) REFERENCES users(student_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_user_cards_rfid_uid ON user_cards(rfid_uid);
CREATE INDEX IF NOT EXISTS idx_user_cards_student_id ON user_cards(student_id);

CREATE TABLE IF NOT EXISTS access_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT(20) NOT NULL,
    rfid_uid TEXT(50) NOT NULL,
    action TEXT(10) NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (student_id) REFERENCES users(student_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_access_logs_timestamp ON access_logs(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_access_logs_student_id ON access_logs(student_id);

CREATE TABLE IF NOT EXISTS registration_sessions (
    student_id TEXT(20) PRIMARY KEY,
    first_uid TEXT(50),
    step INTEGER DEFAULT 0,
    expires_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (student_id) REFERENCES users(student_id) ON DELETE CASCADE
);
