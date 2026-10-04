#!/bin/sh
# Run from the existing checkout AFTER git pull; never remove named volumes.
set -eu
umask 077
python3 - <<'PY'
import os
import re
import tempfile
from pathlib import Path
p=Path('.env')
if not p.is_file():raise SystemExit('File .env existing tidak ditemukan; jalankan dari checkout VPS yang sudah terpasang.')
text=p.read_text()
for key,value in [('OFFICE_DESKTOP_WIDTH','430'),('OFFICE_DESKTOP_HEIGHT','932')]:
    pattern=r'(?m)^\s*(?:export\s+)?'+key+r'=.*$'
    if re.search(pattern,text):text=re.sub(pattern,key+'='+value,text)
    else:text=text.rstrip('\n')+'\n'+key+'='+value+'\n'
with tempfile.NamedTemporaryFile(mode='w',prefix='.env.portrait-',dir='.',delete=False) as f:
    f.write(text);f.flush();os.fsync(f.fileno());temp=f.name
os.chmod(temp,0o600)
os.replace(temp,p)
PY
export OFFICE_DESKTOP_WIDTH=430 OFFICE_DESKTOP_HEIGHT=932
docker compose config --quiet
docker compose up -d --build --no-deps --force-recreate --wait --wait-timeout 240 worker
# Refresh upstream connections; TLS volume and all worker/session volumes stay intact.
docker compose restart proxy
docker compose ps
