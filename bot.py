import os
import logging
import aiohttp
import asyncio
import time
from datetime import datetime
from urllib.parse import quote as urlquote
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
ALIST_API_URL = os.getenv("ALIST_API_URL", "http://openlist:5244")

if not ALIST_WEBDAV_CRYPT and os.getenv("ALIST_WEBDAV"):
    ALIST_WEBDAV_CRYPT = os.getenv("ALIST_WEBDAV")

MAX_RETRIES = 3
RETRY_BACKOFF = [5, 15, 30]
CONCURRENT_UPLOADS = 3
STREAM_CHUNK_SIZE = 4 * 1024 * 1024

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

upload_semaphore = asyncio.Semaphore(CONCURRENT_UPLOADS)

# --- 用户状态 ---
user_mode = {}           # user_id -> "crypt" / "direct"
user_autodel = {}        # user_id -> bool
user_folder = {}         # user_id -> selected folder path (for direct mode)
pending_files = {}       # msg_id -> file_info
cancel_flags = {}        # task_id -> bool

# AList token cache
alist_token = None
alist_token_time = 0

# 文件夹浏览状态
folder_browse = {}       # user_id -> {"path": "/", "page": 1}


# ============================================================
#  AList API
# ============================================================

async def alist_login():
    """登录 AList 获取 token"""
    global alist_token, alist_token_time
    if alist_token and time.time() - alist_token_time < 3600:
        return alist_token

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{ALIST_API_URL}/api/auth/login",
            json={"username": ALIST_USER, "password": ALIST_PASS},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("code") == 200:
                alist_token = data["data"]["token"]
                alist_token_time = time.time()
                logger.info("AList login success")
                return alist_token
            else:
                logger.error(f"AList login failed: {data}")
                return None


async def alist_list_dir(path="/", page=1, per_page=100):
    """列出 AList 目录"""
    token = await alist_login()
    if not token:
        return None

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{ALIST_API_URL}/api/fs/list",
            json={"path": path, "page": page, "per_page": per_page, "refresh": False},
            headers={"Authorization": token},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("code") == 200:
                return data["data"]
            logger.error(f"AList list failed: {data}")
            return None


# ============================================================
#  工具函数
# ============================================================

def get_mode(user_id):
    return user_mode.get(user_id, "crypt")

def get_mode_label(mode):
    return "🔒 混淆 (Crypt)" if mode == "crypt" else "📤 直传 (OneDrive)"

def get_type_folder(file_type):
    return {"video": "视频", "audio": "音频", "photo": "图片"}.get(file_type, "文件")

def format_size(size_bytes):
    if size_bytes < 1024: return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024: return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024: return f"{size_bytes / (1024 * 1024):.1f} MB"
    else: return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

def make_progress_bar(pct, width=20):
    filled = int(width * pct / 100)
    return f"[{'█' * filled}{'░' * (width - filled)}] {pct}%"

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
    return f"file_{doc.file_unique_id}{ext_map.get(file_type, '.bin')}"

def get_upload_webdav(user_id, mode):
    """获取上传 WebDAV 地址（带用户选择的文件夹）"""
    if mode == "direct":
        base = user_folder.get(user_id, "")
        if base:
            # 用户选了自定义路径，构造 WebDAV URL
            # AList WebDAV 路径格式: /dav/{path}
            webdav_base = ALIST_WEBDAV_DIRECT.rstrip('/')
            return f"{webdav_base}{base}/" if base != "/" else f"{webdav_base}/"
        return ALIST_WEBDAV_DIRECT
    return ALIST_WEBDAV_CRYPT


# ============================================================
#  /folder — 选择上传文件夹
# ============================================================

async def cmd_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    folder_browse[update.effective_user.id] = {"path": "/", "page": 1}
    await show_folder(update, context, "/", 1)


async def show_folder(update_or_query, context, path, page):
    """显示目录内容（按钮形式）"""
    data = await alist_list_dir(path, page)
    if not data:
        text = f"❌ 无法访问目录: {path}"
        if hasattr(update_or_query, 'message') and update_or_query.message:
            await update_or_query.message.reply_text(text)
        else:
            await update_or_query.edit_message_text(text)
        return

    content = data.get("content") or []
    total = data.get("total", 0)

    keyboard = []

    # 当前目录显示
    current_display = path if path != "/" else "/（根目录）"
    header = f"📂 当前: {current_display}\n"

    # 文件夹按钮
    for item in content:
        if item.get("is_dir"):
            name = item["name"]
            sub_path = f"{path.rstrip('/')}/{name}"
            keyboard.append([InlineKeyboardButton(
                f"📁 {name}", callback_data=f"folder:{sub_path}"
            )])

    if not content or not any(i.get("is_dir") for i in content):
        header += "（此目录下没有子文件夹）\n"

    # 导航按钮
    nav_row = []
    if path != "/":
        parent = "/".join(path.rstrip("/").split("/")[:-1]) or "/"
        nav_row.append(InlineKeyboardButton("⬆️ 上级", callback_data=f"folder:{parent}"))
    nav_row.append(InlineKeyboardButton("✅ 上传到这里", callback_data=f"folder_select:{path}"))
    keyboard.append(nav_row)

    # 分页
    if total > 100:
        page_row = []
        if page > 1:
            page_row.append(InlineKeyboardButton("◀️ 上页", callback_data=f"folder_page:{path}:{page-1}"))
        page_row.append(InlineKeyboardButton(f"📄 {page}", callback_data="folder_noop"))
        if page * 100 < total:
            page_row.append(InlineKeyboardButton("▶️ 下页", callback_data=f"folder_page:{path}:{page+1}"))
        keyboard.append(page_row)

    text = header + "\n选择子目录进入，或点「上传到这里」设为默认路径"
    markup = InlineKeyboardMarkup(keyboard)

    if hasattr(update_or_query, 'callback_query') and update_or_query.callback_query:
        await update_or_query.callback_query.edit_message_text(text, reply_markup=markup)
    elif hasattr(update_or_query, 'message') and update_or_query.message:
        await update_or_query.message.reply_text(text, reply_markup=markup)
    else:
        # query object directly
        await update_or_query.edit_message_text(text, reply_markup=markup)


async def callback_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理文件夹导航"""
    query = update.callback_query
    if query.from_user.id != ALLOWED_USER_ID:
        await query.answer("无权限")
        return

    await query.answer()
    data = query.data

    if data.startswith("folder_select:"):
        # 确认选择
        path = data.replace("folder_select:", "")
        user_folder[query.from_user.id] = path
        current = path if path != "/" else "/（根目录）"
        await query.edit_message_text(f"✅ 默认上传路径已设置为:\n📂 {current}")
        return

    if data == "folder_noop":
        return

    if data.startswith("folder_page:"):
        parts = data.split(":")
        path = parts[1]
        page = int(parts[2])
        folder_browse[query.from_user.id] = {"path": path, "page": page}
        await show_folder(query, context, path, page)
        return

    if data.startswith("folder:"):
        path = data.replace("folder:", "")
        folder_browse[query.from_user.id] = {"path": path, "page": 1}
        await show_folder(query, context, path, 1)


# ============================================================
#  WebDAV 上传（aiohttp）
# ============================================================

async def upload_file_async(local_path, target_url, progress_callback=None):
    """异步上传文件到 WebDAV"""
    file_size = os.path.getsize(local_path)
    last_update = {"time": 0, "pct": 0}

    async def stream_reader():
        loop = asyncio.get_event_loop()
        with open(local_path, 'rb') as f:
            while True:
                chunk = await loop.run_in_executor(None, f.read, STREAM_CHUNK_SIZE)
                if not chunk:
                    break
                if progress_callback:
                    yield chunk
                else:
                    yield chunk

    class ProgressStream:
        def __init__(self, path, cb):
            self._path = path
            self._cb = cb
            self._size = os.path.getsize(path)
            self._sent = 0
            self._loop = asyncio.get_event_loop()

        def __aiter__(self):
            return self

        async def __anext__(self):
            chunk = await asyncio.get_event_loop().run_in_executor(
                None, self._read_chunk
            )
            if not chunk:
                raise StopAsyncIteration
            return chunk

        def _read_chunk(self):
            if not hasattr(self, '_file'):
                self._file = open(self._path, 'rb')
            chunk = self._file.read(STREAM_CHUNK_SIZE)
            if chunk:
                self._sent += len(chunk)
                pct = int(self._sent / self._size * 100) if self._size > 0 else 0
                now = time.time()
                if self._cb and pct - last_update["pct"] >= 3 and now - last_update["time"] >= 3:
                    last_update["time"] = now
                    last_update["pct"] = pct
                    try:
                        self._cb(pct, self._sent, self._size)
                    except Exception:
                        pass
            else:
                self._file.close()
            return chunk

    for attempt in range(MAX_RETRIES):
        try:
            async with aiohttp.ClientSession() as session:
                with open(local_path, 'rb') as f:
                    async with session.put(
                        target_url,
                        data=f,
                        auth=aiohttp.BasicAuth(ALIST_USER, ALIST_PASS),
                        headers={"User-Agent": "SuperBot/4.0"},
                        timeout=aiohttp.ClientTimeout(total=7200)
                    ) as resp:
                        if resp.status in [200, 201, 204]:
                            return True, target_url
                        body = await resp.text()
                        return False, f"HTTP {resp.status}: {body[:100]}"
        except Exception as e:
            logger.error(f"Upload attempt {attempt + 1} failed: {e}")
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_BACKOFF[attempt])
            else:
                return False, str(e)[:150]

    return False, "Max retries exceeded"


# ============================================================
#  后台上传任务
# ============================================================

async def _background_upload(context, query, file_info, file_id, file_size, file_type, filename, type_folder, mode):
    user_id = query.from_user.id
    task_id = f"{user_id}_{int(time.time())}"
    cancel_flags[task_id] = False

    async with upload_semaphore:
        start_time = time.time()
        msg = await query.edit_message_text(
            f"📥 开始处理...\n📄 {filename}\n📏 {format_size(file_size)}\n"
            f"📁 {type_folder}\n📤 {get_mode_label(mode)}"
        )

        webdav_base = get_upload_webdav(user_id, mode)
        upload_webdav = webdav_base.rstrip('/') + f'/{type_folder}/'
        if mode == "direct":
            upload_webdav = webdav_base.rstrip('/') + f'/{type_folder}/'

        local_path = None
        try:
            # 1. 下载文件
            logger.info(f"Getting file: {file_id[:30]}...")
            new_file = None
            for dl_attempt in range(3):
                if cancel_flags.get(task_id):
                    await msg.edit_text("⏹️ 已取消")
                    return
                try:
                    new_file = await context.bot.get_file(file_id)
                    break
                except Exception as dl_err:
                    logger.warning(f"Download attempt {dl_attempt + 1}/3 failed: {dl_err}")
                    if dl_attempt < 2:
                        await asyncio.sleep(5 * (dl_attempt + 1))
                    else:
                        raise dl_err

            local_path = new_file.file_path
            if not os.path.exists(local_path):
                await msg.edit_text(f"❌ 文件不存在: {local_path}")
                return

            actual_size = os.path.getsize(local_path)
            await msg.edit_text(
                f"✅ 下载完成\n📄 {filename}\n📏 {format_size(actual_size)}\n"
                f"📁 {type_folder}\n📤 {get_mode_label(mode)}\n🚀 开始上传..."
            )

            # 2. 上传（带进度）
            last_update = {"time": 0, "pct": 0}
            loop = asyncio.get_event_loop()

            def upload_progress_cb(pct, loaded, total):
                now = time.time()
                pct_diff = pct - last_update["pct"]
                time_diff = now - last_update["time"]
                if time_diff >= 5 and pct_diff >= 3:
                    last_update["time"] = now
                    last_update["pct"] = pct
                    try:
                        bar = make_progress_bar(pct)
                        asyncio.run_coroutine_threadsafe(
                            msg.edit_text(
                                f"🚀 上传中...\n📄 {filename}\n"
                                f"📏 {format_size(loaded)} / {format_size(total)}\n"
                                f"📁 {type_folder}\n📤 {get_mode_label(mode)}\n{bar}"
                            ), loop
                        )
                    except Exception:
                        pass

            logger.info(f"Starting upload to {upload_webdav}")
            success, result = await upload_file_async(local_path, upload_webdav, upload_progress_cb)

            if cancel_flags.get(task_id):
                await msg.edit_text("⏹️ 已取消")
                return

            elapsed = time.time() - start_time

            if success:
                await msg.edit_text(
                    f"✅ 上传完成!\n📄 {filename}\n📏 {format_size(actual_size)}\n"
                    f"📂 {type_folder}/\n📤 {get_mode_label(mode)}\n⏱️ {elapsed:.0f}秒"
                )
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
                await msg.edit_text(f"❌ 上传失败\n📄 {filename}\n原因: {result}")

        except Exception as e:
            logger.error(f"Error: {e}")
            await msg.edit_text(f"❌ 错误: {str(e)[:200]}")
        finally:
            cancel_flags.pop(task_id, None)
            if local_path and os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except Exception:
                    pass
            # 上传结束后清理临时文件
            await cleanup_temp_files()


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
    mode = get_mode(update.effective_user.id)

    # crypt 模式直接上传，direct 模式也直接上传（用已选路径）
    keyboard = [[
        InlineKeyboardButton("🔒 混淆上传", callback_data="upload_crypt"),
        InlineKeyboardButton("📤 直接上传", callback_data="upload_direct"),
    ]]

    # 显示当前上传路径
    if mode == "direct":
        dest = user_folder.get(update.effective_user.id, "默认")
        dest_display = f"\n📂 目标: {dest}"
    else:
        dest_display = ""

    pending_files[update.message.message_id] = {
        "file_id": doc.file_id,
        "file_type": file_type,
        "file_size": file_size,
        "filename": filename,
        "chat_id": update.message.chat_id,
        "msg_id": update.message.message_id,
        "ts": time.time(),
    }

    await update.message.reply_text(
        f"📄 **{filename}**\n📏 {format_size(file_size)}\n📁 {type_folder}{dest_display}\n\n选择上传方式：",
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
        await query.edit_message_text("❌ 未找到文件信息，请重新发送")
        return

    file_id = file_info["file_id"]
    file_type = file_info["file_type"]
    file_size = file_info["file_size"]
    filename = file_info["filename"]
    type_folder = get_type_folder(file_type)

    asyncio.create_task(_background_upload(
        context, query, file_info, file_id, file_size, file_type, filename, type_folder, mode
    ))


# ============================================================
#  命令
# ============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    current_folder = user_folder.get(update.effective_user.id, "默认")
    await update.message.reply_text(
        "🤖 TG Super Bot v4.0\n\n"
        "发送文件即可上传到 AList\n\n"
        "命令（直接发送中文即可）：\n"
        "帮助 - 显示帮助\n"
        "文件夹 - 选择上传文件夹\n"
        "自动删除 - 开关自动删除消息\n"
        "取消 - 取消当前上传\n"
        "状态 - 检查连接状态\n"
        f"\n📂 当前上传路径: {current_folder}"
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理中文关键词命令"""
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    text = update.message.text.strip()
    if text in ("帮助", "help"):
        await cmd_start(update, context)
    elif text in ("文件夹", "文件夹选择"):
        await cmd_folder(update, context)
    elif text in ("自动删除", "自动清理"):
        await cmd_autodel(update, context)
    elif text in ("取消", "取消上传"):
        await cmd_cancel(update, context)
    elif text in ("状态", "ping"):
        await cmd_ping(update, context)


async def cmd_autodel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    uid = update.effective_user.id
    user_autodel[uid] = not user_autodel.get(uid, False)
    status = "✅ 已开启" if user_autodel[uid] else "❌ 已关闭"
    await update.message.reply_text(f"🗑️ 自动删除消息：{status}")

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    uid = update.effective_user.id
    cancelled = 0
    for task_id in list(cancel_flags.keys()):
        if task_id.startswith(f"{uid}_"):
            cancel_flags[task_id] = True
            cancelled += 1
    if cancelled:
        await update.message.reply_text(f"⏹️ 已取消 {cancelled} 个上传任务")
    else:
        await update.message.reply_text("没有正在运行的上传任务")

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    mode = get_mode(update.effective_user.id)
    autodel = "✅ 开" if user_autodel.get(update.effective_user.id, False) else "❌ 关"
    current_folder = user_folder.get(update.effective_user.id, "默认")

    statuses = []
    for name, url in [("Crypt", ALIST_WEBDAV_CRYPT), ("Direct", ALIST_WEBDAV_DIRECT)]:
        if not url: continue
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    "PROPFIND", url,
                    auth=aiohttp.BasicAuth(ALIST_USER, ALIST_PASS),
                    headers={"Depth": "0"},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    statuses.append(f"{'✅' if resp.status in [200, 207] else '❌'} {name}")
        except:
            statuses.append(f"❌ {name}")

    active = sum(1 for t in cancel_flags if not cancel_flags[t])
    await update.message.reply_text(
        f"🏓 Pong!\n{' | '.join(statuses)}\n"
        f"📤 模式: {get_mode_label(mode)}\n"
        f"📂 上传路径: {current_folder}\n"
        f"🗑️ 自动删除: {autodel}\n"
        f"⚡ 活跃任务: {active}/{CONCURRENT_UPLOADS}"
    )


# ============================================================
#  定时清理
# ============================================================

async def cleanup_temp_files():
    """清理 telegram-bot-api 临时文件"""
    base = "/var/lib/telegram-bot-api"
    cleaned = 0
    try:
        for root, dirs, files in os.walk(base):
            for f in files:
                if f.endswith('.binlog'):
                    continue
                fp = os.path.join(root, f)
                try:
                    age_hours = (datetime.now().timestamp() - os.path.getmtime(fp)) / 3600
                    if age_hours > 0.5:
                        os.remove(fp)
                        cleaned += 1
                except:
                    pass
    except Exception as e:
        logger.warning(f"Cleanup error: {e}")
    if cleaned:
        logger.info(f"Cleanup: removed {cleaned} stale files")

    # 清理过期 pending_files（超过 30 分钟）
    now = time.time()
    expired = [k for k, v in pending_files.items() if now - v.get("ts", 0) > 1800]
    for k in expired:
        pending_files.pop(k, None)
    if expired:
        logger.info(f"Cleanup: removed {len(expired)} expired pending files")


# ============================================================
#  启动
# ============================================================

async def check_alist():
    for name, url in [("Crypt", ALIST_WEBDAV_CRYPT), ("Direct", ALIST_WEBDAV_DIRECT)]:
        if not url: continue
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    "PROPFIND", url,
                    auth=aiohttp.BasicAuth(ALIST_USER, ALIST_PASS),
                    headers={"Depth": "0"},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status in [200, 207]:
                        logger.info(f"✅ {name} 连接正常")
                    else:
                        logger.warning(f"⚠️ {name} 返回 {resp.status}")
        except Exception as e:
            logger.error(f"❌ {name} 连接失败: {e}")

    # 测试 AList API
    token = await alist_login()
    if token:
        logger.info("✅ AList API 登录成功")
    else:
        logger.error("❌ AList API 登录失败")


if __name__ == '__main__':
    asyncio.get_event_loop().run_until_complete(check_alist())

    app = ApplicationBuilder().token(BOT_TOKEN).base_url(API_SERVER_URL) \
        .read_timeout(3600).write_timeout(300).connect_timeout(60).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(callback_folder, pattern="^folder"))
    app.add_handler(CallbackQueryHandler(callback_upload, pattern="^upload_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.PHOTO,
        handle_file
    ))

    logger.info(f"🤖 Super Bot v4.0 is running... (concurrency={CONCURRENT_UPLOADS})")
    app.run_polling()
