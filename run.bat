@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
title multiagent 启动菜单

set "PY=.venv\Scripts\python.exe"

REM ---- 1. 检查环境（首次运行自动创建并安装依赖） ----
if not exist "%PY%" (
    echo [初始化] 未找到 .venv，正在创建虚拟环境...
    where python >nul 2>nul
    if errorlevel 1 (
        echo [错误] 未找到 python 命令。请先安装 Python 3.12（安装时勾选 "Add to PATH"）。
        pause
        exit /b 1
    )
    python -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败。
        pause
        exit /b 1
    )
    echo [初始化] 正在安装依赖（首次进项目较慢，请稍候）...
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败。
        pause
        exit /b 1
    )
)

REM ---- 2. .env（首次运行自动从示例生成） ----
if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    echo [初始化] 已从 .env.example 复制 .env。
)
findstr /R /C:"_API_KEY=..*" ".env" >nul
if errorlevel 1 (
    echo.
    echo [警告] .env 里还没有填写任何 API Key，对话将无法使用。
    echo        可选 [5] 编辑 .env，填入至少一个厂商的 Key。
    echo.
)

:menu
echo ==========================================
echo   multiagent 启动菜单
echo ==========================================
echo   [1] WebUI + HTTP API（新窗口启动服务并自动打开浏览器）
echo   [2] 终端对话 CLI
echo   [3] 运行测试
echo   [4] 安装 / 更新依赖
echo   [5] 编辑 .env（重新编辑）
echo   [0] 退出
echo.
set /p "choice=请选择: "

if "%choice%"=="1" goto webui
if "%choice%"=="2" goto cli
if "%choice%"=="3" goto tests
if "%choice%"=="4" goto deps
if "%choice%"=="5" goto edit_env
if "%choice%"=="0" exit /b 0
echo 无效选项，请重新输入。
echo.
goto menu

:webui
echo.
REM ---- 端口取自 .env 的 API_PORT（缺省 8000） ----
set "API_PORT=8000"
for /f "tokens=1,* delims==" %%a in ('findstr /B /C:"API_PORT=" .env') do set "API_PORT=%%b"

REM ---- 端口空闲：直接启动 ----
netstat -ano | findstr /C:":%API_PORT% " | findstr "LISTENING" >nul
if errorlevel 1 goto start_server

REM ---- 端口被占用：通过 /health 的身份标识确认占用者是不是本服务 ----
curl -s -m 2 --noproxy "*" http://127.0.0.1:%API_PORT%/health 2>nul | findstr /C:"multiagent" >nul
if errorlevel 1 (
    echo [提示] 端口 %API_PORT% 已被其他程序占用（不是本服务，已避免误杀）。
    echo        如需换端口，请编辑 .env 里的 API_PORT 后重试。
    echo        查看占用进程：netstat -ano ^| findstr :%API_PORT%
    echo.
    pause
    goto menu
)

REM ---- 是本服务的旧实例：先杀掉再重启 ----
echo [提示] 检测到旧的 multiagent 服务正在端口 %API_PORT% 运行，自动关闭...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /C:":%API_PORT% " ^| findstr "LISTENING"') do taskkill /F /T /PID %%p >nul 2>nul
timeout /t 1 /nobreak >nul

:start_server
echo 正在启动 WebUI + HTTP API（127.0.0.1:%API_PORT%）...
start "multiagent server" cmd /k ".venv\Scripts\python.exe -m app.server"
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:%API_PORT%
echo 服务已在新窗口启动，浏览器已打开；关闭该窗口即停止服务。
echo.
pause
goto menu

:cli
echo.
"%PY%" -m app.cli
echo.
pause
goto menu

:tests
echo.
"%PY%" -m unittest discover tests -v
echo.
pause
goto menu

:deps
echo.
"%PY%" -m pip install -r requirements.txt
echo.
pause
goto menu

:edit_env
start notepad .env
goto menu
