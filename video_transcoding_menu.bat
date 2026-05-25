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
echo Checking for cloudflared...
where cloudflared >nul 2>nul
if errorlevel 1 (
    echo cloudflared is not installed or not in PATH. Skipping TryCloudflare step.
    echo To install:  winget install Cloudflare.cloudflared
    pause
    goto :menu
)

set "CF_LOG=%ROOT%cloudflared.log"
if exist "%CF_LOG%" del /f /q "%CF_LOG%" >nul 2>nul

echo.
echo Starting Cloudflare Tunnel in a new window...
start "Cloudflare Tunnel" powershell -NoExit -Command "& cloudflared tunnel --url http://localhost:8080 2>&1 | ForEach-Object { Add-Content -Path '%CF_LOG%' -Value $_; Write-Host $_ }"

echo.
echo Waiting for TryCloudflare URL (up to 60 seconds)...
powershell -NoProfile -Command "$log='%CF_LOG%'; $url=$null; for ($i=0; $i -lt 60; $i++) { if (Test-Path $log) { $m = Select-String -Path $log -Pattern 'https://[a-z0-9.-]+\.trycloudflare\.com' -ErrorAction SilentlyContinue | Select-Object -First 1; if ($m) { $url = $m.Matches[0].Value; break } } Start-Sleep -Seconds 1 }; if ($url) { Write-Host ''; Write-Host ('TryCloudflare URL: ' + $url) -ForegroundColor Green} else { Write-Host 'Could not detect TryCloudflare URL within 60s. Please check the Cloudflare Tunnel window.' -ForegroundColor Yellow }"

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
