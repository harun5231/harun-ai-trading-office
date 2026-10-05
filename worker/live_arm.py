"""Hard-disabled activation stubs and private checkpoint utilities. No arming path."""
import json
import os
import stat
import sys
import tempfile
import secrets
from pathlib import Path
from .core import day

RUNTIME=Path('/run/office')
class Disarmed(Exception):pass

def write_private(path,value):
    path=Path(path)
    fd,name=tempfile.mkstemp(prefix='.live-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:
            os.fchmod(f.fileno(),0o600)
            if os.geteuid()==0:os.fchown(f.fileno(),10001,10001)
            json.dump(value,f);f.flush();os.fsync(f.fileno())
        os.replace(name,path)
        fd=os.open(path.parent,os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:Path(name).unlink(missing_ok=True)

def read_private(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd) as f:
        st=os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode)!=0o600 or st.st_uid not in (os.geteuid(),10001):raise ValueError()
        return json.loads(f.read(4097))

def new_boot(runtime=RUNTIME):
    # Called at every worker service start, not at CLI startup. Never clear ledger.
    runtime=Path(runtime)
    write_private(runtime/'live-boot.json',{'boot':secrets.token_hex(32)})
    (runtime/'live-arm.json').unlink(missing_ok=True)
    (runtime/'live-run.json').unlink(missing_ok=True)

def require_arm(runtime=RUNTIME):
    # No environment, private file, confirmation, date, or boot nonce can enable it.
    raise Disarmed('LIVE_EXECUTION_DISARMED')

def arm(runtime=RUNTIME):
    raise Disarmed('LIVE_EXECUTION_DISARMED')

def request_execution(runtime=RUNTIME):
    raise Disarmed('LIVE_EXECUTION_DISARMED')

def execution_requested(runtime=RUNTIME):
    return False
