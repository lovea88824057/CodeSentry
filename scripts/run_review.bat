@echo off
setlocal
title CodeSentry - AI code review

REM ============================================================
REM  CodeSentry 一键代码审查
REM
REM  用法：
REM    1) 直接双击                    - 审查本项目当前的未提交改动
REM    2) 把仓库文件夹拖到本文件上    - 审查那个仓库
REM    3) 命令行传参（原样转发）      - 例如
REM         run_review.bat D:\work\myrepo --base main
REM         run_review.bat --diff-file changes.diff
REM
REM  报告位置：<被审查仓库>\.codesentry\review.md
REM ============================================================

set "SCRIPT_DIR=%~dp0"
REM 用 pushd/popd 做一次路径规范化：%~dp0 带反斜杠，直接拼 ".." 会得到
REM "D:\Git\CodeSentry\scripts\.." 这种没被折叠的路径（goes 到子目录时
REM 报告路径会显示得很奇怪，且相对路径判断容易出错）。
pushd "%SCRIPT_DIR%.."
set "PROJECT_DIR=%CD%"
popd
cd /d "%PROJECT_DIR%"

REM 第一个参数如果是选项（以 - 开头），说明用户没给仓库路径，就用当前目录
set "TARGET=%~1"
if "%TARGET%"=="" set "TARGET=%CD%"
if "%TARGET:~0,1%"=="-" set "TARGET=%CD%"

set "ARGS=%*"
if "%ARGS%"=="" set ARGS="%TARGET%"

set "OUTDIR=%TARGET%\.codesentry"
set "OUTFILE=%OUTDIR%\review.md"

REM GBK 输出，避免中文在 cmd 下乱码；:replace 保证遇到 GBK 装不下的
REM 字符（例如报告里的 emoji）也不会崩
set "PYTHONIOENCODING=gbk:replace"
set "PYTHONUTF8=0"

echo.
echo ============================================================
echo   CodeSentry - AI code review
echo ============================================================
echo   repo   : %TARGET%
echo   report : %OUTFILE%
echo.

REM ---------- 1) 优先用项目自带的 venv ----------
set "PYEXE="

set "CAND_VENV=%PROJECT_DIR%\.venv\Scripts\python.exe"
if exist "%CAND_VENV%" set PYEXE="%CAND_VENV%"
if defined PYEXE goto :py_found

REM ---------- 2) 兜底：用户级 Python 安装位置 ----------
REM  用 %USERPROFILE% / %LOCALAPPDATA% 拼路径，避免硬编码某个用户名
for %%D in ("%LOCALAPPDATA%\Programs\Python") do (
  if exist "%%~D" for /d %%V in ("%%~D\Python3*") do (
    if not defined PYEXE if exist "%%~V\python.exe" set PYEXE="%%~V\python.exe"
  )
)
if defined PYEXE goto :py_found

for %%D in ("%USERPROFILE%\anaconda3" "%USERPROFILE%\miniconda3" "%ProgramData%\anaconda3") do (
  if not defined PYEXE if exist "%%~D\python.exe" set PYEXE="%%~D\python.exe"
)
if defined PYEXE goto :py_found

REM ---------- 3) 再兜底：PATH 里的 python / py ----------
where python >nul 2>nul
if %errorlevel%==0 set PYEXE=python
if defined PYEXE goto :py_found

where py >nul 2>nul
if %errorlevel%==0 set PYEXE=py
if defined PYEXE goto :py_found

echo [ERROR] 没有找到 Python 解释器。
echo.
echo   已尝试:
echo     %CAND_VENV%
echo     %LOCALAPPDATA%\Programs\Python\Python3*
echo     %USERPROFILE%\anaconda3
echo     PATH 中的 python / py
echo.
echo   解决办法:
echo     python -m venv .venv
echo     .venv\Scripts\python.exe -m pip install -e .
echo.
goto :end

:py_found
echo   python : %PYEXE%
echo.
echo   正在审查，请稍候...
echo.

if not exist "%OUTDIR%" mkdir "%OUTDIR%"

%PYEXE% -m codesentry.cli review %ARGS% --output "%OUTFILE%" --no-color

if %errorlevel%==0 goto :ok
if %errorlevel%==2 goto :nochange

echo.
echo ------------------------------------------------------------
echo   运行失败 (exit code %errorlevel%)
echo ------------------------------------------------------------
echo.
echo   常见原因:
echo     * 没有配置 API Key  -^> 在 .env 里写 DEEPSEEK_API_KEY=...
echo     * 目标不是 git 仓库 -^> 传入仓库路径，或用 --diff-file
echo     * 网络受限          -^> 检查代理，或换一个模型
echo.
goto :end

:nochange
echo.
echo ------------------------------------------------------------
echo   没有检测到改动，无需审查。
echo ------------------------------------------------------------
echo.
goto :end

:ok
echo.
echo ------------------------------------------------------------
echo   完成
echo.
echo   报告: %OUTFILE%
echo ------------------------------------------------------------
echo.
echo   提示: 想额外产出机器可读结果，加 --json-output ^<path^>。
echo.

:end
if not defined CODESENTRY_NO_PAUSE pause
endlocal
