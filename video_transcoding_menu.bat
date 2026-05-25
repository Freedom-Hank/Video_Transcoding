@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
set "SHARED=%ROOT%shared-volume"
set "UPLOADS=%SHARED%\uploads"
set "SEGMENTS=%SHARED%\segments"
set "OUTPUTS=%SHARED%\outputs"
set "STATE_FILE=%SHARED%\manager_state.json"

pushd "%ROOT%" >nul

docker compose version >nul 2>nul
if errorlevel 1 (
    echo Docker Compose plugin was not found.
    echo Please update Docker Desktop and make sure this command works:
    echo docker compose version
    echo.
    pause
    goto :end
)

:menu
cls
echo ==========================================
echo   Video Transcoding Manager
echo ==========================================
echo.
echo 1. Clean previous files, restart all containers, and open http://localhost:8080
echo 2. Clean previous files only
echo 3. Stop and remove all project containers
echo 0. Exit
echo.
set /p "choice=Enter your choice: "

if "%choice%"=="1" goto :clean_restart_open
if "%choice%"=="2" goto :clean_only
if "%choice%"=="3" goto :shutdown_containers
if "%choice%"=="0" goto :end

echo.
echo Invalid choice. Please try again.
pause
goto :menu

:clean_restart_open
call :clean_files
echo.
echo Restarting all containers...
docker compose up -d --build
if errorlevel 1 (
    echo.
    echo Failed to start containers. Please make sure Docker Desktop is running.
    pause
    goto :menu
)
echo.
echo Opening browser...
start "" "http://localhost:8080"
pause
goto :menu

:clean_only
call :clean_files
pause
goto :menu

:shutdown_containers
echo.
echo Stopping and removing project containers...
docker compose down --remove-orphans
if errorlevel 1 (
    echo.
    echo Failed to stop containers.
    pause
    goto :menu
)
echo.
echo Project containers were stopped and removed.
pause
goto :menu

:clean_files
echo.
echo Cleaning previous files...

if exist "%UPLOADS%" rmdir /s /q "%UPLOADS%"
if exist "%SEGMENTS%" rmdir /s /q "%SEGMENTS%"
if exist "%OUTPUTS%" rmdir /s /q "%OUTPUTS%"
if exist "%STATE_FILE%" del /f /q "%STATE_FILE%"

mkdir "%UPLOADS%" 2>nul
mkdir "%SEGMENTS%" 2>nul
mkdir "%OUTPUTS%" 2>nul

echo Cleaned uploads, segments, outputs, and manager_state.json.
exit /b 0

:end
popd >nul
endlocal
