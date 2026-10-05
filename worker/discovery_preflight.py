"""Staged Phase 3 discovery. Only safe codes cross the exception boundary."""
import fcntl
import json
import os
import stat
import tempfile
import time
from pathlib import Path
from . import selector_diagnostic as diagnostic
from . import selector_evidence as evidence
from .process_safety import prove_quiet
from .session_service import SessionBrowser
from .desktop import browser_options
from .structural_discovery import discover_phase3
from .selector_discovery import commit_session_verified


class Failure(Exception):
    def __init__(self, stage, reason): self.stage=stage;self.reason=reason


class DiscoveryJob:
    def __init__(self, config, data_dir):
        self.config_path=Path(config).absolute()
        self.directory=Path(data_dir).absolute()/'browser'
        self.profile=self.directory/'browser-profile'
        self.browser=None;self.service=None;self.stage='PRIVATE_CONFIG_CHECK'
        self.private_safe=False;self.quiet_proven=False;self.evidence_saved=False
        self.config_updated=False;self.browser_started=False

    def step(self, stage, action):
        self.stage=stage
        try:return action()
        except Failure:raise
        except Exception:raise Failure(stage,diagnostic.REASONS[stage]) from None

    def private_check(self):
        p=self.config_path;repo=Path(__file__).resolve().parent.parent
        if p.is_symlink() or repo in p.resolve().parents:raise ValueError('unsafe')
        fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd) as f:
            info=os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size>1024*1024:raise ValueError('unsafe')
            original=json.load(f)
        if not isinstance(original,dict):raise ValueError('unsafe')
        parent=p.parent.stat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o022:raise ValueError('unsafe')
        for name in ('selector-discovery.json','selector-discovery-diagnostic.json'):
            dest=p.with_name(name)
            if dest.is_symlink():raise ValueError('unsafe')
            if dest.exists():
                s=dest.stat()
                if not stat.S_ISREG(s.st_mode) or s.st_uid!=info.st_uid or s.st_mode & 0o077:raise ValueError('unsafe')
        # Actual create/write/fsync/owner proof, not os.access (unreliable for RO mounts).
        try:
            with tempfile.NamedTemporaryFile(dir=p.parent,prefix='.selector-preflight-') as f:
                os.fchmod(f.fileno(),0o600)
                s=os.fstat(f.fileno())
                if (s.st_uid,s.st_gid)!=(info.st_uid,info.st_gid):os.fchown(f.fileno(),info.st_uid,info.st_gid)
                f.write(b'{}\n');f.flush();os.fsync(f.fileno())
        except Exception:raise Failure(self.stage,'PRIVATE_DESTINATION_UNWRITABLE') from None
        self.private_safe=True
        if self.profile.is_symlink() or self.directory.is_symlink() or not self.profile.is_dir() or repo in self.profile.resolve().parents:
            raise Failure(self.stage,'PROFILE_DIRECTORY_UNAVAILABLE')
        self.original=original

    def service_lock(self):
        self.service=(self.directory/'session-service.lock').open('a')
        fcntl.flock(self.service,fcntl.LOCK_EX|fcntl.LOCK_NB)

    def owner_lock(self):
        self.browser=SessionBrowser(self.directory,self.original)
        self.browser.acquire(allow_stale=True)  # worker.lock + .office-owner.lock

    def recovery(self):
        # Existing process proof and recovery remain authoritative; no blind unlink.
        prove_quiet(self.profile)
        self.browser.owner.recover_stale(self.browser.lock)
        self.quiet_proven=True

    def released(self):
        self.browser.owner.wait_released(timeout=0)
        prove_quiet(self.profile)

    def start(self):
        from playwright.sync_api import sync_playwright
        self.quiet_proven=False;self.browser_started=True
        self.browser.pw=sync_playwright().start()

    def context(self):
        self.browser.context=self.browser.pw.chromium.launch_persistent_context(
            str(self.browser.profile),headless=False,accept_downloads=False,**browser_options())
        self.browser.context.set_default_timeout(3000)
        self.browser.page=self.browser.context.new_page()

    def navigate(self):
        self.browser.page.goto('https://app.neurobro.ai/',wait_until='domcontentloaded',timeout=45000)

    def discover(self):
        deadline=time.monotonic()+20
        while True:
            result=discover_phase3(self.browser.page)
            if result['state'] in ('AUTHENTICATED','LOGIN_REQUIRED','CLOUDFLARE_REQUIRED') or time.monotonic()>=deadline:return result
            time.sleep(.5)

    def close_browser(self):
        if self.browser and self.browser_started:
            self.browser.close()
            self.quiet_proven=True;self.browser_started=False

    def release(self):
        if self.browser:
            self.browser.owner.close()
            if self.browser.lock:self.browser.lock.close();self.browser.lock=None
        if self.service:self.service.close();self.service=None

    def run(self):
        failure=None;cleanup_failed=False;result=None
        try:
            self.step('PRIVATE_CONFIG_CHECK',self.private_check)
            self.step('SERVICE_LOCK_ACQUIRE',self.service_lock)
            self.step('PROFILE_OWNER_ACQUIRE',self.owner_lock)
            self.step('STALE_SINGLETON_RECOVERY',self.recovery)
            self.step('PROFILE_RELEASE_CHECK',self.released)
            self.step('PLAYWRIGHT_START',self.start)
            self.step('PERSISTENT_CONTEXT_OPEN',self.context)
            self.step('NEUROBRO_NAVIGATION',self.navigate)
            result=self.step('DOM_DISCOVERY',self.discover)
            # Normal shutdown before publishing either evidence or config.
            self.step('BROWSER_CLOSE',self.close_browser)
            result=self.step('EVIDENCE_SANITIZE',lambda:evidence.sanitize(result))
            self.step('EVIDENCE_WRITE',lambda:evidence.write_private(self.config_path,'selector-discovery.json',result))
            self.evidence_saved=True
            self.config_updated=self.step('CONFIG_COMMIT',lambda:commit_session_verified(self.config_path,result,self.original))
        except Failure as exc:failure=exc
        finally:
            try:self.close_browser()
            except Exception:cleanup_failed=True
            # No workflow continues after a failure. This short-lived job exits;
            # Docker --init reaps any remaining descendants before worker restore.
            if not cleanup_failed:
                try:self.release()
                except Exception:
                    cleanup_failed=True
                    if failure is None:failure=Failure('BROWSER_CLOSE','BROWSER_CLOSE_FAILED')
        reason=failure.reason if failure else 'COMPLETED'
        stage=failure.stage if failure else 'CONFIG_COMMIT'
        saved=False
        if self.private_safe:
            try:
                diagnostic.save(self.config_path,diagnostic.record(stage,reason,self.profile,
                    quiet_proven=self.quiet_proven,cleanup_failed=cleanup_failed));saved=True
            except Exception:pass  # Only the boolean is public, never exception text.
        summary={'state':'ERROR' if failure else result['state'],'stage':stage,'reason':reason,
            'diagnostic_saved':saved,'evidence_saved':self.evidence_saved,'config_updated':self.config_updated,'mode':'DRY_RUN'}
        if not failure:
            summary.update(session_check_ready=result['session_check_ready'],screening_ready=result['screening_ready'],
                selectors={k:(v+' ('+str(len(result['evidence'][k]['candidates']))+' candidates)' if v=='AMBIGUOUS' else v) for k,v in result['status'].items()})
        print(json.dumps(summary,sort_keys=True))
        return 1 if failure else 0 if self.config_updated else 2
