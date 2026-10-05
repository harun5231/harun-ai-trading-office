"""Root bootstrap copies private runtime secrets then permanently drops privileges."""
import os
from pathlib import Path

def main():
    os.umask(0o077)
    for p in (Path('/data'),Path('/run/office')):
        p.mkdir(parents=True,exist_ok=True);p.chmod(0o700);os.chown(p,10001,10001)
    for name,env in [('read_token','OFFICE_READ_TOKEN'),('control_token','OFFICE_CONTROL_TOKEN')]:
        value=(Path('/run/secrets')/name).read_text().strip()
        if len(value)<32:raise SystemExit('WORKER_SECRET_INVALID')
        os.environ[env]=value
        p=Path('/run/office')/name;p.write_text(value);p.chmod(0o600);os.chown(p,10001,10001)
    # Empty secret is permitted: API reports NOT_CONFIGURED, not a boot crash.
    p=Path('/run/office/neuroapi_key');p.write_bytes(Path('/run/secrets/neuroapi_key').read_bytes());p.chmod(0o600);os.chown(p,10001,10001)
    os.environ['NEUROBRO_API_KEY_FILE']=str(p)
    os.setgroups([]);os.setgid(10001);os.setuid(10001)
    os.execvp('python',['python','-m','worker.api_service'])
if __name__=='__main__':main()
