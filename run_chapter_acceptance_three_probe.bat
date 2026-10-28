@echo off
setlocal
cd /d "%~dp0"

echo Starting isolated three-chapter acceptance validation...
echo Story ID: chapter_acceptance_three_ch_probe_20261016
echo.

py -3 engine.py --story-id chapter_acceptance_three_ch_probe_20261016 --direction-file tmp\control_probe_direction_20261014.txt --chapters 3 --words 1600 --controlled-generation --control-dir shorts\chapter_acceptance_three_ch_probe_20261016\control
set "run_exit_code=%ERRORLEVEL%"

echo.
if not "%run_exit_code%"=="0" (
    echo Run ended with exit code %run_exit_code%. Read the output above before retrying.
) else (
    echo Run completed. Review shorts\chapter_acceptance_three_ch_probe_20261016 and out\chapter_acceptance_three_ch_probe_20261016.
)
pause
exit /b %run_exit_code%
