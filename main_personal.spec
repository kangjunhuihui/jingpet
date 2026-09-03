# -*- mode: python ; coding: utf-8 -*-
# main_personal.spec — 鲸鲸个人版（免登录）打包配置（onefile 单文件 + 无控制台窗口）
# 构建：buildpersonal.bat 或 pyinstaller main_personal.spec --noconfirm --clean --distpath user
# 入口：app_personal.py（免登录，读同级 APIkey.txt）；与登录版 main.spec 共存，互不影响

a = Analysis(
    ['app_personal.py'],
    pathex=[],
    binaries=[],
    datas=[('Assets', 'Assets')],   # 立绘 + 内置 msedgedriver 打进包内（resource_path 已兼容 _MEIPASS）
    # selenium 的 webdriver.Edge/ChromeOptions 等是惰性导入（__getattr__ 动态 import），
    # PyInstaller 静态分析发现不了，必须显式声明，否则打包后搜索报
    # "No module named 'selenium.webdriver.edge.webdriver'"
    hiddenimports=[
        'selenium.webdriver.edge.webdriver',
        'selenium.webdriver.edge.service',
        'selenium.webdriver.edge.options',
        'selenium.webdriver.chrome.webdriver',
        'selenium.webdriver.chrome.service',
        'selenium.webdriver.chrome.options',
        'selenium.webdriver.firefox.webdriver',
        'selenium.webdriver.firefox.service',
        'selenium.webdriver.firefox.options',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='鲸鲸',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                  # GUI 程序，不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico',                # 程序图标
    version='version_info.txt',     # Windows 版本信息
)
