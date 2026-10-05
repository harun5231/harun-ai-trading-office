"""Provision only missing server secrets; key entry is silent and outside git."""
import getpass
import os
import secrets
from pathlib import Path

def main():
    os.umask(0o077)
    settings={}
    for line in Path('.env').read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k,v=line.split('=',1);settings[k]=v.strip().strip('"').strip("'")
    root=Path(settings['OFFICE_PRIVATE_DIR']).resolve()
    repo=Path.cwd().resolve()
    if root==repo or repo in root.parents:raise SystemExit('Private directory must be outside checkout')
    folder=root/'secrets';folder.mkdir(parents=True,exist_ok=True,mode=0o700);folder.chmod(0o700)
    for name in ('read_token','control_token'):
        p=folder/name
        if not p.exists():p.write_text(secrets.token_urlsafe(36));p.chmod(0o600)
    key=folder/'neuroapi_key'
    if key.is_symlink():raise SystemExit('SECRET_SYMLINK_REJECTED')
    if not key.exists() or not key.read_text().strip():
        value=getpass.getpass('Tempel NeuroAPI key (tidak terlihat), lalu Enter: ')
        if not value or len(value)>512 or not value.isascii() or any(c.isspace() for c in value):raise SystemExit('Key kosong/tidak valid; tidak disimpan')
        key.write_text(value+'\n');key.chmod(0o600)
    key.chmod(0o600)
    print('Secret privat siap; key tidak ditampilkan.')
if __name__=='__main__':main()
