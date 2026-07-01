@echo off
chcp 65001 >nul 2>&1
echo === Smart Lighting News Pipeline ===
echo.
echo Commands:
echo   python run_pipeline.py crawl
echo   python run_pipeline.py export
echo   python run_pipeline.py generate
echo   python run_pipeline.py all
echo.
C:\Users\shzhangzhongze\AppData\Local\Programs\Python\Python313\python.exe D:\NOTES\zzz\BriefNexus\scripts\run_pipeline.py %1
echo.
if "%1"=="" pause
