# 🤖 TG Super Bot

Telegram 文件上传机器人，支持流式上传、并发传输、混淆/直传双模式、文件夹选择。

## ✨ 功能

- **流式上传** — 边下载边上传，不占用磁盘空间
- **并发传输** — 同时处理多个文件（默认 3 个）
- **双模式上传** — 🔒混淆（Crypt 文件名加密）/ 📤直传（OneDrive 原始文件名）
- **文件夹选择** — `/folder` 浏览 AList 目录，设置默认上传路径
- **按钮选择** — 发送文件后直接弹出模式选择按钮
- **自动分类** — 直传模式自动按 文件类型（视频/音频/图片/文件）分类存放
- **进度显示** — 下载和上传双进度条
- **失败重试** — 流式失败自动回退磁盘模式，最多重试 3 次
- **取消上传** — `/cancel` 取消正在进行的上传
- **自动清理** — 定时清理临时文件和过期任务

## 📦 依赖

- [Telegram Bot API Server](https://github.com/tdlib/telegram-bot-api) — 本地部署，支持最大 2GB 文件
- [OpenList](https://github.com/OpenListTeam/OpenList) / [Alist](https://github.com/alist-org/alist) — WebDAV 网盘管理

## 🚀 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/ohh561/tg-super-bot.git
cd tg-super-bot
```

### 2. 配置环境变量

```bash
cp .env.example .env
vim .env
```

### 3. 启动服务

```bash
docker-compose up -d
```

## ⚙️ 配置说明

| 变量 | 说明 | 示例 |
|------|------|------|
| `BOT_TOKEN` | Telegram Bot Token | `123456:ABC-DEF...` |
| `ALLOWED_USER_ID` | 允许使用的用户 ID | `123456789` |
| `ALIST_WEBDAV_CRYPT` | 混淆模式 WebDAV 地址 | `http://openlist:5244/dav/PrivateVideo/` |
| `ALIST_WEBDAV_DIRECT` | 直传模式 WebDAV 地址 | `http://openlist:5244/dav/Onedrive/` |
| `ALIST_API_URL` | AList API 地址（用于文件夹浏览） | `http://openlist:5244` |
| `ALIST_USER` | Alist/OpenList 用户名 | `admin` |
| `ALIST_PASS` | Alist/OpenList 密码 | `password` |

## 📁 目录结构

**混淆模式（Crypt）：**
```
PrivateVideo/
└── YYYY-MM-DD/
    └── 文件名加密.mp4
```

**直传模式（OneDrive）：**
```
OneDrive/telegram/
├── 视频/YYYY-MM-DD/
├── 音频/YYYY-MM-DD/
├── 图片/YYYY-MM-DD/
└── 文件/YYYY-MM-DD/
```

> 💡 使用 `/folder` 命令可以自定义直传模式的目标文件夹

## 🎮 命令

| 命令 | 说明 |
|------|------|
| `/start` | 显示帮助 |
| `/wenjianjia` | 选择上传文件夹（浏览 AList 目录） |
| `/zidongshanchu` | 开关自动删除消息 |
| `/quxiao` | 取消当前上传 |
| `/zhuangtai` | 检查连接状态 |

## 🔧 自定义

修改 `bot.py` 中的配置：

```python
MAX_RETRIES = 3                          # 重试次数
CONCURRENT_UPLOADS = 3                   # 并发上传数
STREAM_CHUNK_SIZE = 4 * 1024 * 1024      # 流式块大小
```

## 📄 License

MIT
