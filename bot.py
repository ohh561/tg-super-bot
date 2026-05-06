import os
import logging
import requests
import urllib.parse
import asyncio
import time
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, CallbackQueryHandler, filters

# --- 配置 ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID"))
API_SERVER_URL = "http://telegram-bot-api:8081/bot"

ALIST_WEBDAV_CRYPT = os.getenv("ALIST_WEBDAV_CRYPT", "")
ALIST_WEBDAV_DIRECT = os.getenv("ALIST_WEBDAV_DIRECT", "")
ALIST_USER = os.getenv("ALIST_USER")
ALIST_PASS = os.getenv("ALIST_PASS")

if not ALIST_WEBDAV_CRYPT and os.getenv("ALIST_WEBDAV"):
    ALIST_WEBDAV_CRYPT = os.getenv("ALIST_WEBDAV")

MAX_RETRIES = 3
RETRY_BACKOFF = [5, 15, 30]
CONCURRENT_UPLOADS = 3

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

upload_semaphore = asyncio.Semaphore(CONCURRENT_UPLOADS)

# 用户设置
user_mode = {}
user_autodel = {}
pending_files = {}


# --- 工具函数 ---
def get_mode(user_id):
    return user_mode.get(user_id, "crypt")

def get_webdav_url(user_id):
    mode = get_mode(user_id)
    if mode == "direct":
        return ALIST_WEBDAV_DIRECT, "direct"
    return ALIST_WEBDAV_CRYPT, "crypt"

def get_mode_label(mode):
    return "🔒 混淆 (Crypt)" if mode == "crypt" else "📤 直传 (OneDrive)"

def get_type_folder(file_type):
    if file_type == "video": return "视频"
    elif file_type == "audio": return "音频"
    elif file_type == "photo": return "图片"
    else: return "文件"

def format_size(size_bytes):
    if size_bytes < 1024: return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024: return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024: return f"{size_bytes / (1024 * 1024):.1f} MB"
    else: return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

def make_progress_bar(pct, width=20):
    filled = int(width * pct / 100)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {pct}%"


# --- 文件类型提取 ---
def extract_file(message):
    if message.document: return message.document, "document"
    if message.video: return message.video, "video"
    if message.audio: return message.audio, "audio"
    if message.photo: return message.photo[-1], "photo"
    return None, None

def get_file_name(doc, file_type):
    original = getattr(doc, 'file_name', None)
    if original: return original
    ext_map = {"video": ".mp4", "audio": ".mp3", "photo": ".jpg", "document": ".bin"}
    ext = ext_map.get(file_type, ".bin")
    return f"file_{doc.file_unique_id}{ext}"


# ============================================================
#  上传到 Alist/WebDAV（带进度）
# ============================================================

class ProgressFileWrapper:
    def __init__(self, filepath, callback=None, interval=3):
        self._file = open(filepath, 'rb')
        self._size = os.path.getsize(filepath)
        self._read_bytes = 0
        self._callback = callback
        self._interval = interval
        self._last_time = 0
        self._last_pct = 0

    def read(self, size=-1):
        chunk = self._file.read(size)
        if chunk:
            self._read_bytes += len(chunk)
            now = time.time()
            pct = int(self._read_bytes / self._size * 100) if self._size > 0 else 0
            if self._callback and (now - self._last_time >= self._interval or pct - self._last_pct >= 3):
                self._last_time = now
                self._last_pct = pct
                try: self._callback(pct, self._read_bytes, self._size)
                except Exception: pass
        return chunk

    def __len__(self): return self._size
    def close(self): self._file.close()
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def upload_file(local_path, target_filename, webdav_url, progress_callback=None):
    base_url = webdav_url.rstrip('/')
    target_url = f"{base_url}/{urllib.parse.quote(target_filename)}"

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            logger.info(f"Upload attempt {attempt + 1}/{MAX_RETRIES}: {target_filename}")
            with ProgressFileWrapper(local_path, progress_callback) as f:
                response = requests.put(
                    target_url, data=f,
                    auth=(ALIST_USER, ALIST_PASS),
                    headers={"User-Agent": "SuperBot/3.0"},
                    timeout=7200
                )
            if response.status_code in [200, 201, 204]:
                return response, target_url
            else:
                last_error = f"HTTP {response.status_code}: {response.text[:100]}"
                logger.warning(f"Upload failed: {last_error}")
        except Exception as e:
            last_error = str(e)[:150]
            logger.error(f"Upload error: {e}")

        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_BACKOFF[attempt])

    return None, last_error


# ============================================================
#  后台上传任务
# ============================================================

async def _background_upload(context, query, file_info, file_id, file_size, file_type, filename, type_folder, mode):
    user_id = query.from_user.id
    async with upload_semaphore:
        start_time = time.time()
        msg = await query.edit_message_text(
            f"📥 开始处理...\n"
            f"📄 {filename}\n"
            f"📏 {format_size(file_size)}\n"
            f"📁 {type_folder}\n"
            f"📤 {get_mode_label(mode)}"
        )

        webdav_url, _ = get_webdav_url(user_id)
        upload_webdav = webdav_url.rstrip('/') + f'/{type_folder}/'
        if mode == "direct":
            upload_webdav = webdav_url.rstrip('/') + f'/telegram/{type_folder}/'

        local_path = None
        try:
            # 1. 下载文件
            logger.info(f"Getting file: {file_id[:30]}...")
            new_file = await context.bot.get_file(file_id)
            local_path = new_file.file_path
            logger.info(f"File at: {local_path}, exists={os.path.exists(local_path)}")

            if not os.path.exists(local_path):
                await msg.edit_text(f"❌ 文件不存在: {local_path}")
                return

            actual_size = os.path.getsize(local_path)
            logger.info(f"File size: {actual_size} bytes")

            # 显示下载完成
            await msg.edit_text(
                f"✅ 下载完成\n"
                f"📄 {filename}\n"
                f"📏 {format_size(actual_size)}\n"
                f"📁 {type_folder}\n"
                f"📤 {get_mode_label(mode)}\n"
                f"🚀 开始上传..."
            )

            # 2. 上传（带进度）
            last_update = [0]
            loop = asyncio.get_event_loop()

            def upload_progress_cb(pct, loaded, total):
                now = time.time()
                if now - last_update[0] >= 5:
                    last_update[0] = now
                    try:
                        bar = make_progress_bar(pct)
                        asyncio.run_coroutine_threadsafe(
                            msg.edit_text(
                                f"🚀 上传中...\n"
                                f"📄 {filename}\n"
                                f"📏 {format_size(loaded)} / {format_size(total)}\n"
                                f"📁 {type_folder}\n"
                                f"📤 {get_mode_label(mode)}\n"
                                f"{bar}"
                            ), loop
                        )
                    except Exception: pass

            logger.info(f"Starting upload to {upload_webdav}")
            response, result = await asyncio.to_thread(
                upload_file, local_path, filename, upload_webdav, upload_progress_cb
            )

            elapsed = time.time() - start_time

            if response and response.status_code in [200, 201, 204]:
                if mode == "direct":
                    path_display = f"telegram/{type_folder}/"
                else:
                    path_display = f"{type_folder}/"

                await msg.edit_text(
                    f"✅ 上传完成!\n"
                    f"📄 {filename}\n"
                    f"📏 {format_size(actual_size)}\n"
                    f"📂 {path_display}\n"
                    f"📤 {get_mode_label(mode)}\n"
                    f"⏱️ {elapsed:.0f}秒"
                )

                # 自动删除消息
                if user_autodel.get(user_id, False):
                    try:
                        await asyncio.sleep(3)
                        await msg.delete()
                        orig_chat = file_info.get("chat_id")
                        orig_msg_id = file_info.get("msg_id")
                        if orig_chat and orig_msg_id:
                            await context.bot.delete_message(orig_chat, orig_msg_id)
                    except Exception as e:
                        logger.warning(f"Auto-delete failed: {e}")
            else:
                await msg.edit_text(
                    f"❌ 上传失败\n"
                    f"📄 {filename}\n"
                    f"原因: {result}"
                )

        except Exception as e:
            logger.error(f"Error: {e}")
            await msg.edit_text(f"❌ 错误: {str(e)[:200]}")
        finally:
            # 清理本地文件
            if local_path and os.path.exists(local_path):
                try:
                    os.remove(local_path)
                    logger.info(f"Cleaned up: {local_path}")
                except Exception as e:
                    logger.warning(f"Cleanup failed: {e}")


# ============================================================
#  文件接收 → 按钮选择
# ============================================================

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    doc, file_type = extract_file(update.message)
    if not doc:
        return

    file_size = doc.file_size
    filename = get_file_name(doc, file_type)
    type_folder = get_type_folder(file_type)

    keyboard = [[
        InlineKeyboardButton("🔒 混淆上传", callback_data="upload_crypt"),
        InlineKeyboardButton("📤 直接上传", callback_data="upload_direct"),
    ]]

    doc_obj, _ = extract_file(update.message)
    pending_files[update.message.message_id] = {
        "file_id": doc_obj.file_id,
        "file_type": file_type,
        "file_size": file_size,
        "filename": filename,
        "chat_id": update.message.chat_id,
        "msg_id": update.message.message_id,
    }

    await update.message.reply_text(
        f"📄 **{filename}**\n"
        f"📏 {format_size(file_size)}\n"
        f"📁 {type_folder}\n\n"
        f"选择上传方式：",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def callback_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ALLOWED_USER_ID:
        await query.answer("无权限")
        return

    await query.answer()
    mode = query.data.replace("upload_", "")
    user_mode[query.from_user.id] = mode

    file_info = None
    reply_msg = query.message.reply_to_message
    if reply_msg and reply_msg.message_id in pending_files:
        file_info = pending_files.pop(reply_msg.message_id)
    elif pending_files:
        latest_id = max(pending_files.keys())
        file_info = pending_files.pop(latest_id)

    if not file_info:
        await query.edit_message_text("❌ 找不到文件信息，请重新发送文件")
        return

    file_id = file_info["file_id"]
    file_type = file_info["file_type"]
    file_size = file_info["file_size"]
    filename = file_info["filename"]
    type_folder = get_type_folder(file_type)

    remaining = upload_semaphore._value
    queue_text = "⏳ 已满，排队等待..." if remaining == 0 else f"🟢 剩余 {remaining}/{CONCURRENT_UPLOADS} 个槽位"

    await query.edit_message_text(
        f"📥 准备上传...\n"
        f"📄 {filename}\n"
        f"📏 {format_size(file_size)}\n"
        f"📁 {type_folder}\n"
        f"📤 {get_mode_label(mode)}\n"
        f"{queue_text}"
    )

    asyncio.create_task(_background_upload(
        context, query, file_info, file_id, file_size, file_type, filename, type_folder, mode
    ))


# ============================================================
#  命令
# ============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    mode = get_mode(update.effective_user.id)
    autodel = "✅ 开" if user_autodel.get(update.effective_user.id, False) else "❌ 关"
    await update.message.reply_text(
        "🤖 **文件上传机器人 v3.1**\n\n"
        "发送 文件/视频/音频/图片，选择模式即可上传。\n\n"
        f"⚡ **并发上传：** {CONCURRENT_UPLOADS} 个\n"
        f"🔄 **失败重试：** {MAX_RETRIES} 次\n\n"
        f"📤 **当前模式：** {get_mode_label(mode)}\n"
        f"🗑️ **自动删除：** {autodel}\n\n"
        "📁 **混淆路径：** PrivateVideo/{类型}/\n"
        "📁 **直传路径：** OneDrive/telegram/{类型}/\n\n"
        "命令：\n"
        "/start - 显示帮助\n"
        "/autodel - 开关自动删除消息\n"
        "/ping - 检查连接状态"
    )

async def cmd_autodel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    uid = update.effective_user.id
    user_autodel[uid] = not user_autodel.get(uid, False)
    status = "✅ 已开启" if user_autodel[uid] else "❌ 已关闭"
    await update.message.reply_text(f"🗑️ 自动删除消息：{status}")

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    mode = get_mode(update.effective_user.id)
    autodel = "✅ 开" if user_autodel.get(update.effective_user.id, False) else "❌ 关"

    statuses = []
    for name, url in [("Crypt", ALIST_WEBDAV_CRYPT), ("Direct", ALIST_WEBDAV_DIRECT)]:
        if not url: continue
        try:
            resp = requests.request("PROPFIND", url, auth=(ALIST_USER, ALIST_PASS),
                                    headers={"Depth": "0"}, timeout=10)
            statuses.append(f"{'✅' if resp.status_code in [200,207] else '❌'} {name}")
        except:
            statuses.append(f"❌ {name}")

    await update.message.reply_text(
        f"🏓 Pong!\n"
        f"{' | '.join(statuses)}\n"
        f"📤 模式: {get_mode_label(mode)}\n"
        f"🗑️ 自动删除: {autodel}\n"
        f"⚡ 并发槽: {CONCURRENT_UPLOADS - upload_semaphore._value}/{CONCURRENT_UPLOADS} 使用中"
    )


# ============================================================
#  启动
# ============================================================

def check_alist():
    for name, url in [("Crypt", ALIST_WEBDAV_CRYPT), ("Direct", ALIST_WEBDAV_DIRECT)]:
        if not url: continue
        try:
            resp = requests.request("PROPFIND", url, auth=(ALIST_USER, ALIST_PASS),
                                    headers={"Depth": "0"}, timeout=10)
            if resp.status_code in [200, 207]:
                logger.info(f"✅ {name} 连接正常")
            else:
                logger.warning(f"⚠️ {name} 返回 {resp.status_code}")
        except Exception as e:
            logger.error(f"❌ {name} 连接失败: {e}")

if __name__ == '__main__':
    check_alist()
    app = ApplicationBuilder().token(BOT_TOKEN).base_url(API_SERVER_URL) \
        .read_timeout(3600).write_timeout(300).connect_timeout(60).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("autodel", cmd_autodel))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CallbackQueryHandler(callback_upload, pattern="^upload_"))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.PHOTO,
        handle_file
    ))
    logger.info(f"🤖 Super Bot v3.1 is running... (concurrency={CONCURRENT_UPLOADS})")
    app.run_polling()
