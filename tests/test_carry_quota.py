"""Real SQLite restart and signed-fill arithmetic; no credentials or network."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from worker import carry_quota as quota
from worker.core import Review
from worker.robot_store import RobotStore

DAY='2026-10-23'
ANCHOR=1792738800000
START=quota.day_start(DAY,ANCHOR)


class Client:
    def __init__(self, positions=None, fills=None):
        self.positions=positions or []
        self.fills=fills or []
        self.anchor=ANCHOR
        self.calls=[]
        self.change=False
    def sync_time(self): pass
    def server_time_ms(self): return self.anchor
    def signed_get(self,path,symbol=None,**options):
        self.calls.append((path,symbol,options))
        if path=='/fapi/v3/positionRisk':
            rows=copy.deepcopy(self.positions)
            if self.change and len(self.calls)>1 and rows: rows[0]['updateTime']+=1
            return rows
        if path=='/fapi/v1/userTrades':
            return [copy.deepcopy(r) for r in self.fills if r['symbol']==symbol and
                    options['start_time']<=r['time']<=options['end_time']][:1000]
        raise AssertionError('Only fixed GET endpoints are allowed')


def position(amount='1',symbol='BTCUSDT'):
    return dict(symbol=symbol,positionSide='BOTH',positionAmt=amount,updateTime=START-1)


def fill(amount='1',side='BUY',symbol='BTCUSDT',at=START+1,identity=1):
    return dict(symbol=symbol,positionSide='BOTH',side=side,qty=amount,time=at,id=identity,orderId=identity)


class CarryQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'ledger.sqlite3'
        self.open()
        self.addCleanup(lambda:self.db.close())
    def open(self):
        self.db=sqlite3.connect(self.path,isolation_level=None);self.db.row_factory=sqlite3.Row
        self.store=RobotStore(self.db)
    def restart(self):
        self.db.close();self.open()
    def receipt(self,symbol='ETHUSDT',identity='first-fill',day=DAY):
        self.db.execute('INSERT INTO robot_entry_receipts VALUES(?,?,?,?)',(identity,symbol,day,'verified-fixture'))
    def test_carry_and_one_today_closed_trade_have_no_slot_even_after_carry_closes_and_restart(self):
        client=Client([position()])
        self.store.reconcile_carry(client,DAY,['BTCUSDT'])
        self.assertEqual(self.store.available_slots(1,['BTCUSDT'],DAY),1)
        self.receipt()
        self.assertEqual(self.store.available_slots(1,['BTCUSDT'],DAY),0)
        receipts=[tuple(r) for r in self.db.execute('SELECT * FROM robot_entry_receipts')]
        self.restart();client.positions=[];client.fills=[fill(side='SELL')];calls=len(client.calls)
        self.store.reconcile_carry(client,DAY,[])
        self.assertEqual(len(client.calls),calls)
        self.assertEqual(self.store.available_slots(0,[],DAY),0)
        self.assertEqual([tuple(r) for r in self.db.execute('SELECT * FROM robot_entry_receipts')],receipts)
    def test_today_open_fill_is_not_charged_twice(self):
        client=Client([position()], [fill()]);self.receipt('BTCUSDT')
        self.store.reconcile_carry(client,DAY,['BTCUSDT'])
        self.assertEqual(quota.charged_symbols(self.db,DAY),set())
        self.assertEqual(self.store.available_slots(1,['BTCUSDT'],DAY),1)
    def test_partial_close_and_sign_reversal_keep_the_midnight_charge(self):
        for rows,current in (([fill('0.4','SELL')],position('0.6')),
                             ([fill('2','SELL')],position('-1'))):
            with self.subTest(rows=rows):
                self.db.execute('DELETE FROM robot_daily_carry')
                self.store.reconcile_carry(Client([current],rows),DAY,['BTCUSDT'])
                self.assertEqual(quota.charged_symbols(self.db,DAY),{'BTCUSDT'})
    def test_tracked_carry_closed_before_startup_is_reconstructed_from_fills(self):
        self.db.execute("INSERT INTO robot_carry_symbols VALUES('BTCUSDT')")
        self.store.reconcile_carry(Client([], [fill(side='SELL')]),DAY,[])
        self.receipt()
        self.assertEqual(self.store.available_slots(0,[],DAY),0)
    def test_day_rollover_reconstructs_fresh_carry_without_deleting_yesterday(self):
        client=Client([position()]);self.store.reconcile_carry(client,DAY,['BTCUSDT'])
        self.receipt()
        client.anchor+=86400000
        self.store.reconcile_carry(client,'2026-10-24',['BTCUSDT'])
        self.assertEqual(self.store.available_slots(1,['BTCUSDT'],'2026-10-24'),1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM robot_daily_carry').fetchone()[0],2)
        self.assertEqual(self.store.entries(DAY),1)
    def test_changed_snapshot_and_invalid_history_do_not_write_proofs(self):
        for client in (Client([position()]),Client([position()],[fill(amount='-1')])):
            if not client.fills: client.change=True
            self.assertFalse(self.store.reconcile_carry(client,DAY,['BTCUSDT']))
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM robot_daily_carry').fetchone()[0],0)
            self.assertEqual(self.store.available_slots(1,['BTCUSDT'],DAY),0)
            self.assertFalse(quota.ready(self.db,DAY,['BTCUSDT']))
    def test_full_history_page_is_split_and_every_fill_used(self):
        rows=[fill('0.001',at=START+i+1,identity=i+1) for i in range(1001)]
        client=Client([position('1.001')],rows)
        self.store.reconcile_carry(client,DAY,['BTCUSDT'])
        self.assertEqual(quota.charged_symbols(self.db,DAY),set())
        self.assertGreater(sum(path=='/fapi/v1/userTrades' for path,_,_ in client.calls),1)
    def test_pending_remainder_of_carry_does_not_charge_same_symbol_twice(self):
        self.store.reconcile_carry(Client([position()]),DAY,['BTCUSDT'])
        self.receipt('BTCUSDT','carry-first-fill','2026-10-22')
        self.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
            ('owned','owned','BTCUSDT','ENTRY_PENDING','{"client_order_id":"carry-first-fill"}',None,None,'prior-day','now'))
        self.assertEqual(self.store.available_slots(1,['BTCUSDT'],DAY),1)
    def test_new_pending_same_symbol_after_carry_closed_reserves_second_daily_slot(self):
        self.store.reconcile_carry(Client([position()]),DAY,['BTCUSDT'])
        self.receipt('BTCUSDT','old-carry','2026-10-22')
        self.db.execute('INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)',
            ('new','new','BTCUSDT','ENTRY_PENDING','{"client_order_id":"new-entry"}',None,None,'today','now'))
        self.assertEqual(self.store.available_slots(0,[],DAY),0)


if __name__=='__main__': unittest.main()
