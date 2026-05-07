# 🌉 TG Cloud Bridge

Telegram 文件自动转存到云存储的机器人。通过 AList/OpenList 将 Telegram 文件上传到 OneDrive、Google Drive 等网盘，支持生成公网下载链接。

A Telegram bot that automatically transfers files to cloud storage via AList/OpenList. Supports OneDrive, Google Drive, and more — with public share link generation.

## ✨ 功能 / Features

- **双模式上传 / Dual Upload Modes** — 🔒混淆（Crypt 文件名加密）/ 📤直传（OneDrive 原始文件名）
- **按钮选择 / Inline Buttons** — 发送文件后弹出模式选择按钮
- **🔗 下载链接 / Share Links** — 上传完成后一键生成公网分享链接
- **并发传输 / Concurrent Uploads** — 同时处理多个文件（默认 3 个）
- **自动分类 / Auto Categorize** — 按文件类型（视频/音频/图片/文件）自动归类
- **进度显示 / Progress Bar** — 下载和上传双进度条
- **失败重试 / Retry on Failure** — 上传失败保留文件，点击按钮重试
- **文件夹选择 / Folder Browse** — `/folder` 浏览 AList 目录，设置默认上传路径
- **取消上传 / Cancel Upload** — `/cancel` 取消正在进行的上传
- **自动删除 / Auto Delete** — `/autodel` 上传成功后自动删除原始消息
- **自动清理 / Auto Cleanup** — 定时清理临时文件和过期任务

## 🏗️ 架构 / Architecture

```
用户发送文件
    ↓
Telegram 云端
    ↓ (Bot API)
telegram-bot-api（本地服务器，支持 2GB 大文件）
    ↓ (文件落盘)
tg-cloud-bridge（读取本地文件）
    ↓ (WebDAV)
AList / OpenList
    ↓ (302 重定向 / Proxy)
OneDrive / Google Drive / 其他网盘
```

> 💡 为什么需要本地 telegram-bot-api？
> Telegram 官方 Bot API 限制文件大小 20MB。本地部署的 telegram-bot-api 使用 `--local` 模式，文件先下载到磁盘再处理，支持最大 2GB。
>
> Why a local telegram-bot-api? The official Telegram Bot API limits file size to 20MB. The local server with `--local` mode saves files to disk first, supporting up to 2GB.

## 🚀 快速开始 / Quick Start

### 第一步：创建 Telegram Bot / Step 1: Create Telegram Bot

1. 打开 Telegram，搜索 [@BotFather](https://t.me/BotFather)
2. 发送 `/newbot`
3. 按提示设置 bot 显示名称（如 `TG Cloud Bridge`）
4. 按提示设置 bot 用户名（如 `tg_cloud_bridge_bot`，必须以 `bot` 结尾）
5. BotFather 会回复一个 **Bot Token**，格式如 `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`
6. **保存这个 Token**，后面要用

> Open Telegram, find [@BotFather](https://t.me/BotFather), send `/newbot`, follow the prompts. Save the **Bot Token**.

### 第二步：获取你的 User ID / Step 2: Get Your User ID

1. 搜索 [@userinfobot](https://t.me/userinfobot)
2. 发送任意消息
3. 它会回复你的 **User ID**（纯数字，如 `123456789`）
4. **保存这个 ID**，这是允许使用 bot 的唯一用户

> Find [@userinfobot](https://t.me/userinfobot), send any message. Save your **User ID**.

### 第三步：获取 Telegram API 凭据 / Step 3: Get Telegram API Credentials

本地 telegram-bot-api 服务器需要 API ID 和 API Hash（这不是 Bot Token，是另一套凭据）：

1. 打开 [my.telegram.org](https://my.telegram.org)
2. 用你的手机号登录（会收到验证码）
3. 点击 **API development tools**
4. 填写 App title（随意，如 `TG Cloud Bridge`）和 Short name（随意，如 `tgcb`）
5. 点 **Create application**
6. 页面会显示 **App api_id**（数字）和 **App api_hash**（字符串）
7. **保存这两个值**

> Visit [my.telegram.org](https://my.telegram.org), log in with your phone number, go to **API development tools**, create an app. Save **api_id** and **api_hash**.

### 第四步：准备 AList / OpenList / Step 4: Set Up AList/OpenList

你需要一个运行中的 AList 或 OpenList 实例，并配置好至少一个存储驱动（如 OneDrive、Google Drive）。

You need a running AList/OpenList instance with at least one storage driver (e.g., OneDrive, Google Drive).

安装指南 / Install guides:
- [OpenList](https://github.com/OpenListTeam/OpenList#readme)（推荐 / Recommended）
- [AList](https://github.com/alist-org/alist#readme)

记录以下信息 / Note down:
- **API 地址** — 如 `http://your-server:5244`（docker 内部通信用容器名，如 `http://openlist:5244`）
- **WebDAV 地址** — 如 `http://your-server:5244/dav/你的存储路径/`
- **用户名和密码**

### 第五步：配置 / Step 5: Configure

```bash
git clone https://github.com/ohh561/tg-cloud-bridge.git
cd tg-cloud-bridge
cp .env.example .env
vim .env
```

填写 .env / Fill in .env:

```env
# === 第一步获取 / From Step 1 ===
BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz

# === 第二步获取 / From Step 2 ===
ALLOWED_USER_ID=123456789

# === 第三步获取 / From Step 3 ===
TELEGRAM_API_ID=12345
TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890

# === 第四步获取 / From Step 4 ===
ALIST_WEBDAV_CRYPT=http://openlist:5244/dav/PrivateVideo/
ALIST_WEBDAV_DIRECT=http://openlist:5244/dav/Onedrive/
ALIST_API_URL=http://openlist:5244
ALIST_USER=admin
ALIST_PASS=your_password

# === 可选：公网地址（用于生成下载链接）===
# === Optional: public URL for share links ===
# PUBLIC_URL=https://openlist.example.com
```

> ⚠️ **重要：** `ALIST_WEBDAV_CRYPT` 和 `ALIST_WEBDAV_DIRECT` 的路径必须是 AList 中已存在的目录。如果还没有，先在 AList Web UI 中创建。
>
> **Important:** The WebDAV paths must exist in AList. Create them in the AList Web UI first if needed.

### 第六步：启动 / Step 6: Start

```bash
docker compose up -d
```

这会启动两个容器 / This starts two containers:
- **telegram-bot-api** — 本地 Telegram Bot API 服务器（接收文件）
- **tg-cloud-bridge** — 文件处理机器人（上传到网盘）

### 第七步：验证 / Step 7: Verify

1. 在 Telegram 中找到你的 bot（用第一步设置的用户名搜索）
2. 发送 `/start`，bot 应该回复帮助信息
3. 发送一个文件，bot 应该弹出上传模式按钮
4. 如果没有反应，检查日志：`docker logs tg-super-bot --tail 20`

> Find your bot on Telegram (search the username from Step 1). Send `/start`, then send a file. If no response, check logs: `docker logs tg-super-bot --tail 20`

## ⚙️ 配置说明 / Configuration Reference

| 变量 / Variable | 必填 / Required | 说明 / Description | 示例 / Example |
|------|------|------|------|
| `BOT_TOKEN` | ✅ | Telegram Bot Token | `123456:ABC-DEF...` |
| `ALLOWED_USER_ID` | ✅ | 允许使用的用户 ID / Allowed user ID | `123456789` |
| `TELEGRAM_API_ID` | ✅ | Telegram API ID（本地 Bot API 用） | `12345` |
| `TELEGRAM_API_HASH` | ✅ | Telegram API Hash（本地 Bot API 用） | `abcdef1234567890` |
| `ALIST_WEBDAV_CRYPT` | ✅ | 混淆模式 WebDAV 地址 / Crypt mode WebDAV URL | `http://openlist:5244/dav/PrivateVideo/` |
| `ALIST_WEBDAV_DIRECT` | ✅ | 直传模式 WebDAV 地址 / Direct mode WebDAV URL | `http://openlist:5244/dav/Onedrive/` |
| `ALIST_API_URL` | ✅ | AList API 地址 / AList API endpoint | `http://openlist:5244` |
| `ALIST_USER` | ✅ | 用户名 / Username | `admin` |
| `ALIST_PASS` | ✅ | 密码 / Password | `password` |
| `PUBLIC_URL` | ❌ | 公网地址（生成下载链接用）/ Public URL for share links | `https://openlist.example.com` |

## 📁 目录结构 / Directory Structure

**混淆模式 / Crypt Mode：**
```
PrivateVideo/
├── 视频/ (Videos)
├── 音频/ (Audio)
├── 图片/ (Images)
└── 文件/ (Files)
```

**直传模式 / Direct Mode：**
```
Onedrive/telegram/
├── 视频/ (Videos)
├── 音频/ (Audio)
├── 图片/ (Images)
└── 文件/ (Files)
```

> 💡 使用 `/folder` 命令可以自定义直传模式的目标文件夹
>
> Use `/folder` to customize the upload destination for direct mode

## 🎮 命令 / Commands

| 命令 / Command | 说明 / Description |
|------|------|
| `/start` | 显示帮助 / Show help |
| `/folder` | 选择上传文件夹 / Browse & select upload folder |
| `/autodel` | 开关自动删除 / Toggle auto-delete messages |
| `/cancel` | 取消当前上传 / Cancel ongoing uploads |
| `/status` | 检查连接状态 / Check connection status |
| `/retry` | 查看待重试文件 / List files pending retry |

## 🔗 下载链接 / Share Links

配置 `PUBLIC_URL` 后，上传完成会显示「🔗 获取下载链接」按钮：

With `PUBLIC_URL` configured, a share link button appears after upload:

1. 发送文件到 Bot / Send file to bot
2. 选择上传模式 / Choose upload mode
3. 等待上传完成 / Wait for upload
4. 点击「🔗 获取下载链接」/ Click share link button
5. 获取公网 URL / Get public download URL

分享链接特点 / Share link features:
- 永久有效 / Permanent (unless manually deleted)
- 无需密码 / No password required
- 直接下载 / Direct download, no login needed

## 🔧 自定义 / Customization

```python
MAX_RETRIES = 3              # 重试次数 / Retry attempts
CONCURRENT_UPLOADS = 3       # 并发上传数 / Concurrent uploads
```

## 🐛 故障排除 / Troubleshooting

**Bot 没有反应 / Bot not responding:**
```bash
docker logs tg-super-bot --tail 20
docker logs telegram-bot-api --tail 20
```

**telegram-bot-api 崩溃（Signal 6）/ telegram-bot-api crash:**
```bash
docker exec telegram-bot-api rm -f /var/lib/telegram-bot-api/*/td.binlog
docker restart telegram-bot-api
```

**大文件上传超时 / Large file upload timeout:**
确保 `telegram-bot-api` 使用 `--local` 模式（docker-compose.yml 中已配置 `TELEGRAM_LOCAL: "true"`）。
Ensure `telegram-bot-api` uses `--local` mode (already configured in docker-compose.yml).

## 📄 License

MIT
