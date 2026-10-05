"""Conservative Linux process proof; incomplete visibility is never permission."""
import os
from pathlib import Path


def prove_quiet(profile, session_id=None, proc=Path('/proc'), dead_pid=None):
    profile=Path(profile).resolve()
    try:
        def namespace_ids(status,key):
            return [int(n) for n in next(line for line in status.splitlines() if line.startswith(key+':')).split()[1:]]
        depth=len(namespace_ids((proc/'self/status').read_text(),'NSpid'))
        mounts=(proc/'mounts').read_text().splitlines()
        for line in mounts:
            parts=line.split()
            if len(parts)>3 and parts[1]==str(proc):
                if any(o.startswith('hidepid=') and o not in ('hidepid=0','hidepid=off') for o in parts[3].split(',')):
                    raise RuntimeError('PROFILE_NOT_RELEASED')
        for entry in proc.iterdir():
            if not entry.name.isdigit():continue
            try:
                raw=(entry/'stat').read_text()
                end=raw.rindex(')');comm=raw[raw.index('(')+1:end]
                fields=raw[end+2:].split();state=fields[0];sid=int(fields[3])
                if state in ('Z','X'):continue  # exited; cannot access profile
                status=(entry/'status').read_text()
                pids=namespace_ids(status,'NSpid');sessions=namespace_ids(status,'NSsid')
                sid=sessions[depth-1] if len(sessions)>=depth else None
                pid=pids[depth-1] if len(pids)>=depth else None
                args=(entry/'cmdline').read_bytes().split(b'\0')
                args=[os.fsdecode(a) for a in args if a]
            except FileNotFoundError:
                if not entry.exists():continue
                raise RuntimeError('PROFILE_NOT_RELEASED') from None
            if dead_pid is not None and pid==dead_pid:
                raise RuntimeError('PROFILE_NOT_RELEASED')
            if session_id is not None and sid==session_id:
                raise RuntimeError('PROFILE_NOT_RELEASED')
            # Kernel threads cannot own browser profiles. Unknown user tasks fail closed.
            if not args:
                flags=int(fields[6])
                if flags & 0x00200000:continue  # PF_KTHREAD
                raise RuntimeError('PROFILE_NOT_RELEASED')
            names=(comm.lower(),Path(args[0]).name.lower())
            # Conservatively refuse even another Chromium profile: subprocesses
            # often omit --user-data-dir. No attribution guess permits cleanup.
            if any('chromium' in n or 'chrome' in n or 'headless_shell' in n for n in names):
                raise RuntimeError('PROFILE_NOT_RELEASED')
            for i,arg in enumerate(args):
                value=arg.split('=',1)[1] if arg.startswith('--user-data-dir=') else (
                    args[i+1] if arg=='--user-data-dir' and i+1<len(args) else None)
                if value is not None:
                    candidate=Path(value)
                    if not candidate.is_absolute():candidate=Path(os.readlink(entry/'cwd'))/candidate
                    if candidate.resolve()==profile:raise RuntimeError('PROFILE_NOT_RELEASED')
    except (OSError,ValueError,IndexError,StopIteration):
        raise RuntimeError('PROFILE_NOT_RELEASED') from None
