# 🐳 鲸鲸 – 你的 Windows AI 桌宠

> 一只住在你电脑里的鲸鱼少女，陪你聊天、帮你搜东西、控制电脑，还会傲娇和撒娇。

![Python](https://img.shields.io/badge/Python-3.12-blue) ![PySide6](https://img.shields.io/badge/PySide6-6.6+-green) [![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE) ![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)

---

## ✨ 功能特色

- 🗣️ **AI 聊天**：基于 DeepSeek API，支持文本与图片识别，自带情绪感知（立绘会随着心情变化）。
- 🔍 **联网搜索**：说一句“搜一下”，鲸鲸会自动打开必应搜索并为你总结结果。
- 🖥️ **电脑控制**：
  - **打开/关闭软件**：支持注册表与开始菜单扫描，找不到时还能用 AI 语义匹配。
  - **系统控制**：音量调节、静音、锁屏、关机/重启、媒体播放控制。
  - **文件创建**：在指定盘符（非系统盘）新建文件，路径多重安全校验。
- ⏰ **定时提醒**：支持“10分钟后提醒我喝水”等自然语言指令。
- 🎨 **迷你模式**：鼠标中键/双击立绘可切换为 150px 小图标，驻留右下角不占空间。
- 💾 **本地优先**：聊天记录保存在用户目录，API Key 每次手动输入（登录版）或通过 `APIkey.txt` 配置（个人版）。
- 🚀 **双版本打包**：
  - **登录版**：每次启动输入 API Key，适合多用户或共享电脑。
  - **个人版**：从同级 `APIkey.txt` 读取密钥，换 key 无需重打包，适合自用。

---



## 🚀 快速开始

### 1. 获取 API Key

前往 [DeepSeek 平台](https://platform.deepseek.com) 注册并创建 API Key（格式：`sk-xxxxxxxx`）。

### 2. 下载与运行

#### 方式一：使用打包好的 exe（推荐）

从[Releases](https://github.com/kangjunhuihui/jingpet/releases) 下载最新版本：

- **登录版**：`鲸鲸.exe`，每次启动输入 API Key。
- **个人版**：`user/鲸鲸.exe` + `APIkey.txt` + `start_jingjing.vbs`。  
  将 `APIkey.txt` 放在 exe 同级目录，里面只写你的密钥，双击 exe 即可直接进入聊天。

#### 方式二：从源码运行

```bash
git clone git clone https://github.com/kangjunhuihui/jingpet.git
cd jingjing
pip install -r requirements.txt
python app.py          # 登录版
# 或
python app_personal.py # 个人版（需要同级有 APIkey.txt）
