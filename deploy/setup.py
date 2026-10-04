"""One-time cloud-console setup; real secrets stay outside git and Docker build context."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess

ROOT=Path(__file__).resolve().parent.parent


def private_location(value):
    path=Path(value).expanduser().resolve()
    if path==ROOT or ROOT in path.parents:raise ValueError('Private directory must be outside repository')
    return path


def create_once(path,value):
    path=Path(path)
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:return False
    with os.fdopen(fd,'w') as f:f.write(value);f.flush();os.fsync(f.fileno())
    return True


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description='Setup privat sekali dari console VPS di iPhone')
    p.add_argument('--host',required=True,help='Domain worker yang sudah menunjuk IP server')
    p.add_argument('--private-dir',default='/opt/harun-office-private')
    p.add_argument('--prepare-only',action='store_true',help='Buat konfigurasi tanpa menjalankan Compose')
    args=p.parse_args()
    if os.geteuid()!=0:raise SystemExit('Jalankan setup dengan sudo pada server sendiri')
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]+\.[a-z]{2,}',args.host):raise SystemExit('Domain lowercase tanpa URL/path diperlukan')
    private=private_location(args.private_dir)
    # Env interpolation treats $, quotes and newlines specially: reject ambiguous paths.
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+',str(private)):raise SystemExit('Gunakan path privat sederhana tanpa spasi')
    subprocess.run(['docker','compose','version'],check=True,stdout=subprocess.DEVNULL)
    for folder in (private,private/'secrets',private/'config'):
        folder.mkdir(parents=True,exist_ok=True);folder.chmod(0o700)
    create_once(private/'secrets/read_token',secrets.token_urlsafe(48))
    create_once(private/'secrets/control_token',secrets.token_urlsafe(48))
    # Create the password file before hashing, so an interrupted setup cannot rotate it.
    password_path=private/'secrets/desktop_password'
    create_once(password_path,secrets.token_urlsafe(24))
    if not (private/'secrets/desktop_hash').exists():
        result=subprocess.run(['docker','run','--rm','-i','caddy:2.10.2-alpine','caddy','hash-password'],
            input=password_path.read_text()+'\n',text=True,capture_output=True,check=True)
        hashes=[line for line in result.stdout.splitlines() if line.startswith(('$2a$','$2b$'))]
        if len(hashes)!=1:raise SystemExit('Pembuatan hash desktop gagal; secret tetap tersimpan privat')
        create_once(private/'secrets/desktop_hash',hashes[0])
    config=private/'config/browser.json'
    create_once(config,(ROOT/'worker/browser-config.example.json').read_text())
    # Same uid as the container; files are not world-readable.
    os.chown(private/'config',10001,10001);os.chown(config,10001,10001)
    env=(f'OFFICE_WORKER_HOST={args.host}\nOFFICE_DASHBOARD_ORIGIN=https://harun5231.github.io\n'
         f'OFFICE_DESKTOP_USER=harun\nOFFICE_PRIVATE_DIR={private}\n'
         'OFFICE_AUTO_DRY_RUN=false\nOFFICE_RUN_AT=08:00\n')
    env_path=ROOT/'.env'
    if env_path.exists():
        existing=env_path.read_text().splitlines()
        for expected in (f'OFFICE_WORKER_HOST={args.host}',f'OFFICE_PRIVATE_DIR={private}'):
            if expected not in existing:raise SystemExit('Konfigurasi existing berbeda; tidak ditimpa')
    else:create_once(env_path,env)
    access=(f'Worker: https://{args.host}\nDesktop user: harun\n'
            f'Desktop password: {password_path.read_text()}\n'
            f'Dashboard control token: {(private/"secrets/control_token").read_text()}\n'
            f'Dashboard read token: {(private/"secrets/read_token").read_text()}\n')
    create_once(private/'ACCESS.txt',access)
    subprocess.run(['docker','compose','config','--quiet'],cwd=ROOT,check=True)
    if not args.prepare_only:
        subprocess.run(['docker','compose','up','-d','--build'],cwd=ROOT,check=True)
    print('Konfigurasi siap. Akses privat tersimpan pada '+str(private/'ACCESS.txt'))
    print('Belum membuktikan login Neurobro atau layanan publik sehat. Periksa docker compose ps dan /health.')

if __name__=='__main__':main()
