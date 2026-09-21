@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
title multiagent 快速启动

set "PY=.venv\Scripts\python.exe"

REM ---- 1. 虚拟环境（首次运行自动创建并装依赖） ----
if not exist "%PY%" (
    echo [初始化] 未找到 .venv，开始创建虚拟环境...
    where python >nul 2>nul
    if errorlevel 1 (
        echo [错误] 未找到 python 命令。请先安装 Python 3.12，安装时勾选 "Add to PATH"。
        pause
        exit /b 1
    )
    python -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败。
        pause
        exit /b 1
    )
    echo [初始化] 安装依赖（首次较慢，请稍候）...
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
    echo [初始化] 已从 .env.example 生成 .env。
)
findstr /R /C:"_API_KEY=..*" ".env" >nul
if errorlevel 1 (
    echo.
    echo [提醒] .env 里还没有填写任何 API Key，对话无法发起。
    echo        请选 [5] 编辑 .env，至少填一个厂商的 Key。
    echo.
)

:menu
echo ==========================================
echo   multiagent 快速启动
echo ==========================================
echo   [1] WebUI + HTTP API（新窗口启动，自动开浏览器）
echo   [2] 终端对话 CLI
echo   [3] 运行测试
echo   [4] 安装 / 更新依赖
echo   [5] 编辑 .env（记事本）
echo   [0] 退出
echo.
set /p "choice=输入选项: "

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
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo [提示] 端口 8000 已被占用，可能有一个旧的服务窗口还开着。
    echo        本次直接为你打开页面；若要重启服务，请先关闭占用端口的旧窗口。
    echo        查看占用进程：netstat -ano ^| findstr :8000
    start "" http://127.0.0.1:8000
    echo.
    pause
    goto menu
)
echo 正在启动 WebUI + HTTP API（127.0.0.1:8000）...
start "multiagent server" cmd /k ".venv\Scripts\python.exe -m app.server"
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:8000
echo 服务已在新窗口启动，浏览器已打开；关闭那个窗口即停止服务。
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
