@echo off
chcp 65001 >nul
echo [1/2] 파이썬 라이브러리 설치 확인 중...
pip install selenium webdriver-manager

echo.
echo [2/2] 크롤링 스크립트를 실행합니다...
python auto_list.py

pause