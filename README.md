

# MoliDoor System 

**MoliDoor** 是一套專為 Moli 設計的 IoT 門禁管理系統。整合了 Raspberry Pi、RFID 識別、電磁鎖控制以及 Telegram Bot 通知功能。系統具備即時進出推播、網頁端註冊流程，以及有趣的實驗室數據分析報告。

##  功能特色

### 1. 門禁控制 (Access Control)

- **RFID 快速驗證**：刷卡即時比對權限。
    
- **自動開門**：驗證成功後，透過繼電器觸發電磁鎖3秒後自動上鎖。


### 2. 智慧註冊 (Smart Registration)

- **網頁端發起**：透過 Web 介面填寫學號姓名，並透過 Email 驗證身份。
    
- **雙重刷卡綁定**：註冊模式下，要求使用者連續刷卡兩次以確保 UID 正確寫入，防止誤讀。
    
- **逾時保護**：60 秒內未刷卡自動退出註冊模式。
    

### 3. Telegram Bot 整合 (MoliBot)

- **進出通知**：`"你好！[User] 已進入 moli"`
    
- **註冊通知**：新用戶註冊成功/失敗即時回報。
    
- **空氣品質提醒**：當實驗室人數超過閥值，回傳 `"請大家記得換氣"`。
    
- **📊 每日日報 (18:00)**：
    
    - 最常來的同學。
        
    - 最晚離開的同學（肝帝提醒）。
        
- **👑 每週排行榜 (Pressure King)**：
    
    - 依據停留時數排名（金/銀/銅牌）。
        

---

## 🏗 系統架構 (Architecture)

系統分為 **雲端 (Cloud)** 與 **地端 (Edge)** 兩大部分，透過 Cloudflare Tunnel 進行內網穿透溝通。

### 流程圖 

#### Access control system 

狀態 1：NORMAL（預設：進門邏輯）在此模式下，刷卡永遠不會進註冊流程。

使用者靠卡
Pi 驗證卡片是否已註冊
若合法 → 開門 （3秒）/ 綠燈、嗶一聲 並在tg bot跳通知“你好！xxx已進入moli”，xxx會顯示之用戶名稱
若非法 → 顯示紅燈 / 訊息 逼逼 陌生卡號通知


狀態 2：REGISTER_MODE（掃條碼後加入tg）

觸發方式：  
👉 從tg開啟註冊連結https://midorsys.onrender.com
👉 前端呼叫 Pi：`POST /mode/register?enable=true`

```

使用者於前端填完學號姓名
         │
        ▼
 透過mailserver 寄送email 包含開始註冊的button（一次性連結 get { H(stdID+time) }  ）
         │
        ▼
點擊按鈕開始註冊
         │
        ▼
轉跳到 start 頁面（先讓使用者勾選本人確認）
         │
        ▼
使用者手機顯示「請到讀卡機前刷學生證」- 60 秒倒數計時（地端pi也顯示“[🔄 模式切換] 進入註冊模式！”）
	     │
         ├─- 期間沒有讀到卡 → 自動退出註冊 → 刪除該筆uid為空的資料（前端掉線 ≠ 中止註冊）
         ▼
使用者第一次刷卡 → Pi 讀 UID → 傳給後端 → 後端暫存 first_UID  
         │
        ▼
前端顯示請使用著進行“第二次刷卡” （地端顯示“請再次刷卡”）
         │
        ▼
使用者第二次刷卡 → Pi 再讀 UID → 後端比對 (卡片是否被其他使用者綁定過)
         │
        ├─若不一致 → 回傳錯誤 → 要求重新刷卡（重新開始）
        ▼
若一致 → 綁定成功 → 更新 user.rfid_uid → 回傳 success → 永存UID
         │
        ▼
前端顯示：綁定成功 → 完成註冊 , tg也會跳“註冊者資訊（學號、姓名）綁定成功”通知

```
## 🔌 硬體接線 (Hardware Wiring)

### A. 訊號控制 (Raspberry Pi ↔ Relay)

| Raspberry Pi Pin | Relay Module | 功能說明              |
| ---------------- | ------------ | ----------------- |
| **5V**           | **VCC**      | 供電                |
| **GND**          | **GND**      | 共地                |
| **GPIO 16 **     | **IN**       | 控制訊號 (`LOCK_PIN`) |

匯出到試算表

### B. 電力驅動 (Power & Lock)

**注意**：使用 NC (常閉) 接法，確保斷電時門鎖失效（或依需求設定），此處設定為**通電吸磁，斷電開門**或是**觸發時斷電**。

|起點|終點|說明|
|---|---|---|
|**12V 變壓器 (+)**|Relay **COM** (公共端)|電力輸入|
|**Relay NC** (常閉)|電磁鎖 **V+**|**關鍵**：平常導通吸住，觸發時斷開開門|
|**12V 變壓器 (-)**|電磁鎖 **V-**|接地迴路|
|**Relay NO** (常開)|(懸空)|不使用|


---

## 🚀 安裝與執行 (Installation)

### 1. 環境準備 (Raspberry Pi)

安裝系統依賴與 GPIO 庫：

Bash

```
sudo apt-get update
sudo apt-get install python3-rpi.gpio
```

安裝 Python 套件：

Bash

```
# 進入虛擬環境
source venv/bin/activate

# 安裝依賴
pip install flask requests python-dotenv psycopg2-binary flask-cors
```

### 2. 設定環境變數 (.env)

在 `molidorBackend2` 目錄下建立 `.env` 檔案：

```
# === 資料庫設定 (二選一) ===
# 選項 A: 使用本地 SQLite (最簡單，檔案存在 Pi 上)
DATABASE_URL=sqlite:///./moli.db

# 選項 B: 使用雲端 Neon Postgres (若你想讓資料庫在雲端)
# DATABASE_URL=postgres://neondb_owner:你的密碼@ep-purple-lake...aws.neon.tech/neondb?sslmode=require

# === 系統設定 ===
BOT_TOKEN=你的Telegram_Bot_Token
TG_CHAT_ID=你的Telegram_Chat_ID
SERVER_URL=https://你的網域.com (稍後設定 Cloudflare 後回來填)

# === 郵件設定 (Gmail 為例) ===
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=你的email@gmail.com
SMTP_PASSWORD=你的應用程式密碼

# === 硬體設定 ===
RFID_ENABLED=true
RFID_DEVICE_PATH=/dev/input/event0
# 若不確定路徑，程式碼沒有自動偵測執行 ls -l /dev/input/by-id/. 
```

### 3. 啟動服務

**啟動門禁系統 (Systemd Service)**

Bash

```
# 手動測試執行
sudo ./venv/bin/python door_system.py

# 重啟服務
sudo systemctl restart door_system.service
```

**設定 ngrok ** 用於將地端 API 暴露給雲端呼叫：

Bash

```
ngrok httpd 5001
```

### 4. 資料庫管理

使用 `sqlitebrowser` 查看地端資料庫：

Bash

```
sqlitebrowser moli_door.db
```

若僅用人工填寫註冊資料請使用reg學長撰寫好的腳本

---

## 📊 Analytics Features

系統包含自動化分析腳本，透過 Crontab 或排程執行：

- **人流過多警示**：即時監控 `current_people_count`。
- **每日報告 (18:00)**：統計當日 `entry_logs`。
- **每週壓力王**：計算每週每位使用者的總停留時間 (`Delta = LeaveTime - EntryTime`) 並排序。
    

---

## 📝 參考資料 (References)
	
- [DIY Motion Detection Surveillance System](https://www.reddit.com/r/raspberry_pi/comments/133kkxd/diy_motion_detection_surveillance_system_with/?tl=zh-hant)
- [Raspberry Pi Webcam MJPG Streamer](https://gsyan888.blogspot.com/2013/04/raspberry-pi-webcam-mjpg-streamer.html)
- [Relay Module Wiring Guide](https://xianghu.pixnet.net/blog/post/155977403)
