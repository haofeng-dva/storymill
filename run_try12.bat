@echo off
cd /d "E:\novel pro"
echo [start] %date% %time% >> "logs\try12.out"
py -3 engine.py --direction-file "directions\rec_1.txt" --story-id story_try_20260820 --chapters 12 --quality-gate --quality-min 4 --parallel-blocks 2 >> "logs\try12.out" 2>> "logs\try12.err"
echo [end] EXIT_CODE=%ERRORLEVEL% %date% %time% >> "logs\try12.out"
