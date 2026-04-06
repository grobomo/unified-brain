@echo off
REM Launch unified-brain service. Used by scheduled task and manual start.
cd /d "%~dp0\.."
set PYTHONPATH=src
python -m unified_brain --config config/brain.yaml --health-port 8790 %*
