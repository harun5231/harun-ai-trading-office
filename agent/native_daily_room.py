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
    def matches(rows, anchors, binding, state):
        if anchors and len(rows) >= len(anchors) and identities(rows[:len(anchors)]) == anchors:
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
        return False

    def restore(self, anchors, binding, state):
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
        for row, occurrence in ordered:
            before = identities(self.full_turns())
            self.history.select(row, occurrence=occurrence)
            until = self.bridge._until(5)
            previous, stable = None, 0
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
                if self.matches(rows, anchors, binding, state):
                    return rows
                observed = identities(rows)
                stable = stable + 1 if observed == previous else 0
                previous = observed
                if observed != before and stable >= 3 and rows and rows[-1].get('complete') is True:
                    break  # Loaded another historical room; inspect no reply there.
                self.bridge.page.wait_for_timeout(50)
        self.retain_restore_failure(choices)
        raise DailyRoomUnavailable('NEUROBRO_DAILY_ROOM_NOT_FOUND')

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
            if not self.matches(rows, anchors, self._binding, state):
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
