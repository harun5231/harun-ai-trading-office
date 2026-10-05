"""Ordinary headed Chromium subprocess: no Playwright import, CDP or challenge logic."""
import os
import subprocess
import signal
import sys
import time
from pathlib import Path
from .desktop import dimensions


def chromium_executable():
    # Use the full Chromium binary shipped in the worker image, not headless_shell.
    override=os.environ.get('OFFICE_CHROMIUM_EXECUTABLE')
    candidates=[Path(override)] if override else sorted(
        Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH','/ms-playwright')).glob('chromium-*/chrome-linux*/chrome'))
    matches=[p for p in candidates if p.is_file() and os.access(p,os.X_OK)]
    if len(matches)!=1:raise RuntimeError('CHROMIUM_EXECUTABLE_NOT_FOUND_OR_AMBIGUOUS')
    return str(matches[0])


class ManualBrowser:
    def __init__(self, profile):self.profile=Path(profile);self.process=None
    def active(self):return self.process is not None and self.process.poll() is None
    def start(self, owner_fds):
        if self.active():return
        width,height=dimensions()
        command=[chromium_executable(),f'--user-data-dir={self.profile}',
                 f'--window-size={width},{height}','--window-position=0,0',
                 # Display controls only: no mobile identity or touch-point emulation.
                 '--start-fullscreen','--force-device-scale-factor=1','--new-window',
                 '--no-first-run','--no-default-browser-check','https://app.neurobro.ai/']
        # Match the existing container's browser sandbox configuration; never add
        # stealth flags, automation overrides or change the browser identity.
        if os.environ.get('OFFICE_MANAGED')=='1':command.insert(1,'--no-sandbox')
        # Inherited locks protect the profile even if the controller unexpectedly dies.
        self.process=subprocess.Popen([sys.executable,'-m','worker.manual_browser','--guard',*command],stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,pass_fds=tuple(owner_fds))
        time.sleep(.5)
        if not self.active():raise RuntimeError('MANUAL_BROWSER_EXITED')
    def stop(self):
        if self.process is None:return
        if self.active():
            # Chromium handles SIGTERM gracefully. Never force-kill then open Playwright.
            self.process.terminate()
            try:self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:raise RuntimeError('MANUAL_BROWSER_NOT_STOPPED') from None
        self.process=None


def guard(command):
    """Hold inherited flock descriptors until Chromium exits, even if API dies."""
    child=None;stopping=False
    def stop(*_):
        nonlocal stopping
        stopping=True
        if child is not None and child.poll() is None:child.terminate()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    child=subprocess.Popen(command,stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if stopping:child.terminate()
    return child.wait()

if __name__=='__main__':
    if len(sys.argv)<3 or sys.argv[1]!='--guard':raise SystemExit(2)
    raise SystemExit(guard(sys.argv[2:]))
