#!/usr/bin/env python3
"""Telegram bot runner for MOLi door access camera.

Commands:
 - /start: show welcome and quick buttons
 - /now: take an immediate photo (plain annotated)
 - /snapshot: take a designed snapshot with decorative frame
 - /status: show last known count and time

This file will try to import `camera.monitor` and `tg_bot_basic.start`.
If those modules are not present, simple fallbacks will reply with informative messages.
"""

import os
import asyncio
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("請在 .env 設定 BOT_TOKEN")

# Try to import monitor (camera) and start handler; provide stubs if missing
try:
    from camera import monitor
except Exception:
    monitor = None

try:
    from tg_bot_basic import start as start_handler
except Exception:
    async def start_handler(update, context):
        await update.message.reply_text("歡迎使用 MOLi Bot（start handler 未實作）")

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

executor = ThreadPoolExecutor(max_workers=2)


async def _run_blocking(func, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, lambda: func(*args))


async def now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📷 相機啟動中，請稍等 6~10 秒...")
    if not monitor:
        await update.message.reply_text("❌ camera.monitor 未安裝或不可用，請先部署相機模組")
        return
    # run blocking capture in threadpool
    result = await _run_blocking(monitor.capture_and_detect_once)
    jpeg_bytes, count, time_str = result

    if jpeg_bytes is None:
        await update.message.reply_text("❌ 拍攝失敗，請檢查相機排線後再試")
        return

    await update.message.reply_photo(
        photo=jpeg_bytes,
        caption=f"🔔 MOLi 實驗室即時照片\n👥 人數: {count} 人\n🕒 時間: {time_str}"
    )


async def snapshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📷 正在拍攝門禁樣式快照，請稍等...")
    if not monitor:
        await update.message.reply_text("❌ camera.monitor 未安裝或不可用，請先部署相機模組")
        return
    result = await _run_blocking(monitor.capture_and_snapshot)
    jpeg_bytes, count, time_str = result

    if jpeg_bytes is None:
        await update.message.reply_text("❌ 拍攝失敗，請檢查相機排線後再試")
        return

    await update.message.reply_photo(
        photo=jpeg_bytes,
        caption=f"🔐 MOLi 門禁快照\n👥 人數: {count} 人\n🕒 時間: {time_str}"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not monitor:
        await update.message.reply_text("❌ camera.monitor 未安裝或不可用，無法顯示狀態")
        return
    # read persisted latest values under lock if available
    try:
        with monitor.lock:
            count = monitor.latest_count
            time_str = monitor.last_update_time
    except Exception:
        count = getattr(monitor, 'latest_count', 'N/A')
        time_str = getattr(monitor, 'last_update_time', 'N/A')

    await update.message.reply_text(
        f"📊 MOLi 實驗室狀態\n👥 人數: {count} 人\n🕒 最後更新: {time_str}\n輸入 /now 立即拍攝"
    )


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler('start', start_handler))
    app.add_handler(CommandHandler('now', now))
    app.add_handler(CommandHandler('snapshot', snapshot))
    app.add_handler(CommandHandler('status', status))

    print("啟動 Telegram Bot...")
    app.run_polling()


if __name__ == '__main__':
    main()
