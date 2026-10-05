"""Optional post-validation archive. Never removes trading data or logs."""
import os
import shutil
from datetime import datetime,timezone
from pathlib import Path
os.umask(0o077)
settings=dict(line.split('=',1) for line in Path('.env').read_text().splitlines() if '=' in line and not line.lstrip().startswith('#'))
root=Path(settings['OFFICE_PRIVATE_DIR'].strip().strip('"').strip("'")).resolve()
if root==Path.cwd() or Path.cwd() in root.parents:raise SystemExit('PRIVATE_PATH_REQUIRED')
archive=root/('retired-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S'))
archive.mkdir(mode=0o700)
for p in (root/'config',root/'secrets/desktop_hash'):
    if p.exists() and not p.is_symlink():shutil.move(str(p),str(archive/p.name))
print('Arsip privat dibuat; state trading dan log tidak dihapus.')
