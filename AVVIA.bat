@echo off
cd /d "%~dp0"
if not exist .env (
    echo CREA .env da .env.example e imposta OPENAI_API_KEY
    pause
    exit /b 1
)
echo.
echo ============================================
echo G1 Talk Module - Avvio
echo ============================================
echo Apri nel browser: http://localhost:8081
echo ============================================
echo.
python -m talk_module.web_app --host 0.0.0.0 --port 8081
pause
