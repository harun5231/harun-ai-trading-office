"""Build-time only: normalize public source modes, never touch runtime secrets/data."""
import os
from pathlib import Path

def normalize(root):
    root=Path(root)
    for path in [root,*root.rglob('*')]:
        if path.is_symlink():raise SystemExit('RUNTIME_SOURCE_SYMLINK_DENIED')
        path.chmod(0o755 if path.is_dir() else 0o644)

if __name__=='__main__':normalize('/app')
