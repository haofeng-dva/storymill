@echo off
setlocal
cd /d "%~dp0"
title Novel Pro - M/M Werewolf 12 Chapter Production

set "STORY_ID=mm_werewolf_12ch_20261016"
set "DIRECTION=tmp\mm_werewolf_direction_20261016.txt"
set "OUT_DIR=shorts\%STORY_ID%"
set "CONTROL_DIR=%OUT_DIR%\control_20261019"

rem A verified control bundle must be reusable: recompiling it would spend model
rem calls to reproduce artifacts that already passed schema and budget checks.
rem Reuse requires all five artifacts - a half-written control directory must
rem never be mistaken for a complete bundle.
set "CONTROL_OK=1"
for %%F in (chapter_contracts.json scene_ledger.json opening_contract.json character_voice_cards.json prose_budget.json) do (
  if not exist "%CONTROL_DIR%\%%F" set "CONTROL_OK=0"
)
if "%CONTROL_OK%"=="1" (
  echo [1/3] Verified control bundle found - reusing it.
  echo       Delete %CONTROL_DIR% to force a recompile.
  goto :controls_ready
)
if exist "%CONTROL_DIR%\chapter_contracts.json" echo [1/3] Control bundle is incomplete - recompiling.
echo [1/3] Compiling verified chapter controls (resumes valid cache)...
py -3 -u control_compiler.py --story-id %STORY_ID% --direction-file %DIRECTION% --outline %OUT_DIR%\outline.md --world-bible %OUT_DIR%\world_bible.md --chapters 12 --control-dir %CONTROL_DIR% --batch-size 1 --batch-attempts 3
if errorlevel 1 goto :failed
:controls_ready

echo.
echo [2/3] Validating controls before prose generation...
py -3 engine.py --story-id %STORY_ID% --direction-file %DIRECTION% --genre lgbt_romance --chapters 12 --controlled-generation --control-dir %CONTROL_DIR% --control-dry-run
if errorlevel 1 goto :failed

echo.
echo [3/3] Writing chapters with mandatory acceptance and bounded repair...
py -3 -u engine.py --story-id %STORY_ID% --direction-file %DIRECTION% --genre lgbt_romance --chapters 12 --controlled-generation --control-dir %CONTROL_DIR% --segment-words 250 --segment-request-timeout 75 --run-id %STORY_ID%_production
if errorlevel 1 goto :failed

echo.
echo Production completed. Review %OUT_DIR%\chapter_acceptance before treating the output as deliverable.
pause
exit /b 0

:failed
echo.
echo Production stopped. The failing step did not bypass the acceptance controls. Read the error above.
pause
exit /b 1
