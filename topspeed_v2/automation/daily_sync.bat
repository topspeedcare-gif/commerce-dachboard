@echo off
REM automation/daily_sync.bat — Windows 작업 스케줄러 등록용
REM 작업 스케줄러 "프로그램/스크립트" 칸에 이 파일의 전체 경로를 넣으면 됩니다.

cd /d "%~dp0.."
python automation\Daily_sync.py >> automation\sync_log.txt 2>&1
