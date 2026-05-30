@echo off 
cd C:\consigplat 
:loop 
python main.py 
timeout /t 3 
goto loop 
