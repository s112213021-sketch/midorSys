# MOLi Telegram Bot — 啟動與安裝說明

這個檔案說明如何安裝相依套件並執行 `tg_bot_runner.py`。

1) 建議建立並啟用虛擬環境
```bash
cd /Users/a-----/Desktop/dor_v3/midorSys-main
python3 -m venv ../.venv
source ../.venv/bin/activate
```

2) 安裝需求
```bash
pip install -r requirements.txt
# 或僅安裝 telegram 套件
pip install python-telegram-bot --upgrade
```

3) 設定 BOT_TOKEN
在專案根目錄或 /etc/default/molidor_bot 中設定環境變數 `BOT_TOKEN`，例如：
```bash
export BOT_TOKEN="<your-bot-token>"
```

4) 本地測試啟動
```bash
source ../.venv/bin/activate
python tg_bot_runner.py
```

5) 建議以 systemd 運行（範例單元檔 `molidor_bot.service` 已放在本目錄）
- 把你的 BOT_TOKEN 加到 `/etc/default/molidor_bot`：
  `BOT_TOKEN="<your-bot-token>"`
- 重新載入 systemd：
```
sudo systemctl daemon-reload
sudo systemctl enable --now molidor_bot.service
sudo journalctl -u molidor_bot.service -f
```

6) 相機模組
如果要使用 `/now` 與 `/snapshot`，請確保專案中有 `camera.monitor` 模組，並提供 `capture_and_detect_once()`、`capture_and_snapshot()` 等函式，以及 `monitor.lock`、`monitor.latest_count`、`monitor.last_update_time` 屬性。
