"""Minimal root bootstrap; all browser/worker processes run as uid 10001."""
import os
from pathlib import Path


def main():
    os.umask(0o077)
    for path in (Path('/data'),Path('/run/office')):
        path.mkdir(parents=True,exist_ok=True);path.chmod(0o700);os.chown(path,10001,10001)
    for name,env in [('read_token','OFFICE_READ_TOKEN'),('control_token','OFFICE_CONTROL_TOKEN')]:
        value=(Path('/run/secrets')/name).read_text().strip()
        if len(value)<32:raise SystemExit('Worker secret missing/invalid')
        os.environ[env]=value
        # Docker exec and health probes can read privately; no secret is an image layer.
        path=Path('/run/office')/name
        path.write_text(value);path.chmod(0o600);os.chown(path,10001,10001)
    os.environ['HOME']='/home/office'
    os.execvp('gosu',['gosu','office','python','-m','deploy.supervise'])

if __name__=='__main__':main()
