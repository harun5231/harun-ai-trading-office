"""One persistent Neurobro room per Jakarta day, using the existing native UI.

The native transaction still owns uploads, dispatch, completion and receipts.
Only its view of message rows is scoped to this request: older daily discussion
stays on the actual page and remains available to Neurobro and Mini's memory.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import copy
import hashlib
import json
import os
import re
from pathlib import Path
import sqlite3
import stat
import sys
import threading
import time
import uuid
from zoneinfo import ZoneInfo


JOURNAL_NAME = 'neurobro-daily-rooms.sqlite3'


class DailyRoomUnavailable(ValueError):
    pass


def need(value):
    if not value:
        raise DailyRoomUnavailable('NEUROBRO_DAILY_ROOM_UNVERIFIED')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
        allow_nan=False)


def identities(rows):
    """Bind real native IDs, roles, text and completion, not upload projections."""
    need(type(rows) is list and all(type(row) is dict for row in rows))
    return [hashlib.sha256(canonical({key: value for key, value in row.items()
        if key != 'attachments'}).encode()).hexdigest() for row in rows]


def _prefix_name(day, room_id, anchors):
    return 'daily-room-prefix-'+hashlib.sha256(canonical(dict(day=day,room_id=room_id,anchors=anchors)).encode()).hexdigest()+'.json'


def read_prefix_snapshot(root, day, room_id, anchors):
    """Read exact old native identities; a snapshot never authorizes Send."""
    from .native_chart_followup import _directory,_private_file,pairs
    directory=_directory(root);name=_prefix_name(day,room_id,anchors)
    try:
        try:raw=_private_file(directory,name,8*1024*1024)
        except FileNotFoundError:return None
    finally:os.close(directory)
    value=json.loads(raw,object_pairs_hook=pairs)
    need(type(value)is dict and set(value)=={'schema_version','kind','day','room_id','anchors','rows'}
        and value['schema_version']==1 and value['kind']=='NATIVE_DAILY_ROOM_PREFIX_V1'
        and value['day']==day and value['room_id']==room_id and value['anchors']==anchors
        and identities(value['rows'])==anchors and all('attachments'not in row for row in value['rows']))
    return dict(name=name,sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw),value=value)


def retain_prefix_snapshot(root, day, room_id, rows):
    """Preserve native rows before the existing anchor checkpoint advances."""
    from .native_turn_batch import _write
    from .native_chart_followup import _directory
    selected=[{key:value for key,value in row.items()if key!='attachments'}for row in rows]
    anchors=identities(selected)
    value=dict(schema_version=1,kind='NATIVE_DAILY_ROOM_PREFIX_V1',day=day,room_id=room_id,anchors=anchors,rows=selected)
    need(len(canonical(value).encode())<=8*1024*1024)
    _write(root,_prefix_name(day,room_id,anchors),value)
    directory=_directory(root)
    try:os.fsync(directory)
    finally:os.close(directory)
    receipt=read_prefix_snapshot(root,day,room_id,anchors)
    need(receipt is not None and receipt['value']==value)
    return {key:receipt[key]for key in('name','sha256','bytes')}


def prefix_snapshot_matches(root, day, room_id, anchors, rows):
    """Use exact archived native rows when a reloaded UUID changes."""
    try:
        receipt=read_prefix_snapshot(root,day,room_id,anchors)
        if receipt is None or not rows:return False
        old=receipt['value']['rows']
        semantic=lambda row:{key:value for key,value in row.items()if key not in('id','attachments')}
        def same(prior,current):
            return bool(prior and len(prior)==len(current)and all(semantic(before)==semantic(after)
                and (before.get('role')=='assistant'and before.get('complete')is True
                    and type(after.get('id'))is str and re.fullmatch('[A-Za-z0-9_.:-]{1,160}',after['id'])
                    or identities([before])==identities([after]))for before,after in zip(prior,current)))
        if len(rows)>=len(old)and same(old,rows[:len(old)]):return True
        if (len(rows)>=2 and len(rows)<len(old)and rows[-2].get('role')=='user'
                and rows[-1].get('role')=='assistant'and rows[-1].get('complete')is True):
            if same(old[-len(rows):],rows):return True
        # A capped native window may show the old suffix followed by one new
        # user/assistant pair. This identifies history, never its new operation.
        if (len(rows)>=4 and rows[-2].get('role')=='user'and rows[-1].get('role')=='assistant'
                and rows[-1].get('complete')is True and len(rows)-2<len(old)):
            return same(old[-(len(rows)-2):],rows[:-2])
        return False
    except (OSError,ValueError,KeyError,TypeError):return False


# The existing production history observer established ordinary BUTTON history
# entries under direct sidebar group labels (including Hari ini). No guessed
# application route, private runtime state or application API is used.
HISTORY_ROWS_JS = r"""root => {
 const norm=s=>String(s||'').replace(/\s+/g,' ').trim();
 const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();
  return e.isConnected&&s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity)!==0&&r.width>0&&r.height>0;};
 const out=[], buttons=Array.from(root.querySelectorAll('button'));
 for(let index=0;index<buttons.length;index++){
  const e=buttons[index],text=norm(e.innerText);
  if(!text||e.disabled||e.getAttribute('aria-disabled')==='true'||!visible(e))continue;
  if(e.getAttribute('data-analytics-id')||/^(?:Obrolan baru|New chat|Percakapan baru)$/i.test(text))continue;
  let label=null,p=e.parentElement;
  for(let depth=0;p&&depth<7&&p!==document.body&&p!==root;depth++,p=p.parentElement){
   const headings=Array.from(p.querySelectorAll(':scope > h1,:scope > h2,:scope > h3,:scope > [role="heading"],:scope > header,:scope > [data-slot="sidebar-group-label"]'));
   const found=headings.map(n=>norm(n.innerText)).find(t=>/^(?:Hari ini|Kemarin|Today|Yesterday|\d{1,2}\s+\S+(?:\s+\d{4})?|\S+\s+\d{1,2},?\s+\d{4})$/i.test(t));
   if(found){label=found;break;}
  }
  if(label)out.push({index,text,label});
 }
 return out;
}"""

# Passive failure evidence only. Exclude message descendants and input values;
# the private record describes the actual history controls that were rendered.
HISTORY_DIAGNOSTIC_JS = r"""(root, messageSelector) => {
 const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();
  return e.isConnected&&s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity)!==0&&r.width>0&&r.height>0;};
 const shell=e=>visible(e)&&!e.closest(messageSelector);
 const norm=s=>String(s||'').replace(/\s+/g,' ').trim().slice(0,160);
 const shape=e=>({tag:e.tagName,role:e.getAttribute('role'),slot:e.getAttribute('data-slot'),
  analytics:e.getAttribute('data-analytics-id'),expanded:e.getAttribute('aria-expanded'),
  parent:{tag:e.parentElement?.tagName,role:e.parentElement?.getAttribute('role'),
   slot:e.parentElement?.getAttribute('data-slot')}});
 // Resource Timing observes requests already issued by the page. It never
 // fetches an application endpoint or reads bodies, headers or auth state.
 // Keep only fixed route words: IDs, queries and unknown path segments stay
 // outside this private diagnostic as well as outside the public report.
 const routeWords=new Set(('api v1 v2 v3 chat chats conversation conversations history thread threads auth session sessions verify config message messages me user users folder folders saved prompt prompts completion completions list bootstrap turnstile trpc health init initialize initialization refresh token get balance workspace rpc').split(' '));
 const route=name=>{const u=new URL(name,location.origin);
  const host=u.origin===location.origin?'SAME_ORIGIN':
   /(?:^|\.)neurobro\.ai$/.test(u.hostname)?'NEUROBRO':
   /(?:^|\.)privy\.(?:io|ai)$/.test(u.hostname)?'PRIVY':'OTHER';
  return {origin:host,path:u.pathname.split('/').slice(0,9).map(part=>
   !part?'':routeWords.has(part.toLowerCase())?part.toLowerCase():':redacted').join('/')};};
 let network={available:false,resources:[]};
 try{
  const entries=performance.getEntriesByType('resource').filter(e=>
   e.initiatorType==='fetch'||e.initiatorType==='xmlhttprequest');
  network={available:true,observed_elapsed_ms:Math.round(performance.now()),
   request_count:entries.length,resources:entries.slice(-60).map(e=>({...route(e.name),
    initiator:e.initiatorType,start_ms:Math.round(e.startTime),duration_ms:Math.round(e.duration),
    response_status:Number.isInteger(e.responseStatus)?e.responseStatus:null}))};
 }catch(_){/* Observation failure cannot change the original refusal. */}
 return {
  network,
  buttons:Array.from(root.querySelectorAll('button')).filter(shell).slice(0,100)
   .map(e=>({...shape(e),text:norm(e.innerText),disabled:e.disabled})),
  headings:Array.from(root.querySelectorAll('h1,h2,h3,[role="heading"],[data-slot="sidebar-group-label"]'))
   .filter(shell).slice(0,60).map(e=>({...shape(e),text:norm(e.innerText)})),
  widgets:Array.from(root.querySelectorAll('[role],[data-slot]')).filter(shell).slice(0,100).map(shape)
 };
}"""


# Observe ordinary message DOM and its nearest actual scrollable ancestor.
# The action re-resolves and verifies the same observed container/row IDs. It
# scrolls existing history only; no application state, endpoint or Send control.
HISTORY_OLDER_UI_JS = r"""(messages, request) => {
 const visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();
  return e.isConnected&&s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity)!==0&&r.width>0&&r.height>0;};
 const rows=messages.filter(visible);
 const empty={available:false,message_count:rows.length};
 if(!rows.length)return empty;
 let target=null,depth=0;
 for(let p=rows[0].parentElement;p;p=p.parentElement,depth++){
  const s=getComputedStyle(p);
  if(visible(p)&&/^(?:auto|scroll|overlay)$/.test(s.overflowY)
    &&p.clientHeight>0&&p.scrollHeight>p.clientHeight+1
    &&rows.every(e=>p.contains(e))){target=p;break;}
 }
 if(!target)return empty;
 const rect=target.getBoundingClientRect();
 const observed={available:true,message_count:rows.length,depth,tag:target.tagName,
  role:target.getAttribute('role'),slot:target.getAttribute('data-slot'),
  overflow_y:getComputedStyle(target).overflowY,scroll_top:target.scrollTop,
  client_height:target.clientHeight,scroll_height:target.scrollHeight,
  rectangle:{x:rect.x,y:rect.y,width:rect.width,height:rect.height},
  first_message_id:rows[0].getAttribute('data-message-id'),
  last_message_id:rows[rows.length-1].getAttribute('data-message-id')};
 if(request.action==='observe')return observed;
 const previous=request.observed;
 const keys=['message_count','depth','tag','role','slot','overflow_y',
  'client_height','scroll_height','first_message_id','last_message_id'];
 if(request.action!=='show_oldest'||!previous||!keys.every(k=>(observed[k]??null)===(previous[k]??null)))
  return {...observed,scrolled:false,after_scroll_top:target.scrollTop};
 target.scrollTo({top:0,behavior:'instant'});
 return {...observed,scrolled:true,after_scroll_top:target.scrollTop};
}"""


class DailyRoomHistory:
    def __init__(self, room):
        self.room = room

    def rows(self):
        room = self.room
        result = room.bridge.page.locator('html').evaluate(HISTORY_ROWS_JS,
            timeout=room.bridge._timeout(2500))
        need(type(result) is list and all(type(row) is dict and set(row) == {'index', 'text', 'label'}
            and type(row['index']) is int and row['index'] >= 0
            and type(row['text']) is str and row['text']
            and type(row['label']) is str and row['label'] for row in result))
        return result

    def open_sidebar(self):
        room = self.room
        need(room.bridge.status() == 'CONNECTED')
        selector = getattr(room.native, 'CURRENT_CHAT_NAV', None)
        need(type(selector) is str and selector)
        navigation = room.original_visible(selector)
        need(len(navigation) == 1 and navigation[0].is_enabled())
        if navigation[0].get_attribute('aria-expanded') == 'true':
            return  # An already open sidebar may still be loading its rows.
        navigation[0].click(timeout=room.bridge._timeout(5000))

    def select(self, row, *, occurrence=0):
        room = self.room
        need(room.bridge.status() == 'CONNECTED')
        # Loading a conversation adds message/tool buttons before the sidebar.
        # Re-resolve its observed title/label instead of reusing a stale global
        # button index. Duplicate titles retain their history occurrence.
        matches = [item for item in self.rows()
            if (item['text'], item['label']) == (row['text'], row['label'])]
        need(type(occurrence) is int and 0 <= occurrence < len(matches))
        selected = matches[occurrence]
        room.composer()  # No owner draft is discarded by a history selection.
        target = room.bridge.page.locator('button').nth(selected['index'])
        need(target.is_visible() and target.is_enabled()
            and ' '.join(target.inner_text().split()) == row['text'])
        target.click(timeout=room.bridge._timeout(5000))


class DailySend:
    """Durable uncertainty witness immediately before the unchanged Send click."""
    def __init__(self, room, locator):
        self.room, self.locator = room, locator

    def __getattr__(self, name):
        return getattr(self.locator, name)

    def click(self, *args, **kwargs):
        self.room.before_send()
        return self.locator.click(*args, **kwargs)


class DailyRoomStore:
    def __init__(self, archive_root):
        self.root = Path(archive_root)
        self.path = self.root / JOURNAL_NAME

    @contextmanager
    def open(self):
        # Reuse the production chart archive's admitted private persistent root.
        from .native_chart_followup import _directory
        directory = _directory(self.root)
        fd = None
        db = None
        try:
            fd = os.open(JOURNAL_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600, dir_fd=directory)
            before = os.fstat(fd)
            need(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid()
                and before.st_nlink == 1 and stat.S_IMODE(before.st_mode) == 0o600)
            anchored = '/proc/self/fd/' + str(directory) + '/' + JOURNAL_NAME
            db = sqlite3.connect(anchored, timeout=3, isolation_level=None)
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('PRAGMA journal_mode=DELETE')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS daily_rooms(day TEXT PRIMARY KEY, '
                'room_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL, '
                'url TEXT, anchors TEXT NOT NULL, binding TEXT NOT NULL, updated_at REAL NOT NULL)')
            current = os.stat(JOURNAL_NAME, dir_fd=directory, follow_symlinks=False)
            need((before.st_dev, before.st_ino) == (current.st_dev, current.st_ino))
            yield db
            current = os.stat(JOURNAL_NAME, dir_fd=directory, follow_symlinks=False)
            need((before.st_dev, before.st_ino) == (current.st_dev, current.st_ino))
        finally:
            if db is not None:
                db.close()
            if fd is not None:
                os.close(fd)
            os.close(directory)


class DailyRoom:
    def __init__(self, bridge, native, archive_root, *, wall_clock=time.time, history=None):
        self.bridge, self.native, self.wall_clock = bridge, native, wall_clock
        self.store = DailyRoomStore(archive_root)
        self.lock = threading.RLock()
        self.original_fresh = bridge._fresh_conversation
        self.original_turns, self.original_visible = bridge._turns, bridge._visible
        self.offset = 0
        self.prefix = []
        self.day = self.room_id = None
        self.mode = None
        self._connection = None
        self._prepared = False
        self._binding = {}
        self._payload = None
        self._history_windows = []
        self.history = history if history is not None else DailyRoomHistory(self)
        need(type(getattr(native, 'MESSAGE', None)) is str)
        # Install once, before NativeFileTurnReader captures these methods.
        bridge._fresh_conversation = self.fresh
        bridge._turns, bridge._visible = self.turns, self.visible

    @property
    def conversation_id(self):
        need(self.day is not None and self.room_id is not None)
        return 'neurobro:' + self.day + ':' + self.room_id

    def full_turns(self):
        # Native _turns calls _visible; suppress only our offset while reading
        # the complete real DOM. No attachment or assistant text is synthesized.
        current = self.bridge._visible
        try:
            self.bridge._visible = self.original_visible
            return self.original_turns()
        finally:
            self.bridge._visible = current

    def verify_prefix(self, rows):
        need(len(rows) >= self.offset and identities(rows[:self.offset]) == self.prefix)

    def turns(self):
        rows = self.full_turns()
        self.verify_prefix(rows)
        return rows[self.offset:]

    def visible(self, selector):
        nodes = self.original_visible(selector)
        send = getattr(getattr(self.bridge, '_dom_contract', None), 'send', None)
        if send is not None and selector == send:
            return [DailySend(self, locator) for locator in nodes]
        if selector == self.native.MESSAGE:
            # FileTurnReader takes a second native-node snapshot. Its unchanged
            # count-race handling can resample a growing assistant turn.
            self.verify_prefix(self.full_turns())
            return nodes[self.offset:]
        return nodes

    def evidence(self):
        rows = self.full_turns()
        self.verify_prefix(rows)
        return {'schema_version': 1, 'day': self.day, 'room_id': self.room_id,
            'conversation_id': self.conversation_id, 'mode': self.mode,
            'operation_offset': self.offset, 'full_turn_count': len(rows),
            'prefix_sha256': hashlib.sha256(canonical(self.prefix).encode()).hexdigest(),
            'journal': JOURNAL_NAME}

    @staticmethod
    def matches(rows, anchors, binding, state, *, archive_root=None, prefix_identity=None):
        reconciliation = binding.get('identifier_reconciliation')
        if reconciliation is not None:
            if archive_root is None or not isinstance(reconciliation, dict):
                return False
            from .native_screening_recovery import (matches_identifier_binding,
                identifier_checkpoint_verified)
            if anchors == reconciliation.get('original_anchors'):
                return matches_identifier_binding(rows, anchors, binding, archive_root)
            if not identifier_checkpoint_verified(anchors, binding, archive_root):
                return False
        if anchors and len(rows) >= len(anchors) and identities(rows[:len(anchors)]) == anchors:
            return True
        # A reloaded history page may contain only its already anchored tail.
        # Admit that exact suffix, including message IDs and completion, only
        # after a complete user/assistant pair. Active-request prefix checks
        # remain unchanged and cannot accept a changing or unknown tail.
        if (2 <= len(rows) < len(anchors)
                and rows[-2].get('role') == 'user'
                and rows[-1].get('role') == 'assistant'
                and rows[-1].get('complete') is True
                and identities(rows) == anchors[-len(rows):]):
            return True
        if not rows:
            return not anchors and state == 'PREPARED'
        # Reload can prepend older history or replace temporary message IDs.
        # The exact authored prompt and its one marker VALUE identify the room;
        # later assistant text/IDs are not a durable conversation identifier.
        # This fallback does not relax verify_prefix within an active request.
        marker = binding.get('marker')
        for row in rows:
            text = row.get('text')
            if row.get('role') != 'user' or type(text) is not str:
                continue
            text = text.replace('\r\n', '\n').strip()
            if hashlib.sha256(text.encode()).hexdigest() != binding.get('first_prompt_sha256'):
                continue
            values = set(re.findall(r'(?<![A-Za-z0-9_])HARUN_[a-f0-9]{32}(?![A-Za-z0-9_])', text))
            if type(marker) is str and values == {marker}:
                return True
        if archive_root is not None and type(prefix_identity)is tuple and len(prefix_identity)==2:
            return prefix_snapshot_matches(archive_root,*prefix_identity,anchors,rows)
        return False

    def restore(self, anchors, binding, state, *, prefix_identity=None):
        prefix_identity=prefix_identity or(self.day,self.room_id)
        self._history_windows = []
        # A root SPA URL identifies no chat. Reopen ordinary observed history
        # entries, and admit only the room whose actual native messages match.
        self.composer()
        choices = self.history.rows()
        if not choices:
            self.history.open_sidebar()
            # Opening the rail schedules React rendering. Its first empty DOM
            # snapshot does not establish that the owned room is absent.
            history_until = self.bridge._until(5)
            while self.bridge.clock() < history_until:
                status = self.bridge.status()
                need(status in ('CONNECTED', 'UNAVAILABLE'))
                if status == 'CONNECTED':
                    choices = self.history.rows()
                    if choices:
                        break
                self.bridge.page.wait_for_timeout(min(50,
                    max(1, int((history_until-self.bridge.clock())*1000))))
        hints = binding.get('history_titles', [])
        occurrences, ordered = {}, []
        for row in choices:
            key = row['text'], row['label']
            occurrence = occurrences.get(key, 0)
            occurrences[key] = occurrence + 1
            ordered.append((row, occurrence))
        ordered.sort(key=lambda item: item[0]['text'] not in hints)
        for candidate, (row, occurrence) in enumerate(ordered):
            # Passive evidence uses only observations already reached below.
            # Never poll, match, navigate or read a draft for this diagnostic.
            selected = False; before = rows = observed_rows = status = matched = None
            samples = stable = 0; disposition = 'EXCEPTION'
            started = time.monotonic() if candidate == 0 else None
            try:
                before = identities(self.full_turns())
                self.history.select(row, occurrence=occurrence)
                selected = True
                until = self.bridge._until(5)
                previous, stable = None, 0
                older_attempted = False
                while self.bridge.clock() < until:
                    status = self.bridge.status()
                    if status == 'UNAVAILABLE':
                        # Native detection can be unavailable while this selected
                        # history room renders. Wait within its existing budget;
                        # never resubmit, create a room or interact with a challenge.
                        previous, stable = None, 0
                        self.bridge.page.wait_for_timeout(50)
                        continue
                    need(status == 'CONNECTED')
                    rows = self.full_turns()
                    samples += 1
                    matched = self.matches(rows, anchors, binding, state, archive_root=self.store.root,prefix_identity=prefix_identity)
                    if matched:
                        disposition = 'MATCHED'
                        return rows
                    # A saved title is only a navigation hint. Load its older UI
                    # history, then require the exact unchanged identity contract.
                    unique_hint = (row['text'] in hints and sum(
                        (choice['text'], choice['label']) == (row['text'], row['label'])
                        for choice in choices) == 1)
                    if (not older_attempted and unique_hint and state == 'BOUND'
                            and 2 <= len(rows) < len(anchors)
                            and rows[-2].get('role') == 'user'
                            and rows[-1].get('role') == 'assistant'
                            and rows[-1].get('complete') is True):
                        older_attempted = True
                        restored = self.restore_older(rows, anchors, binding, state,prefix_identity=prefix_identity)
                        if restored is not None:
                            observed_rows = restored
                            disposition = 'RESTORED_OLDER'
                            return restored
                    observed = identities(rows)
                    stable = stable + 1 if observed == previous else 0
                    previous = observed
                    if observed != before and stable >= 3 and rows and rows[-1].get('complete') is True:
                        disposition = 'FOREIGN_STABLE'
                        break  # Loaded another historical room; inspect no reply there.
                    self.bridge.page.wait_for_timeout(50)
                else:
                    disposition = 'TIMEOUT'
            finally:
                if candidate == 0:
                    try:
                        self.retain_first_restore_candidate(row, occurrence, anchors, binding, state,
                            rows if observed_rows is None else observed_rows, selected=selected,
                            status=status, matched=matched if disposition != 'RESTORED_OLDER' else None,
                            samples=samples, stable=stable, disposition=disposition,
                            elapsed=time.monotonic()-started, before=before,
                            exception_class=(sys.exc_info()[0].__name__
                                if disposition == 'EXCEPTION' and sys.exc_info()[0] else None))
                    except BaseException:
                        pass  # Evidence never changes the original return/error.
        self.retain_restore_failure(choices)
        raise DailyRoomUnavailable('NEUROBRO_DAILY_ROOM_NOT_FOUND')

    def retain_first_restore_candidate(self, choice, occurrence, anchors, binding, state, rows,
            *, selected, status, matched, samples, stable, disposition, elapsed, exception_class, before=None):
        """Best-effort bounded PRIVATE evidence; never a room admission receipt.

        Raw rows are retained only for known native fields without credential
        patterns. Missing/oversize/sensitive observations are explicit; hashes
        and whitespace comparisons never authorize a room or alter its journal.
        """
        try:
            sensitive = re.compile(r'(?i)(?:\bsk-[A-Za-z0-9_-]{12,}|'
                r'\b(?:api[_ -]?key|api[_ -]?secret|secret[_ -]?key|client[_ -]?secret|'
                r'authorization|bearer|password|access[_ -]?token|refresh[_ -]?token)'
                r'\b(?:[\'"]?\s*[:=]|\s+\S)|'
                r'(?<![A-Za-z0-9])[A-Za-z0-9]{64,}(?![A-Za-z0-9]))')
            safe = lambda value, size: (value if type(value) is str and len(value) <= size
                and sensitive.search(value) is None else None)
            text_sha = lambda text: hashlib.sha256(text.encode()).hexdigest()
            digest = lambda value: value if type(value) is str and re.fullmatch('[a-f0-9]{64}', value) else None
            payload = self._payload if type(self._payload) is dict else {}
            marker = binding.get('marker')
            marker = marker if type(marker) is str and re.fullmatch('HARUN_[a-f0-9]{32}', marker) else None
            observed = rows if type(rows) is list else None
            record = dict(schema_version=1, kind='NATIVE_DAILY_ROOM_FIRST_CANDIDATE_V1',
                at=time.time(), source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                day=self.day if type(self.day) is str and re.fullmatch(r'\d{4}-\d\d-\d\d',self.day) else None,
                room_id=self.room_id if type(self.room_id) is str and re.fullmatch('[a-f0-9]{32}',self.room_id) else None,
                state=state if state in ('CLAIMED','PREPARED','SENT','BOUND') else None,
                operation=safe(payload.get('operation'), 160), request_sha256=digest(payload.get('request_sha256')),
                anchors_sha256=text_sha(canonical(anchors)), first_prompt_sha256=digest(binding.get('first_prompt_sha256')),
                marker=marker, candidate=dict(index=0, occurrence=occurrence,
                    button_index=choice.get('index') if type(choice.get('index')) is int else None,
                    title=safe(choice.get('text'), 256), label=safe(choice.get('label'), 80),
                    title_sha256=text_sha(choice['text']) if type(choice.get('text')) is str else None),
                selected=selected, status=status if status in ('CONNECTED','UNAVAILABLE','LOGIN_REQUIRED','CHALLENGE_REQUIRED','BUSY') else None,
                matches_result=matched if type(matched) is bool else None, disposition=disposition,
                samples=samples, stable=stable, elapsed_seconds=elapsed, exception_class=safe(exception_class,80),
                rows_observed=observed is not None, native_row_count=len(observed) if observed is not None else None,
                rows_observation='LAST_EXISTING_NATIVE_READ' if observed is not None else 'NOT_REACHED',
                before_identities_sha256=text_sha(canonical(before)) if type(before) is list else None,
                native_rows=None, full_rows_retained=False, row_diagnostics=[],
                diagnostic_browser_inputs_performed=False, model_calls=0, journal_database_writes=0)
            if observed is not None:
                # Metadata is bounded independently of the raw-row byte budget.
                summary = observed if len(observed) <= 128 else observed[:2]+observed[-2:]
                record['row_diagnostics_truncated'] = len(summary) != len(observed)
                for row in summary:
                    text = row.get('text') if type(row) is dict else None
                    if type(text) is not str:
                        record['row_diagnostics'].append({'text_observed': False})
                        continue
                    normalized = text.replace('\r\n', '\n').strip()
                    record['row_diagnostics'].append(dict(role=row.get('role') if row.get('role') in
                        ('user','assistant','unknown') else None, complete=row.get('complete') if type(row.get('complete')) is bool else None,
                        id=safe(row.get('id'),160), text_characters=len(text), text_sha256=text_sha(text),
                        normalized_sha256=text_sha(normalized), whitespace_sha256=text_sha(re.sub(r'\s+', ' ', normalized)),
                        first_prompt_hash_matches=text_sha(normalized)==binding.get('first_prompt_sha256'),
                        bound_marker_present=bool(marker and re.search(r'(?<![A-Za-z0-9_])'+re.escape(marker)+r'(?![A-Za-z0-9_])',text))))
                known = {'role','text','id','complete','attachments'}
                supported = (len(observed) <= 128 and all(type(row) is dict and set(row) <= known
                    and type(row.get('text')) is str and row.get('role') in ('user','assistant','unknown')
                    and sensitive.search(row['text']) is None
                    and (row.get('id') is None or safe(row['id'],160) == row['id'])
                    and (row.get('complete') is None or type(row['complete']) is bool)
                    and type(row.get('attachments',[])) is list
                    and all(type(name) is str and safe(name,256) == name for name in row.get('attachments',[]))
                    for row in observed))
                if supported:
                    raw = canonical(observed)
                    record['native_rows_sha256'] = text_sha(raw)
                    if len(raw.encode()) <= 120000 and sensitive.search(raw) is None:
                        record.update(native_rows=observed, full_rows_retained=True)
                    else:
                        record['raw_omission_reason'] = 'SENSITIVE_OR_OVERSIZE'
                else:
                    record['raw_omission_reason'] = 'UNSUPPORTED_OR_OVERSIZE'
            if len(canonical(record).encode()) > 131072:
                record.update(native_rows=None, full_rows_retained=False, raw_omission_reason='BYTE_LIMIT')
            if len(canonical(record).encode()) > 131072:
                return
            from .native_turn_batch import _write
            _write(self.store.root, 'on-daily-room-first-candidate.json', record, replace=True)
        except Exception:
            pass  # Includes serialization, write, replace and writer cleanup.

    def retain_history_window(self, rows, anchors, binding, container):
        """Persist actual identities before any older-history scroll."""
        actual = identities(rows)
        record = {'at': self.wall_clock(), 'native_row_count': len(rows),
            'stored_anchor_count': len(anchors),
            'anchors_sha256': hashlib.sha256(canonical(anchors).encode()).hexdigest(),
            'native_identities_sha256': hashlib.sha256(canonical(actual).encode()).hexdigest(),
            'original_prompt_hash_present': any(row.get('role') == 'user'
                and type(row.get('text')) is str
                and hashlib.sha256(row['text'].replace('\r\n', '\n').strip().encode()).hexdigest()
                    == binding.get('first_prompt_sha256') for row in rows),
            'native_rows': [{'identity_sha256': digest,
                'matching_anchor_indices': [i for i, anchor in enumerate(anchors) if anchor == digest],
                'expected_tail_index': len(anchors) - len(rows) + index,
                'expected_tail_match': (0 <= len(anchors) - len(rows) + index < len(anchors)
                    and digest == anchors[len(anchors) - len(rows) + index]),
                'role': row.get('role'), 'id': row.get('id'), 'complete': row.get('complete'),
                'text_characters': len(row.get('text', '')),
                'text_sha256': hashlib.sha256(row.get('text', '').encode()).hexdigest()}
                for index, (row, digest) in enumerate(zip(rows, actual))],
            'scroll_container': container}
        self._history_windows.append(record)
        # This diagnostic is required before the UI action so the original
        # mismatch survives a subsequent failure or browser recovery.
        from .native_turn_batch import _write
        _write(self.store.root, 'on-daily-room-window.json', {
            'schema_version': 1, 'day': self.day, 'room_id': self.room_id,
            'windows': self._history_windows}, replace=True)

    def restore_older(self, rows, anchors, binding, state, *, prefix_identity=None):
        """Load older messages in the observed room, without changing matches."""
        until = self.bridge._until(20)
        while self.bridge.clock() < until:
            need(self.bridge.status() == 'CONNECTED')
            self.composer()  # Preserve an owner draft or unsent attachments.
            messages = self.bridge.page.locator(self.native.MESSAGE)
            container = messages.evaluate_all(HISTORY_OLDER_UI_JS, {'action': 'observe'})
            need(type(container) is dict)
            self.retain_history_window(rows, anchors, binding, container)
            if (container.get('available') is not True
                    or container.get('message_count') != len(rows)):
                return None  # No unambiguous actual message scroll container.
            need(self.bridge.status() == 'CONNECTED')
            action = messages.evaluate_all(HISTORY_OLDER_UI_JS,
                {'action': 'show_oldest', 'observed': container})
            after = messages.evaluate_all(HISTORY_OLDER_UI_JS, {'action': 'observe'})
            self.retain_history_window(self.full_turns(), anchors, binding,
                {**after, 'phase': 'AFTER_SCROLL', 'action_result': action})
            if type(action) is not dict or action.get('scrolled') is not True:
                return None  # The observed container changed before input.
            previous_count = len(rows)
            advancement_until = min(until, self.bridge._until(5))
            while self.bridge.clock() < advancement_until:
                status = self.bridge.status()
                need(status in ('CONNECTED', 'UNAVAILABLE'))
                if status == 'CONNECTED':
                    current = self.full_turns()
                    if self.matches(current, anchors, binding, state, archive_root=self.store.root,
                            prefix_identity=prefix_identity or(self.day,self.room_id)):
                        return current
                    if (len(current) > previous_count and current[-1].get('role') == 'assistant'
                            and current[-1].get('complete') is True):
                        rows = current
                        break
                self.bridge.page.wait_for_timeout(50)
            else:
                after = messages.evaluate_all(HISTORY_OLDER_UI_JS, {'action': 'observe'})
                self.retain_history_window(self.full_turns(), anchors, binding,
                    {**after, 'phase': 'NO_ADVANCEMENT', 'action_result': action})
                return None  # Loading did not advance; never keep scrolling.
        return None

    def retain_restore_failure(self, choices):
        """Keep the failed history observation before any browser recovery."""
        directory = descriptor = None
        temporary = '.on-daily-room-restore-' + uuid.uuid4().hex + '.tmp'
        try:
            rows = self.full_turns()
            composer = self.original_visible(self.bridge._dom_contract.composer)
            drafts = [item.input_value() for item in composer]
            controls = self.bridge.page.locator('html').evaluate(
                HISTORY_DIAGNOSTIC_JS, self.native.MESSAGE,
                timeout=self.bridge._timeout(2500))
            record = {'schema_version': 1, 'reason': 'NEUROBRO_DAILY_ROOM_NOT_FOUND',
                'at': self.wall_clock(), 'day': self.day, 'room_id': self.room_id,
                'history_choice_count': len(choices), 'history_choices': choices[:100],
                'history_windows': self._history_windows,
                'controls': controls, 'page_url_sha256': hashlib.sha256(
                    self.bridge.page.url.encode()).hexdigest(),
                'composer': [{'characters': len(value), 'empty': not value,
                    'sha256': hashlib.sha256(value.encode()).hexdigest()} for value in drafts],
                'native_rows': [{'role': row.get('role'), 'complete': row.get('complete'),
                    'text_characters': len(row.get('text', '')),
                    'text_sha256': hashlib.sha256(row.get('text', '').encode()).hexdigest()}
                    for row in rows[:100]], 'diagnostic_browser_inputs_performed': False}
            raw = canonical(record).encode()
            need(len(raw) <= 131072)
            from .native_chart_followup import _directory
            directory = _directory(self.store.root)
            name = 'on-daily-room-restore.json'
            try:
                prior = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                need(stat.S_ISREG(prior.st_mode) and prior.st_uid == os.geteuid()
                    and prior.st_nlink == 1 and stat.S_IMODE(prior.st_mode) == 0o600)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=directory)
            with os.fdopen(descriptor, 'wb') as output:
                descriptor = None
                output.write(raw); output.flush(); os.fsync(output.fileno())
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        except Exception:
            pass  # Evidence collection must not change the original refusal.
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if directory is not None:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
                os.close(directory)

    def fresh(self):
        need(self._connection is not None and not self._prepared)
        db = self._connection
        selected = db.execute('SELECT room_id,state,url,anchors,binding FROM daily_rooms '
            'WHERE day=?', (self.day,)).fetchone()
        rows = self.full_turns()
        if selected is None:
            prior = db.execute('SELECT 1 FROM daily_rooms LIMIT 1').fetchone()
            self.room_id = uuid.uuid4().hex
            prompt = self.native.user_prompt(self._payload)
            need(type(prompt) is str and prompt)
            self._binding = {'first_prompt_sha256': hashlib.sha256(
                prompt.replace('\r\n', '\n').strip().encode()).hexdigest(),
                'marker': self._payload['marker'], 'history_titles': []}
            # Claim before any New chat interaction. The first irreversible
            # Send additionally commits SENT, before its actual UI click.
            db.execute('INSERT INTO daily_rooms VALUES(?,?,?,?,?,?,?)',
                (self.day, self.room_id, 'CLAIMED', None, '[]', canonical(self._binding), self.wall_clock()))
            if prior:
                self.mode = 'NEW_DAY'
                self.composer()
                self.offset, self.prefix = 0, []
                composer = self.original_fresh()
                rows = self.full_turns()
                need(not rows)
            else:
                self.mode = 'ADOPTED_CURRENT'
                composer = self.composer()
        else:
            self.room_id, state, url, encoded, binding = selected
            anchors, self._binding = json.loads(encoded), json.loads(binding)
            need(type(anchors) is list and type(self._binding) is dict
                and state in ('CLAIMED', 'PREPARED', 'SENT', 'BOUND'))
            if state == 'CLAIMED':
                # No Send can precede PREPARED/SENT. Native New chat clears a
                # selection only; it does not create a persisted chat. Resume
                # an interrupted selection while retaining this exact claim.
                self.composer()
                prior = db.execute('SELECT 1 FROM daily_rooms WHERE day<>? LIMIT 1',
                    (self.day,)).fetchone()
                if prior and rows:
                    self.offset, self.prefix = 0, []
                    self.original_fresh()
                    rows = self.full_turns()
                    need(not rows)
                state = 'PREPARED' if not rows else 'BOUND'
                anchors = identities(rows)
            if not self.matches(rows, anchors, self._binding, state, archive_root=self.store.root,
                    prefix_identity=(self.day,self.room_id)):
                rows = self.restore(anchors, self._binding, state)
            self.mode = 'REUSED'
            composer = self.composer()
        need(not rows or rows[-1].get('role') == 'assistant'
            and rows[-1].get('complete') is True)
        self.offset, self.prefix = len(rows), identities(rows)
        if not rows:
            # A conclusive pre-Send failure may be followed by another request.
            # Bind that first actual prompt, not the abandoned draft request.
            prompt = self.native.user_prompt(self._payload)
            need(type(prompt) is str and prompt)
            self._binding.update(first_prompt_sha256=hashlib.sha256(
                prompt.replace('\r\n', '\n').strip().encode()).hexdigest(),
                marker=self._payload['marker'])
        self._binding['history_titles'] = [row['text'] for row in self.history.rows()]
        retain_prefix_snapshot(self.store.root,self.day,self.room_id,rows)
        db.execute('UPDATE daily_rooms SET state=?,url=?,anchors=?,binding=?,updated_at=? WHERE day=?',
            ('BOUND' if rows else 'PREPARED', self.bridge.page.url, canonical(self.prefix),
                canonical(self._binding), self.wall_clock(), self.day))
        self.bridge.uploaded_names = []  # Native fresh-chat upload admission, per request.
        self._prepared = True
        return composer

    def before_send(self):
        need(self._connection is not None and self._prepared)
        self._connection.execute('UPDATE daily_rooms SET state=?,updated_at=? WHERE day=?',
            ('SENT', self.wall_clock(), self.day))

    def composer(self):
        need(self.bridge.status() == 'CONNECTED')
        composers = self.original_visible(self.bridge._dom_contract.composer)
        need(len(composers) == 1 and not composers[0].input_value())
        previews = self.bridge._preview_scope(composers[0])
        need(not previews.locator('img').count()
            and not previews.locator(':scope > div.mb-2.ml-4').count()
            and not (self.bridge._dom_contract == self.native.RAIL_INPUT_GROUP
                and previews.locator("[data-slot='attachment-group'], [data-slot='attachment']").count()))
        return composers[0]

    @contextmanager
    def operation(self, payload):
        with self.lock, self.store.open() as db:
            self.day = datetime.fromtimestamp(self.wall_clock(), ZoneInfo('Asia/Jakarta')).date().isoformat()
            self._connection, self._prepared, self._payload = db, False, payload
            def checkpoint():
                if self._prepared:
                    rows = self.full_turns()
                    self.verify_prefix(rows)
                    # A partial streaming row is not a stable room anchor. Keep
                    # the already authenticated prefix so its later completion
                    # can be read without a resend or creating another room.
                    stable = rows if (not rows or rows[-1].get('role') == 'assistant'
                        and rows[-1].get('complete') is True) else rows[:self.offset]
                    self._binding['history_titles'] = [row['text'] for row in self.history.rows()]
                    retain_prefix_snapshot(self.store.root,self.day,self.room_id,stable)
                    db.execute('UPDATE daily_rooms SET state=CASE WHEN ? THEN ? ELSE state END, '
                        'anchors=?,binding=?,updated_at=? WHERE day=?',
                        (bool(stable), 'BOUND', canonical(identities(stable)), canonical(self._binding),
                            self.wall_clock(), self.day))
            try:
                yield self
            except BaseException:
                try:
                    checkpoint()
                except Exception:
                    pass  # Retain the original failure and existing room claim.
                raise
            else:
                checkpoint()
            finally:
                self._connection = None
