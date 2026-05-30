@echo off
echo Iniciando Bravo Consig...
cd C:\consigplat
:loop
python main.py
echo Servidor parou. Reiniciando em 3 segundos...
timeout /t 3
goto loop
