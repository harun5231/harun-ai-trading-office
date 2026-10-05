import concurrent.futures
from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from worker.core import Ledger,Rules,Review,Locked,Signal,risk_check,preflight
from worker.monitor import PaperMonitor
from worker.prompts import SCREENING,ANALYSIS

class WorkerTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)
  self.ledger=Ledger(self.path/'ledger.db')
  self.rules=Rules(D('.001'),D('.001'),D('10000'),D('.01'),D('5'),time.time())
  self.signal=Signal('BTCUSDT','LONG',D('100'),D('104'),D('98'),D('2.5'))
 def tearDown(self): self.ledger.db.close();self.tmp.cleanup()
 def plan(self): return risk_check(self.signal,self.rules)
 def test_exact_prompts(self):
  self.assertEqual(SCREENING,'pilihkan 2 coin yang bagus dan rate tinggi mandapatkan profit saat ini di future market binance')
  self.assertEqual(ANALYSIS,'Aku berikan data chart realtime saat ini 2 time frame 1 jam dan 15 menit, silahkan analisa dengan akurat dan Profitable. aku mau entry di time frame 15 menit untuk scalping. Tentukan ! Ukuran posisi ENTRY TP SL : yang tidak mudah terkena wick atau di jilat para bandar. aku bermain di cross, aku hanya bisa resikokan 5 usdt per 1 kali SL RISK REWARD 1:2')
  self.assertNotIn('75',ANALYSIS)
 def test_risk_rejects_instead_of_shrinking(self):
  p=self.plan();self.assertEqual(D(p['quantity']),D('2.5'));self.assertEqual(D(p['risk']),D('5'))
  with self.assertRaises(Review):risk_check(replace(self.signal,quantity=D('10')),self.rules)
  self.assertEqual(p['leverage'],75);self.assertEqual(p['margin_mode'],'CROSS')
  self.assertEqual(D(risk_check(replace(self.signal,quantity=D('1')),self.rules)['quantity']),D('1'))
 def test_precision_rejection_and_short(self):
  p=risk_check(Signal('BTCUSDT','SHORT',D('100'),D('94'),D('103'),D('1')),self.rules)
  self.assertEqual(p['quantity'],'1');self.assertEqual(D(p['risk']),D('3'))
  with self.assertRaises(Review):risk_check(replace(self.signal,quantity=D('1.0001')),self.rules)
  with self.assertRaises(Review):risk_check(replace(self.signal,quantity=None),self.rules)
 def test_bad_risk(self):
  for sig in [replace(self.signal,sl=D('100')),replace(self.signal,sl=D('101')),replace(self.signal,tp=D('103')),replace(self.signal,entry=D('100.001'))]:
   with self.assertRaises(Review):risk_check(sig,self.rules)
  with self.assertRaises(Review):risk_check(self.signal,replace(self.rules,minimum=D('3')))
  with self.assertRaises(Review):risk_check(self.signal,replace(self.rules,min_notional=D('300')))
  with self.assertRaises(Review):risk_check(self.signal,replace(self.rules,observed_at=time.time()-301))
 def test_preflight_tamper_and_live(self):
  for key,value in [('mode','LIVE'),('leverage',74),('margin_mode','ISOLATED'),('risk','1'),('quantity','5'),('side','BUY')]:
   p=self.plan();p[key]=value
   with self.assertRaises(Review):preflight(p,self.rules)
 def test_limit_persistence_and_duplicate(self):
  self.ledger.reserve(self.plan(),'FIXTURE',self.rules)
  with self.assertRaises(Review):self.ledger.reserve(self.plan(),'FIXTURE',self.rules)
  other=self.plan();other['symbol']='ETHUSDT';self.ledger.reserve(other,'FIXTURE',self.rules)
  reopened=Ledger(self.path/'ledger.db')
  with self.assertRaises(Locked): reopened.reserve(self.plan(),'FIXTURE',self.rules)
  reopened.db.close()
 def test_atomic_race(self):
  def reserve(i):
   db=Ledger(self.path/'ledger.db');p=self.plan();p['symbol']=f'C{i}USDT'
   try: db.reserve(p,'FIXTURE',self.rules);return True
   except Locked:return False
   finally:db.db.close()
  with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool: result=list(pool.map(reserve,range(5)))
  self.assertEqual(sum(result),2);self.assertEqual(self.ledger.count(),2)
 def test_paper_monitor_and_gap(self):
  trade=self.ledger.reserve(self.plan(),'FIXTURE',self.rules);m=PaperMonitor(self.ledger)
  self.assertEqual(m.quote(trade,'BTCUSDT','101','FIXTURE'),[])
  with self.assertRaises(Review):m.quote(trade,'ETHUSDT','100','FIXTURE')
  with self.assertRaises(Review):m.quote(trade,'BTCUSDT','100','WRONG_SOURCE')
  self.assertEqual(m.quote(trade,'BTCUSDT','100','FIXTURE'),['POSITION_OPEN','MONITORING'])
  self.assertEqual(m.quote(trade,'BTCUSDT','97','FIXTURE'),['CLOSED'])
  self.assertEqual(D(self.ledger.snapshot('FIXTURE')['pnl_today']),D('-7.5'))
  self.assertEqual(m.quote(trade,'BTCUSDT','97','FIXTURE'),[])
 def test_close_day_accounting_and_reset(self):
  with patch('worker.core.day',return_value='2026-10-04'):tid=self.ledger.reserve(self.plan(),'FIXTURE',self.rules)
  self.ledger.fill(tid)
  with patch('worker.core.day',return_value='2026-10-05'):
   self.ledger.close(tid,'104');snapshot=self.ledger.snapshot('FIXTURE')
   self.assertEqual(snapshot['trades_today'],0);self.assertEqual(D(snapshot['pnl_today']),D('10'))

if __name__=='__main__':unittest.main()
