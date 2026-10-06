"""Root bootstrap copies private runtime secrets then permanently drops privileges."""
import os
import stat
from pathlib import Path

def repair_state_locks(directory):
    """Restore worker ownership of existing named locks without replacing inodes."""
    try:
        parent=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    except FileNotFoundError:return
    except OSError:raise SystemExit('WORKER_STATE_LOCK_INVALID') from None
    try:
        for name in ('migration.lock','cycle.lock'):
            try:
                fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
            except FileNotFoundError:continue
            except OSError:raise SystemExit('WORKER_STATE_LOCK_INVALID') from None
            try:
                value=os.fstat(fd)
                if not stat.S_ISREG(value.st_mode) or value.st_nlink!=1:
                    raise SystemExit('WORKER_STATE_LOCK_INVALID')
                os.fchown(fd,10001,10001)
                os.fchmod(fd,0o600)
            except OSError:raise SystemExit('WORKER_STATE_LOCK_INVALID') from None
            finally:os.close(fd)
    finally:os.close(parent)

def main():
    os.umask(0o077)
    for p in (Path('/data'),Path('/run/office')):
        p.mkdir(parents=True,exist_ok=True);p.chmod(0o700);os.chown(p,10001,10001)
    repair_state_locks(Path('/data')/'trading')
    for name,env in [('read_token','OFFICE_READ_TOKEN'),('control_token','OFFICE_CONTROL_TOKEN')]:
        value=(Path('/run/secrets')/name).read_text().strip()
        if len(value)<32:raise SystemExit('WORKER_SECRET_INVALID')
        os.environ[env]=value
        p=Path('/run/office')/name;p.write_text(value);p.chmod(0o600);os.chown(p,10001,10001)
    # Empty secret is permitted: API reports NOT_CONFIGURED, not a boot crash.
    p=Path('/run/office/neuroapi_key');p.write_bytes(Path('/run/secrets/neuroapi_key').read_bytes());p.chmod(0o600);os.chown(p,10001,10001)
    os.environ['NEUROBRO_API_KEY_FILE']=str(p)
    for name,env in [('binance_api_key','BINANCE_API_KEY_FILE'),('binance_api_secret','BINANCE_API_SECRET_FILE')]:
        try:
            p=Path('/run/office')/name;p.write_bytes((Path('/run/secrets')/name).read_bytes());p.chmod(0o600);os.chown(p,10001,10001)
            os.environ[env]=str(p)
        except Exception:raise SystemExit('BINANCE_SECRET_STORAGE_FAILED') from None
    os.setgroups([]);os.setgid(10001);os.setuid(10001)
    os.execvp('python',['python','-m','worker.api_service'])
if __name__=='__main__':main()
