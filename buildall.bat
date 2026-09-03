@echo off
chcp 936 >nul
rem ============================================
rem  鲸鲸 普通版（登录界面）一键打包
rem  产物：dist\鲸鲸.exe
rem  用法：双击运行，或命令行 buildall.bat
rem ============================================
cd /d %~dp0

echo [1/3] 开始打包登录版...
pyinstaller main.spec --noconfirm --clean --distpath dist
if errorlevel 1 (
    echo.
    echo 打包失败，请检查上方错误信息
    pause
    exit /b 1
)

echo [2/3] 打包完成，运行 smoke 自检...
set "SMOKE_FILE=%TEMP%\jingjing_smoke_buildall.txt"
if exist "%SMOKE_FILE%" del "%SMOKE_FILE%"
start "" /wait "dist\鲸鲸.exe" --smoke "%SMOKE_FILE%"
if exist "%SMOKE_FILE%" type "%SMOKE_FILE%"

echo [3/3] 完成
echo 产物：dist\鲸鲸.exe
pause