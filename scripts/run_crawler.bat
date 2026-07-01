@echo off
chcp 65001 >nul 2>&1
echo === Smart Lighting News Crawler v5 (LLM Classification) ===
echo.
C:\Users\shzhangzhongze\AppData\Local\Programs\Python\Python313\python.exe D:\NOTES\zzz\BriefNexus\scripts\news_crawler.py --max-age 7
echo.
echo Done!
pause
