import argparse
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .core import Ledger, Review
from .workflow import Workflow, FixtureAdapter


def private_directory(value):
    path=Path(value).expanduser().resolve()
    repo=Path(__file__).resolve().parent.parent
    if path==repo or repo in path.parents: raise SystemExit('Data worker harus berada DI LUAR repository/public web root')
    path.mkdir(parents=True,exist_ok=True,mode=0o700);path.chmod(0o700)
    return path


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description='DRY RUN ONLY; tidak ada mode live')
    p.add_argument('command',choices=['demo','browser-dry-run','screen-neurobro','serve'])
    p.add_argument('--data-dir',default=str(Path.home()/'.local/state/harun-office'))
    p.add_argument('--config',help='Konfigurasi selector lokal di luar repository')
    p.add_argument('--source',choices=['fixture','browser'],default='fixture',help='Snapshot yang dibaca server')
    p.add_argument('--port',type=int,default=8787)
    a=p.parse_args();base=private_directory(a.data_dir)
    source='fixture' if a.command=='demo' else ('browser' if a.command in ('browser-dry-run','screen-neurobro') else a.source)
    directory=private_directory(base/source)
    output=directory/'snapshot.json'
    if a.command=='serve':
        token=os.environ.get('OFFICE_READ_TOKEN','')
        origin=os.environ.get('OFFICE_DASHBOARD_ORIGIN','https://harun5231.github.io')
        if len(token)<32: raise SystemExit('OFFICE_READ_TOKEN minimal 32 karakter; hanya simpan di environment worker')
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass  # Never log authorization headers.
            def cors(self):
                if self.headers.get('Origin')==origin:
                    self.send_header('Access-Control-Allow-Origin',origin)
                    self.send_header('Vary','Origin')
            def do_OPTIONS(self):
                self.send_response(204);self.cors();self.send_header('Access-Control-Allow-Headers','Authorization');self.send_header('Access-Control-Allow-Methods','GET, OPTIONS');self.end_headers()
            def do_GET(self):
                if self.path!='/snapshot' or not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token):
                    self.send_response(403);self.end_headers();return
                if not output.exists(): self.send_response(503);self.end_headers();return
                self.send_response(200);self.cors();self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(output.read_bytes())
        print(f'Read-only server: http://127.0.0.1:{a.port}/snapshot (bind lokal saja)')
        ThreadingHTTPServer(('127.0.0.1',a.port),Handler).serve_forever();return
    # One coordinator per source; SQLite independently enforces the daily cap.
    import fcntl
    process_lock=(directory/'worker.lock').open('a')
    try: fcntl.flock(process_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: raise SystemExit('Worker untuk sumber ini masih berjalan; jangan jalankan duplikat')
    adapter=None
    ledger=Ledger(directory/'ledger.sqlite3')
    try:
        if a.command=='demo': adapter=FixtureAdapter(directory/'artifacts')
        else:
            if not a.config: raise Review('NEEDS_REVIEW: konfigurasi browser/login belum dihubungkan')
            config_path=Path(a.config).expanduser().resolve();repo=Path(__file__).resolve().parent.parent
            if config_path==repo or repo in config_path.parents: raise Review('NEEDS_REVIEW: simpan konfigurasi pribadi di luar repository')
            from .browser import BrowserAdapter
            adapter=BrowserAdapter(json.loads(config_path.read_text()),directory/'browser-profile',directory/'artifacts')
        flow=Workflow(ledger,adapter,output);snapshot=flow.run(screening_only=a.command=='screen-neurobro')
        print(json.dumps({'status':snapshot['status'],'mode':snapshot['mode'],'source':snapshot['source'],'locked':snapshot['locked'],'snapshot':str(output)}))
        if snapshot['status']=='ERROR': raise SystemExit(2)
    except Review as exc:
        ledger.event('ERROR','Coordinator',str(exc))
        output.write_text(json.dumps(ledger.snapshot('BROWSER_DRY_RUN'),indent=2))
        print(str(exc));raise SystemExit(2)
    finally:
        if adapter and hasattr(adapter,'close'): adapter.close()
        ledger.db.close()
        process_lock.close()

if __name__=='__main__': main()
