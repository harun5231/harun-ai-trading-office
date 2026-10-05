"""Closed-schema diagnostics. Never serialize exception messages or process data."""
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from .selector_evidence import write_private

REASONS = {
    'PRIVATE_CONFIG_CHECK': 'PRIVATE_CONFIG_UNSAFE',
    'SERVICE_LOCK_ACQUIRE': 'WORKER_BUSY',
    'PROFILE_OWNER_ACQUIRE': 'WORKER_BUSY',
    'STALE_SINGLETON_RECOVERY': 'PROFILE_NOT_RELEASED',
    'PROFILE_RELEASE_CHECK': 'PROFILE_NOT_RELEASED',
    'PLAYWRIGHT_START': 'PLAYWRIGHT_START_FAILED',
    'PERSISTENT_CONTEXT_OPEN': 'PERSISTENT_CONTEXT_FAILED',
    'NEUROBRO_NAVIGATION': 'NAVIGATION_FAILED',
    'DOM_DISCOVERY': 'DOM_DISCOVERY_FAILED',
    'EVIDENCE_SANITIZE': 'EVIDENCE_SANITIZE_FAILED',
    'EVIDENCE_WRITE': 'EVIDENCE_WRITE_FAILED',
    'CONFIG_COMMIT': 'CONFIG_COMMIT_FAILED',
    'BROWSER_CLOSE': 'BROWSER_CLOSE_FAILED',
}
EXTRA = {'PRIVATE_DESTINATION_UNWRITABLE', 'PROFILE_DIRECTORY_UNAVAILABLE', 'COMPLETED'}
FIELDS = {'stage','reason','timestamp','profile_lock_present','singleton_count',
          'browser_process_detected','cleanup_failed'}


def validate(data):
    if not isinstance(data,dict) or set(data)!=FIELDS: raise ValueError('UNSAFE_DIAGNOSTIC')
    if data['stage'] not in REASONS or data['reason'] not in set(REASONS.values())|EXTRA: raise ValueError('UNSAFE_DIAGNOSTIC')
    if not isinstance(data['timestamp'],str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00',data['timestamp']): raise ValueError('UNSAFE_DIAGNOSTIC')
    for key in ('profile_lock_present','browser_process_detected'):
        if data[key] is not None and type(data[key]) is not bool: raise ValueError('UNSAFE_DIAGNOSTIC')
    if type(data['cleanup_failed']) is not bool: raise ValueError('UNSAFE_DIAGNOSTIC')
    count=data['singleton_count']
    if count is not None and (type(count) is not int or not 0<=count<=3): raise ValueError('UNSAFE_DIAGNOSTIC')
    return data


def record(stage, reason, profile, *, quiet_proven=False, cleanup_failed=False):
    # A failed quiet proof can mean inaccessible /proc, not necessarily Chromium.
    # Unknown is null; never guess presence from an arbitrary exception message.
    present=count=None
    try:
        if profile.is_dir():
            present=os.path.lexists(profile/'.office-owner.lock')
            count=sum(os.path.lexists(profile/n) for n in ('SingletonLock','SingletonSocket','SingletonCookie'))
    except OSError: pass
    return validate({'stage':stage,'reason':reason,'timestamp':datetime.now(timezone.utc).isoformat(timespec='microseconds'),
        'profile_lock_present':present,'singleton_count':count,
        'browser_process_detected':False if quiet_proven else None,'cleanup_failed':cleanup_failed})


def save(config, data):
    return write_private(config,'selector-discovery-diagnostic.json',validate(data))


def main():
    import argparse
    p=argparse.ArgumentParser(description='Read private sanitized diagnostic; no browser access')
    p.add_argument('--path',default='/private/selector-discovery-diagnostic.json');args=p.parse_args()
    try:
        fd=os.open(args.path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd) as f:
            s=os.fstat(f.fileno())
            if not stat.S_ISREG(s.st_mode) or s.st_mode & 0o077 or s.st_size>4096: raise ValueError('UNSAFE_DIAGNOSTIC')
            data=validate(json.load(f))
        print(json.dumps(data,sort_keys=True));return 0
    except Exception:
        print('{"state":"ERROR","reason":"PRIVATE_DIAGNOSTIC_UNAVAILABLE_OR_UNSAFE"}');return 1

if __name__=='__main__':raise SystemExit(main())
