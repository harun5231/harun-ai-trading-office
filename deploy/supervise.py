"""Exit on process/hang failure so Compose actually restarts unhealthy services."""
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from worker.health import probe_api
from worker.runner import atomic


def main():
    os.umask(0o077)
    runtime=Path('/run/office');children=[];stopping=False
    def stop(*args):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    def spawn(args):
        process=subprocess.Popen(args);children.append(process);return process
    try:
        subprocess.run(['xauth','-f',os.environ['XAUTHORITY'],'add',os.environ['DISPLAY'],'.',os.urandom(16).hex()],check=True,stdout=subprocess.DEVNULL)
        spawn(['Xvfb',':99','-screen','0','1024x768x24','-nolisten','tcp','-auth',os.environ['XAUTHORITY']])
        for _ in range(40):
            if subprocess.run(['xdpyinfo'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:break
            time.sleep(.25)
        else:raise RuntimeError('Desktop unavailable')
        # RFB is loopback ONLY. Caddy authenticates all noVNC HTTP + WebSocket traffic.
        spawn(['x11vnc','-display',':99','-auth',os.environ['XAUTHORITY'],'-localhost','-rfbport','5900','-nopw','-forever','-shared','-noxdamage','-quiet'])
        spawn(['websockify','--web','/usr/share/novnc','0.0.0.0:6080','127.0.0.1:5900'])
        spawn(['python','-m','worker.session_service','--host','0.0.0.0','--data-dir','/data','--config',os.environ['OFFICE_CONFIG']])
        spawn(['python','-m','worker.runner'])
        started=time.monotonic();failures=0
        while not stopping:
            if any(p.poll() is not None for p in children):raise RuntimeError('Managed process exited')
            atomic(runtime/'supervisor.json',{'at':time.time(),'ok':True})
            if time.monotonic()-started>120:
                try:okay=probe_api()
                except Exception:okay=False
                failures=0 if okay else failures+1
                if failures>=3:raise RuntimeError('Health watchdog failed')
            time.sleep(5)
    finally:
        for p in reversed(children):
            if p.poll() is None:p.terminate()
        deadline=time.monotonic()+60
        for p in reversed(children):
            try:p.wait(timeout=max(.1,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:p.kill()

if __name__=='__main__':main()
