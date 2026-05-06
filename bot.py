import os
import io
import logging
import requests
import urllib.parse
import asyncio
import threading
import time
import queue
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

MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
MAX_RETRIES = 3
RETRY_BACKOFF = [5, 15, 30]
CONCURRENT_UPLOADS = 3
STREAM_CHUNK_SIZE = 4 * 1024 * 1024  # 4MB per chunk
STREAM_BUFFER_CHUNKS = 10  # buffer 10 chunks = 40MB per task

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# 并发限制
upload_semaphore = asyncio.Semaphore(CONCURRENT_UPLOADS)

# 用户设置
user_mode = {}       # user_id -> "crypt" | "direct"
user_autodel = {}    # user_id -> True | False

# 临时文件信息存储：msg_id -> {file_id, file_type, file_size, filename}
pending_files = {}


# --- 模式/路径工具 ---
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
#  流式上传核心：边下边传
# ============================================================

class StreamingBuffer(io.RawIOBase):
    """线程安全的流式缓冲区。写入端(下载线程) -> 读取端(上传线程)"""
    def __init__(self):
        self._buffer = b""
        self._lock = threading.Lock()
        self._data_available = threading.Condition(self._lock)
        self._finished = False
        self._total_written = 0

    def write(self, data: bytes) -> int:
        if not data:
            return 0
        with self._lock:
            self._buffer += data
            self._total_written += len(data)
            self._data_available.notify()
        return len(data)

    def finish(self):
        with self._lock:
            self._finished = True
            self._data_available.notify()

    def read(self, size: int = -1) -> bytes:
        with self._data_available:
            while not self._buffer:
                if self._finished:
                    return b""
                self._data_available.wait(timeout=30)
            if size == -1 or size is None:
                data = self._buffer
                self._buffer = b""
            else:
                data = self._buffer[:size]
                self._buffer = self._buffer[size:]
        return data

    def readable(self): return True
    def writable(self): return True

    @property
    def total_written(self): return self._total_written


def stream_upload_to_alist(local_path_or_stream, target_filename, webdav_url,
                           progress_callback=None, is_stream=False):
    """流式上传。is_stream=True 时 local_path_or_stream 是 StreamingBuffer"""
    base_url = webdav_url.rstrip('/')
    target_url = f"{base_url}/{urllib.parse.quote(target_filename)}"

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            logger.info(f"Upload attempt {attempt + 1}/{MAX_RETRIES}: {target_filename}")
            if is_stream:
                # 流式模式：直接传 StreamingBuffer 对象
                response = requests.put(
                    target_url, data=local_path_or_stream,
                    auth=(ALIST_USER, ALIST_PASS),
                    headers={"User-Agent": "SuperBot/3.0"},
                    timeout=7200
                )
            else:
                # 磁盘模式：带进度追踪
                with ProgressFileWrapper(local_path_or_stream, progress_callback) as f:
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
            logger.error(f"Upload error attempt {attempt + 1}: {e}")

        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_BACKOFF[attempt])

    return None, last_error


class ProgressFileWrapper:
    """磁盘文件进度追踪包装器"""
    def __init__(self, filepath, callback=None, interval=5):
        self._file = open(filepath, 'rb')
        self._size = os.path.getsize(filepath)
        self._read_bytes = 0
        self._callback = callback
        self._interval = interval
        self._last_report_time = 0
        self._last_report_pct = 0

    def read(self, size=-1):
        chunk = self._file.read(size)
        if chunk:
            self._read_bytes += len(chunk)
            now = time.time()
            pct = int(self._read_bytes / self._size * 100) if self._size > 0 else 0
            if self._callback and (now - self._last_report_time >= self._interval or pct - self._last_report_pct >= 5):
                self._last_report_time = now
                self._last_report_pct = pct
                try: self._callback(pct, self._read_bytes, self._size)
                except Exception: pass
        return chunk

    def __len__(self): return self._size
    def close(self): self._file.close()
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


# ============================================================
#  流式下载+上传（核心流程）
# ============================================================

async def do_stream_upload(user_id, context, msg, filename, file_size, file_type, mode, file_id):
    """尝试流式上传，失败则回退到磁盘模式"""
    webdav_url, _ = get_webdav_url(user_id)
    type_folder = get_type_folder(file_type)
    upload_webdav = webdav_url.rstrip('/') + f'/{type_folder}/'
    if mode == "direct":
        upload_webdav = webdav_url.rstrip('/') + f'/telegram/{type_folder}/'

    # 获取事件循环（供线程回调使用）
    loop = asyncio.get_event_loop()

    # 创建流式缓冲区
    buf = StreamingBuffer()
    download_done = threading.Event()
    download_error = [None]
    downloaded_bytes = [0]

    # 下载进度回调（在下载线程中调用）
    last_progress_update = [0]
    def on_download_progress(current, total):
        downloaded_bytes[0] = current
        now = time.time()
        if now - last_progress_update[0] >= 5:
            last_progress_update[0] = now
            pct = int(current / total * 100) if total > 0 else 0
            try:
                asyncio.run_coroutine_threadsafe(
                    msg.edit_text(
                        f"📥 下载中...\n"
                        f"📄 {filename}\n"
                        f"📏 {format_size(current)} / {format_size(total)}\n"
                        f"📁 {type_folder}\n"
                        f"📤 {get_mode_label(mode)}\n"
                        f"{make_progress_bar(pct)}"
                    ), loop
                )
            except Exception: pass

    # 在后台线程执行下载
    def do_download():
        try:
            new_file = asyncio.run_coroutine_threadsafe(
                context.bot.get_file(file_id), loop
            ).result(timeout=60)

            local_path = new_file.file_path

            if os.path.exists(local_path):
                # 本地文件模式：从磁盘流式读取
                with open(local_path, 'rb') as f:
                    while True:
                        chunk = f.read(STREAM_CHUNK_SIZE)
                        if not chunk:
                            break
                        buf.write(chunk)
                        on_download_progress(buf.total_written, file_size)
            else:
                # HTTP 流式下载
                resp = requests.get(new_file.file_url, stream=True, timeout=300)
                for chunk in resp.iter_content(chunk_size=STREAM_CHUNK_SIZE):
                    if chunk:
                        buf.write(chunk)
                        on_download_progress(buf.total_written, file_size)
        except Exception as e:
            logger.error(f"Download error: {e}")
            download_error[0] = str(e)
        finally:
            buf.finish()
            download_done.set()

    download_thread = threading.Thread(target=do_download, daemon=True)
    download_thread.start()

    # 在后台线程执行上传（从缓冲区读取）
    upload_result = [None, None]
    def do_upload():
        resp, url = stream_upload_to_alist(
            buf, filename, upload_webdav, is_stream=True
        )
        upload_result[0] = resp
        upload_result[1] = url

    upload_thread = threading.Thread(target=do_upload, daemon=True)
    upload_thread.start()

    # 等待完成
    await asyncio.to_thread(download_thread.join, timeout=7200)
    await asyncio.to_thread(upload_thread.join, timeout=7200)

    # 检查结果
    if download_error[0]:
        return None, f"下载失败: {download_error[0]}"

    resp = upload_result[0]
    url = upload_result[1]
    if resp and resp.status_code in [200, 201, 204]:
        return resp, url
    else:
        return None, url or "上传失败"


# ============================================================
#  磁盘模式上传（回退方案）
# ============================================================

async def do_disk_upload(user_id, context, msg, filename, file_size, file_type, mode, file_id):
    """传统磁盘模式：先下载到本地，再上传"""
    webdav_url, _ = get_webdav_url(user_id)
    type_folder = get_type_folder(file_type)
    upload_webdav = webdav_url.rstrip('/') + f'/{type_folder}/'
    if mode == "direct":
        upload_webdav = webdav_url.rstrip('/') + f'/telegram/{type_folder}/'

    try:
        new_file = await context.bot.get_file(file_id)
        local_path = new_file.file_path

        if not os.path.exists(local_path):
            return None, f"文件不存在: {local_path}"

        # 上传进度
        last_update = [0]
        loop = asyncio.get_event_loop()
        def progress_cb(pct, loaded, total):
            now = time.time()
            if now - last_update[0] >= 8:
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

        response, result = await asyncio.to_thread(
            stream_upload_to_alist, local_path, filename, upload_webdav, progress_cb, False
        )

        # 清理本地文件
        try: os.remove(local_path)
        except: pass

        if response and response.status_code in [200, 201, 204]:
            return response, result
        else:
            return None, result

    except Exception as e:
        return None, str(e)[:150]


# ============================================================
#  上传主流程（带重试和回退）
# ============================================================

async def do_upload(user_id, context, msg, filename, file_size, file_type, mode, file_id):
    """先尝试流式，失败回退磁盘模式"""
    # 先试流式
    resp, result = await do_stream_upload(user_id, context, msg, filename, file_size, file_type, mode, file_id)
    if resp and resp.status_code in [200, 201, 204]:
        return resp, result, "stream"

    logger.warning(f"Stream upload failed ({result}), falling back to disk mode")

    try:
        await msg.edit_text(
            f"⚠️ 流式上传失败，切换磁盘模式重试...\n"
            f"📄 {filename}\n"
            f"📏 {format_size(file_size)}\n"
            f"📤 {get_mode_label(mode)}"
        )
    except: pass

    # 回退磁盘模式
    resp, result = await do_disk_upload(user_id, context, msg, filename, file_size, file_type, mode, file_id)
    return resp, result, "disk"


# ============================================================
#  按钮处理
# ============================================================

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """收到文件 → 弹出模式选择按钮"""
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    doc, file_type = extract_file(update.message)
    if not doc:
        return

    file_size = doc.file_size
    filename = get_file_name(doc, file_type)
    type_folder = get_type_folder(file_type)

    if file_size > MAX_FILE_SIZE:
        await update.message.reply_text(
            f"❌ 文件太大: {format_size(file_size)}\n"
            f"限制: {format_size(MAX_FILE_SIZE)}"
        )
        return

    keyboard = [
        [
            InlineKeyboardButton("🔒 混淆上传", callback_data=f"upload_crypt"),
            InlineKeyboardButton("📤 直接上传", callback_data=f"upload_direct"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # 存储文件信息，供回调使用
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
        reply_markup=reply_markup
    )


async def callback_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理模式选择按钮 → 开始上传"""
    query = update.callback_query
    if query.from_user.id != ALLOWED_USER_ID:
        await query.answer("无权限")
        return

    await query.answer()

    mode = query.data.replace("upload_", "")
    user_mode[query.from_user.id] = mode

    # 从 pending_files 获取文件信息
    # 找到对应的原始消息ID（bot回复的消息引用了原始文件消息）
    # 遍历 pending_files 找最近的未处理条目
    file_info = None
    bot_msg_id = query.message.message_id

    # 尝试通过 reply_to_message 获取
    reply_msg = query.message.reply_to_message
    if reply_msg and reply_msg.message_id in pending_files:
        file_info = pending_files.pop(reply_msg.message_id)
    else:
        # 回退：找 pending_files 中最新的条目
        if pending_files:
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

    # 检查队列
    remaining = upload_semaphore._value
    if remaining == 0:
        queue_text = "⏳ 已满，排队等待..."
    else:
        queue_text = f"🟢 剩余 {remaining}/{CONCURRENT_UPLOADS} 个槽位"

    await query.edit_message_text(
        f"📥 准备上传...\n"
        f"📄 {filename}\n"
        f"📏 {format_size(file_size)}\n"
        f"📁 {type_folder}\n"
        f"📤 {get_mode_label(mode)}\n"
        f"{queue_text}"
    )

    # 启动后台任务，不阻塞 handler
    asyncio.create_task(_background_upload(
        context, query, file_info, file_id, file_size, file_type, filename, type_folder, mode
    ))


async def _background_upload(context, query, file_info, file_id, file_size, file_type, filename, type_folder, mode):
    """后台上传任务，不阻塞事件循环"""
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

        resp, result, upload_mode = await do_upload(
            user_id, context, msg, filename, file_size, file_type, mode, file_id
        )

        elapsed = time.time() - start_time

        if resp and resp.status_code in [200, 201, 204]:
            mode_tag = "⚡流式" if upload_mode == "stream" else "💾磁盘"
            if mode == "direct":
                path_display = f"telegram/{type_folder}/"
            else:
                path_display = f"{type_folder}/"

            await msg.edit_text(
                f"✅ 上传完成!\n"
                f"📄 {filename}\n"
                f"📏 {format_size(file_size)}\n"
                f"📂 {path_display}\n"
                f"📤 {get_mode_label(mode)}\n"
                f"⏱️ {elapsed:.0f}秒 {mode_tag}"
            )

            # 自动删除消息
            if user_autodel.get(query.from_user.id, False):
                try:
                    await asyncio.sleep(3)
                    await msg.delete()
                    # 删除原始文件消息
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


# ============================================================
#  命令处理
# ============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    mode = get_mode(update.effective_user.id)
    autodel = "✅ 开" if user_autodel.get(update.effective_user.id, False) else "❌ 关"
    await update.message.reply_text(
        "🤖 **文件上传机器人 v3.0**\n\n"
        "发送 文件/视频/音频/图片，选择模式即可上传。\n\n"
        "📋 **支持的类型：**\n"
        "• 文档 / 视频 / 音频 / 图片\n\n"
        f"📏 **大小限制：** {format_size(MAX_FILE_SIZE)}\n"
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
    await update.message.reply_text(
        f"🗑️ 自动删除消息：{status}\n\n"
        + ("上传完成后会自动删除你发送的文件消息和 bot 的回复。" if user_autodel[uid]
           else "上传完成后消息会保留。")
    )


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    mode = get_mode(update.effective_user.id)
    webdav_url, _ = get_webdav_url(update.effective_user.id)
    autodel = "✅ 开" if user_autodel.get(update.effective_user.id, False) else "❌ 关"

    # 连通性检查
    statuses = []
    for name, url in [("Crypt", ALIST_WEBDAV_CRYPT), ("Direct", ALIST_WEBDAV_DIRECT)]:
        if not url:
            continue
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
#  启动检查
# ============================================================

def check_alist():
    for name, url in [("Crypt", ALIST_WEBDAV_CRYPT), ("Direct", ALIST_WEBDAV_DIRECT)]:
        if not url:
            logger.warning(f"⚠️ {name} WebDAV 未配置")
            continue
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
        .read_timeout(300).write_timeout(60).connect_timeout(30).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("autodel", cmd_autodel))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CallbackQueryHandler(callback_upload, pattern="^upload_"))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.PHOTO,
        handle_file
    ))

    logger.info(f"🤖 Super Bot v3.0 is running... (concurrency={CONCURRENT_UPLOADS})")
    app.run_polling()
