"""Priority reconciliation of owned entries, independent of research/login.

Uses the existing gateway and frozen journal intents. This actor never calls
submit, configures leverage, obtains charts, or asks either language model.
"""
import copy
from datetime import datetime, timezone
import json
import queue
import threading
import time

from .fill_priority import FillPriority, FillEventError, owned_intent_lock
from .fill_stream import UserDataFeed


ACTIVE = ('ENTRY_PENDING', 'POSITION_PROTECTED', 'PROTECTION_INCOMPLETE',
    'NEEDS_REVIEW', 'SUBMITTING')


class ProtectionActor:
    def __init__(self, controller, coordinator_factory, gateway_factory, *,
            feed_factory=UserDataFeed, monotonic=time.monotonic, wall_clock=time.time):
        self.controller = controller
        self.coordinator_factory, self.gateway_factory = coordinator_factory, gateway_factory
        self.feed_factory, self.monotonic, self.clock = feed_factory, monotonic, wall_clock
        self.events = queue.Queue(maxsize=1000)
        self.wakeup, self.recovery = threading.Event(), threading.Event()
        self.account_recovery = threading.Event()
        self.recovery.set(); self.account_recovery.set()
        self.progress_at = monotonic()
        self.checked_at = None
        self.status = 'PROTECTION_INITIALIZING'
        self.feed_status = 'USER_DATA_CONNECTING'
        self.cost_history_status = 'NO_CONFIRMED_EXIT_FILLS'
        self.feed_thread = None

    def request_recovery(self):
        self.recovery.set(); self.account_recovery.set()
        self.wakeup.set()
        account_wakeup = getattr(self.controller, 'account_wakeup', None)
        if account_wakeup is not None:
            account_wakeup.set()

    def on_event(self, event):
        try:
            self.events.put_nowait(copy.deepcopy(event))
        except queue.Full:
            self.request_recovery()  # API reconciliation covers a missed update.
        self.wakeup.set()

    def _owned(self, *, client=None, symbol=None, coordinator=None):
        coordinator = coordinator or self.coordinator
        db = coordinator.db
        marks = ','.join('?' for _ in ACTIVE)
        for row in list(db.execute('SELECT * FROM order_intents WHERE state IN (' + marks + ')', ACTIVE)):
            try:
                intent = coordinator.verified_intent(row)
                if client is not None and intent['client_order_id'] != client:
                    continue
                if symbol is not None and intent['symbol'] != symbol:
                    continue
                yield row, intent
            except Exception:
                self.status = 'PROTECTION_INTENT_REVIEW_REQUIRED'

    def recover_pending(self, coordinator, gateway):
        """Existing account actor performs GET recovery without delaying fills."""
        priority = FillPriority(coordinator.db, wall_clock=self.clock)
        for row, intent in self._owned(coordinator=coordinator):
            try:
                gateway._validate_intent(intent)
                entry = gateway._entry(intent)  # Existing GET proof only.
                priority.enqueue(intent, gateway._id(entry['orderId']),
                    entry['executedQty'], recovery=True)
                self.wakeup.set()
            except Exception:
                self.status = 'PROTECTION_RECONCILIATION_REQUIRED'

    def _consume(self):
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            order = event.get('o') if isinstance(event, dict) else None
            if not isinstance(order, dict):
                continue
            owned = list(self._owned(client=order.get('c')))
            if not owned:
                if any(self._owned(symbol=order.get('s'))):
                    self.recovery.set()  # Includes owned exits/partial remainder.
                continue
            row, intent = owned[0]
            if order.get('x') != 'TRADE':
                self.recovery.set()
                continue
            try:
                self.priority.admit(event, intent)
                sample = {key: order.get(key) for key in ('i', 't', 'L', 'l', 'n', 'N', 'T', 'z')}
                sample['detected_at'] = self.clock()
                self.db.execute('INSERT OR IGNORE INTO entry_fill_samples VALUES(?,?,?,?,0)',
                    (intent['client_order_id'], str(order['i']), str(order['t']), json.dumps(sample)))
            except (FillEventError, KeyError, TypeError, ValueError):
                self.recovery.set()

    def _reconcile(self, intent):
        with owned_intent_lock(self.db, intent['client_order_id']):
            row = self.db.execute('SELECT * FROM order_intents WHERE id=?', (intent['intent_id'],)).fetchone()
            current = self.coordinator.verified_intent(row)
            if current != intent:
                raise FillEventError('FILL_FROZEN_INTENT_CHANGED')
            result = self.gateway.reconcile_protection(intent)
            row = self.db.execute('SELECT detected_at FROM fill_priority WHERE client_id=?',
                (intent['client_order_id'],)).fetchone()
            result['fill_detected_at'] = datetime.fromtimestamp(row[0], timezone.utc).isoformat()
            return result

    def _record(self, operation, result):
        row = self.db.execute('SELECT candidate_id FROM order_intents WHERE id=?', (operation,)).fetchone()
        return self.coordinator.record_gateway_observation(operation, row[0], result, None)

    def _samples(self):
        self.cost_history_status = getattr(self.gateway, 'execution_observation_status',
            'CONFIRMED_EXIT_COST_DETAILS_UNAVAILABLE')
        # No price/cost work precedes protection. Only acknowledged owned fills
        # are recorded after reconciliation. Entry LIMIT executions have no
        # comparable closing-book impact and never calibrate closing slippage.
        from .dynamic_market import record_execution
        for client, order_id, trade_id, raw in self.db.execute(
                'SELECT client_id,order_id,trade_id,body FROM entry_fill_samples WHERE recorded=0'):
            row = self.db.execute('SELECT payload,last_result FROM fill_priority WHERE client_id=?',
                (client,)).fetchone()
            if row is None or not row[1]:
                continue
            intent, result, sample = json.loads(row[0]), json.loads(row[1]), json.loads(raw)
            if result.get('order_id') != order_id or result.get('source') != 'BINANCE_FUTURES':
                continue
            from decimal import Decimal
            if Decimal(result['filled_quantity']) < Decimal(sample['z']):
                continue
            try:
                record_execution(self.db, symbol=intent['symbol'], side=intent['entry']['side'],
                    order_id=order_id, execution_id=trade_id, filled_at=sample['T'] / 1000,
                    detected_at=sample['detected_at'], reference_price=intent['entry']['price'],
                    fill_price=sample['L'], quantity=sample['l'], expected_impact_rate=None,
                    fee_amount=sample['n'], fee_asset=sample['N'])
            except Exception:
                self.status = 'EXECUTION_COST_OBSERVATION_REVIEW_REQUIRED'
                recorded = -1
            else:
                recorded = 1
            self.db.execute('UPDATE entry_fill_samples SET recorded=? WHERE client_id=? AND trade_id=? AND order_id=?',
                (recorded, client, trade_id, order_id))

    def step(self):
        self._consume()
        if self.recovery.is_set():
            self.recovery.clear()
            self.account_recovery.set()
            self.controller.account_wakeup.set()
        self.db.execute("UPDATE fill_priority SET state='COMPLETE',claim=NULL WHERE state='PENDING' "
            "AND EXISTS(SELECT 1 FROM order_intents o WHERE o.id=fill_priority.intent_id AND o.state IN ('CLOSED','REJECTED'))")
        while True:
            result = self.priority.run_one(self._reconcile, self._record)
            if result is None:
                break
            self.status = result['state']
            self._samples()
            if result.get('retry_required'):
                break  # Next event/wakeup retries; no analysis scheduler delay.
        self.progress_at = self.monotonic()
        self.checked_at = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()

    def run(self):
        ledger = None
        try:
            ledger = self.controller.open_ledger()
            self.db = ledger.db
            self.gateway = self.gateway_factory()
            self.coordinator = self.coordinator_factory(ledger, None, None, None,
                stopping=self.controller.stopping, gateway=self.gateway)
            self.priority = FillPriority(self.db, wall_clock=self.clock)
            self.priority.recover_interrupted()
            self.db.execute('CREATE TABLE IF NOT EXISTS entry_fill_samples(client_id TEXT NOT NULL,'
                'order_id TEXT NOT NULL,trade_id TEXT NOT NULL,body TEXT NOT NULL,recorded INTEGER NOT NULL,'
                'PRIMARY KEY(client_id,order_id,trade_id))')
            feed = self.feed_factory(self.gateway.api_key, self.on_event, self.request_recovery,
                on_status=lambda code: setattr(self, 'feed_status', code))
            self.feed_thread = threading.Thread(target=feed.run,
                args=(self.controller.stopping,), daemon=True)
            self.feed_thread.start()
            while not self.controller.stopping.is_set():
                try:
                    self.step()
                except Exception:
                    self.status = 'PROTECTION_RECONCILIATION_REQUIRED'
                    self.progress_at = self.monotonic()
                self.wakeup.wait(1)
                self.wakeup.clear()
        except Exception:
            self.status = 'WORKER_PROTECTION_MONITOR_UNAVAILABLE'
            self.controller.failure('WORKER_PROTECTION_MONITOR_UNAVAILABLE')
        finally:
            if ledger is not None:
                ledger.db.close()
