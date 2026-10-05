"""Provision only missing server secrets; key entry is silent and outside git."""
import getpass
import os
import secrets
import tempfile
import warnings
from pathlib import Path

def binance_secret(folder,name,prompt):
    path=folder/name
    if path.is_symlink():raise SystemExit('SECRET_SYMLINK_REJECTED')
    if path.exists():
        if not path.is_file():raise SystemExit('SECRET_FILE_INVALID')
        existing=path.read_bytes()
        if existing:
            value=existing.decode('ascii').removesuffix('\n')
            if not 16<=len(value)<=512 or any(not 33<=ord(c)<=126 for c in value):raise SystemExit('BINANCE_SECRET_INVALID')
            path.chmod(0o600)
            return
    # getpass must never fall back to echoed stdin, including piped deployment.
    with warnings.catch_warnings():
        warnings.simplefilter('error',getpass.GetPassWarning)
        try:value=getpass.getpass(prompt)
        except (getpass.GetPassWarning,EOFError):raise SystemExit('PRIVATE_TERMINAL_REQUIRED') from None
    if not 16<=len(value)<=512 or any(not 33<=ord(c)<=126 for c in value):raise SystemExit('BINANCE_SECRET_INVALID')
    tmp=None
    try:
        with tempfile.NamedTemporaryFile('w',dir=folder,prefix='.secret-',delete=False) as f:
            tmp=Path(f.name);os.fchmod(f.fileno(),0o600)
            f.write(value+'\n');f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
        fd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if tmp is not None:tmp.unlink(missing_ok=True)

def main():
    os.umask(0o077)
    settings={}
    for line in Path('.env').read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k,v=line.split('=',1);settings[k]=v.strip().strip('"').strip("'")
    root=Path(settings['OFFICE_PRIVATE_DIR']).resolve()
    repo=Path.cwd().resolve()
    if root==repo or repo in root.parents:raise SystemExit('Private directory must be outside checkout')
    if root in repo.parents:raise SystemExit('PRIVATE_DIRECTORY_UNSAFE')
    root.mkdir(parents=True,exist_ok=True,mode=0o700);root.chmod(0o700)
    folder=root/'secrets'
    if folder.is_symlink():raise SystemExit('SECRET_SYMLINK_REJECTED')
    folder.mkdir(parents=True,exist_ok=True,mode=0o700);folder.chmod(0o700)
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
    binance_secret(folder,'binance_api_key','Tempel Binance API Key (tidak terlihat), lalu Enter: ')
    binance_secret(folder,'binance_api_secret','Tempel Binance API Secret (tidak terlihat), lalu Enter: ')
    print('Secret privat siap; key tidak ditampilkan.')
if __name__=='__main__':
    try:main()
    except (OSError,UnicodeError):raise SystemExit('PRIVATE_SECRET_STORAGE_FAILED') from None
