import concurrent.futures
from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from worker.core import Ledger,Rules,Review,Locked,Signal,coins,parse_signal,risk_check,preflight
from worker.workflow import Workflow,FixtureAdapter
from worker.monitor import PaperMonitor
from worker.prompts import SCREENING,ANALYSIS

class WorkerTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)
  self.ledger=Ledger(self.path/'ledger.db')
  self.rules=Rules(D('.001'),D('.001'),D('10000'),D('.01'),D('5'),time.time())
  self.signal=Signal('BTCUSDT','LONG',D('100'),D('104'),D('98'),D('10'))
 def tearDown(self): self.ledger.db.close();self.tmp.cleanup()
 def plan(self): return risk_check(self.signal,self.rules)
 def test_exact_prompts(self):
  self.assertEqual(SCREENING,'pilihkan 2 coin yang bagus dan rate tinggi mandapatkan profit saat ini di future market binance')
  self.assertEqual(ANALYSIS,'Aku berikan screenshot chart 2 time frame 1 jam dan 15 menit, silahkan analisa dengan akurat dan Profitable. aku mau entry di time frame 15 menit untuk scalping.\n Tentukan !\n Ukuran posisi\n ENTRY\n TP\n SL : yang tidak mudah terkena wick atau di jilat para bandar.\n aku bermain di cross, aku hanya bisa resikokan 5 usdt per 1 kali SL\n RISK REWARD 1:2')
  self.assertNotIn('75',ANALYSIS)
 def test_screening_requires_explicit_two(self):
  self.assertEqual(coins('1. BTCUSDT\n2. ETHUSDT'),['BTCUSDT','ETHUSDT'])
  for text in ['BTCUSDT','BTCUSDT\nBTCUSDT','BTCUSDT\nETHUSDT\nSOLUSDT','Jangan pilih BTCUSDT dan ETHUSDT']:
   with self.assertRaises(Review): coins(text)
 def test_parse_and_ambiguity(self):
  text='Symbol: BTCUSDT\nSide: LONG\nENTRY: 100\nTP: 104\nSL: 98\nUkuran posisi: 10 BTC'
  self.assertEqual(parse_signal(text,'BTCUSDT'),self.signal)
  for bad in [text.replace('100','100-101'),text.replace('SL: 98',''),text.replace('LONG','LONG atau SHORT'),text+'\nTP: 106',text.replace('10 BTC','10'),text.replace('100','1,000'),text.replace('BTCUSDT','ETHUSDT'),text+'\nTP2: 106']:
   with self.assertRaises(Review): parse_signal(bad,'BTCUSDT')
  with self.assertRaises(Review):parse_signal('{"symbol":"BTCUSDT","symbol":"ETHUSDT"}','BTCUSDT')
 def test_risk_shrinks_and_leverage_does_not_enter_formula(self):
  p=self.plan();self.assertEqual(D(p['quantity']),D('2.5'));self.assertEqual(D(p['risk']),D('5'))
  self.assertEqual(p['leverage'],75);self.assertEqual(p['margin_mode'],'CROSS')
  self.assertEqual(D(risk_check(replace(self.signal,quantity=D('1')),self.rules)['quantity']),D('1'))
 def test_floor_and_short(self):
  p=risk_check(Signal('BTCUSDT','SHORT',D('100'),D('94'),D('103')),self.rules)
  self.assertEqual(p['quantity'],'1.666');self.assertLessEqual(D(p['risk']),D('5'))
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
  with self.assertRaises(Review):m.quote(trade,'BTCUSDT','100','BROWSER_DRY_RUN')
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
 def test_end_to_end_fixture_and_locked_rerun(self):
  a=FixtureAdapter(self.path/'artifacts');w=Workflow(self.ledger,a,self.path/'snapshot.json');s=w.run()
  self.assertEqual(s['status'],'CLOSED');self.assertTrue(s['locked']);self.assertEqual(s['trades_today'],2)
  self.assertEqual(s['active_positions'],0);self.assertEqual(D(s['pnl_today']),D('20'))
  self.assertEqual(s['source'],'FIXTURE');self.assertFalse(s['live_enabled'])
  states=[e['state'] for e in s['events']]
  self.assertEqual(states.count('CAPTURE_1H'),2);self.assertEqual(states.count('CAPTURE_15M'),2)
  self.assertEqual(states.count('NEUROBRO_ANALYSIS'),2)
  s=Workflow(self.ledger,a,self.path/'snapshot.json').run();self.assertEqual(s['status'],'ERROR');self.assertEqual(s['trades_today'],2)
 def test_incomplete_signal_stops_without_order(self):
  a=FixtureAdapter(self.path/'artifacts');a.analyze=lambda *args:'ENTRY: 100'
  s=Workflow(self.ledger,a,self.path/'snapshot.json').run()
  self.assertEqual(s['status'],'ERROR');self.assertEqual(s['trades_today'],0)
 def test_capture_mismatch_and_tampering(self):
  a=FixtureAdapter(self.path/'artifacts');cap=a.capture('BTCUSDT','1h')
  with self.assertRaises(Review):cap.verify('ETHUSDT','1h','FIXTURE')
  with self.assertRaises(Review):cap.verify('BTCUSDT','15m','FIXTURE')
  with self.assertRaises(Review):replace(cap,captured_at=time.time()-601).verify('BTCUSDT','1h','FIXTURE')
  Path(cap.path).write_text('modified')
  with self.assertRaises(Review):cap.verify('BTCUSDT','1h','FIXTURE')
 def test_illegal_transition(self):
  w=Workflow(self.ledger,FixtureAdapter(self.path/'artifacts'),self.path/'snapshot.json')
  with self.assertRaises(Review):w.go('POSITION_OPEN','bad')

if __name__=='__main__':unittest.main()
