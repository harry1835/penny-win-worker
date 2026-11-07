@echo off
REM ============================================================================
REM start_worker.bat — Start Penny Windows CAD/CAE HTTP Worker
REM ============================================================================
cd /d "%~dp0\.."

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
)

echo Starting Penny Windows Worker (SAP2000 OAPI + AutoCAD COM)...
python worker_server.py --host 0.0.0.0 --port 8085
