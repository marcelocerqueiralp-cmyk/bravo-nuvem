"""
Bravo Consig CRM - Servico do Windows
Inicia automaticamente com o Windows
"""
import sys
import os
import subprocess
import win32serviceutil
import win32service
import win32event
import servicemanager

NOME_SERVICO    = "BravoConsigCRM"
DISPLAY_SERVICO = "Bravo Consig CRM"
DESC_SERVICO    = "CRM de consignado com robos ConsigLog"
DIRETORIO_APP   = r"C:\consigplat"
PYTHON_EXE      = sys.executable
LOG_DIR         = r"C:\consigplat\logs"

UVICORN_CMD = [
    PYTHON_EXE, "-m", "uvicorn",
    "main:app",
    "--host", "0.0.0.0",
    "--port", "8000",
    "--workers", "2",
]

os.makedirs(LOG_DIR, exist_ok=True)

class BravoConsigService(win32serviceutil.ServiceFramework):
    _svc_name_         = NOME_SERVICO
    _svc_display_name_ = DISPLAY_SERVICO
    _svc_description_  = DESC_SERVICO

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.processo   = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)
        if self.processo:
            self.processo.terminate()

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, "")
        )
        self._run()

    def _run(self):
        os.chdir(DIRETORIO_APP)
        env = os.environ.copy()
        env["PYTHONPATH"] = DIRETORIO_APP

        stdout_log = open(os.path.join(LOG_DIR, "service_stdout.log"), "a", encoding="utf-8")
        stderr_log = open(os.path.join(LOG_DIR, "service_stderr.log"), "a", encoding="utf-8")

        self.processo = subprocess.Popen(
            UVICORN_CMD,
            cwd=DIRETORIO_APP,
            env=env,
            stdout=stdout_log,
            stderr=stderr_log,
        )
        self.processo.wait()

if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(BravoConsigService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(BravoConsigService)
