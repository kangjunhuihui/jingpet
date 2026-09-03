@echo off
chcp 936 >nul
rem ============================================
rem  鲸鲸 个人版（免登录）一键打包
rem  产物：user\鲸鲸.exe + APIkey.txt + start_jingjing.vbs
rem  用法：双击运行，或命令行 buildpersonal.bat
rem ============================================
cd /d %~dp0

rem 前置检查：APIkey.txt 必须存在
if not exist "APIkey.txt" (
    echo.
    echo [错误] 根目录缺少 APIkey.txt，个人版打包需要它！
    pause
    exit /b 1
)

echo [1/4] 开始打包个人版（输出到 user 文件夹）...
if not exist "user" mkdir "user"
pyinstaller main_personal.spec --noconfirm --clean --distpath user
if errorlevel 1 (
    echo.
    echo 打包失败，请检查上方错误信息
    pause
    exit /b 1
)

echo [2/4] 组装三件套...
copy /y "APIkey.txt" "user\APIkey.txt" >nul
if not exist "user\start_jingjing.vbs" copy /y "start_jingjing.vbs" "user\start_jingjing.vbs" >nul

echo [3/4] 运行 smoke 自检...
set "SMOKE_FILE=%TEMP%\jingjing_smoke_buildpersonal.txt"
if exist "%SMOKE_FILE%" del "%SMOKE_FILE%"
start "" /wait "user\鲸鲸.exe" --smoke "%SMOKE_FILE%"
if exist "%SMOKE_FILE%" type "%SMOKE_FILE%"

echo [4/4] 完成
echo 产物：user\鲸鲸.exe + APIkey.txt + start_jingjing.vbs
pause