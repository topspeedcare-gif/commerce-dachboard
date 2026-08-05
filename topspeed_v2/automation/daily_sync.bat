@echo off
REM automation/daily_sync.bat -- registered in Windows Task Scheduler.
REM Put this file's full path in the Task Scheduler "Program/script" field.
REM
REM NOTE: keep this file ASCII-only. Non-ASCII (Korean) text in REM comments
REM was found to get misread by cmd.exe when the console codepage doesn't
REM match the file's UTF-8 encoding, which can corrupt bytes into stray
REM command separators and make cmd try to run fragments of the comment as
REM commands (observed 2026-08-05). See Daily_sync.py's log() function for
REM the full (Korean) explanation of why this file no longer redirects
REM stdout to sync_log.txt -- log() already writes that file itself, and
REM having both write to the same file at once caused daily sync to fail
REM with a PermissionError every single day from 2026-07-26 to 2026-08-05.

cd /d "%~dp0.."
python automation\Daily_sync.py
