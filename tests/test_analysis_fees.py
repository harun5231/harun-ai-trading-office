"""Fee evidence must reach analysis from the matching fresh account quote."""
import copy
import time
import unittest
from datetime import datetime,timezone

from support import Account,FakeMarket,RULES
from worker.analysis import account_fee_rules,analysis_context
from worker.core import D,Review


class FeeContextTests(unittest.TestCase):
    def test_per_symbol_taker_quote_attaches_to_contract_rules(self):
        account=Account();account.maker_fee='0.0002';account.taker_fee='0.0005'
        rules=account_fee_rules(RULES(),'ETHUSDT',account)
        self.assertEqual(rules.fee_symbol,'ETHUSDT')
        self.assertEqual(rules.taker_fee_rate,D('.0005'))
        self.assertEqual(rules.fee_source,'BINANCE_FUTURES_COMMISSION_RATE')
        self.assertEqual(account.calls,[('GET','/fapi/v1/commissionRate?symbol=ETHUSDT')])

    def test_no_account_reader_has_no_assumed_rate(self):
        with self.assertRaisesRegex(Review,'FEE_EVIDENCE_UNAVAILABLE'):
            account_fee_rules(RULES(),'BTCUSDT',None)

    def test_wrong_symbol_source_rate_or_clock_cannot_supply_fee_evidence(self):
        original=Account().commission_rate('BTCUSDT')
        cases=[{'symbol':'ETHUSDT'},{'source':'SPOT'},{'maker':'-0.1'},
               {'taker':0.0005},{'taker':'NaN'},{'taker':'1'},
               {'checked_at':datetime.now().isoformat()},{'checked_at_ms':True},
               {'checked_at_ms':original['checked_at_ms']-2000}]
        for changes in cases:
            quote={**copy.deepcopy(original),**changes}
            class AccountQuote:
                def commission_rate(self,symbol):return quote
            with self.subTest(changes=changes),self.assertRaises(Review):
                account_fee_rules(RULES(),'BTCUSDT',AccountQuote())

    def test_stale_or_future_matching_timestamps_are_rejected(self):
        for seconds in (-301,10):
            milliseconds=int((time.time()+seconds)*1000)
            quote=Account().commission_rate('BTCUSDT')
            quote.update(checked_at_ms=milliseconds,
                checked_at=datetime.fromtimestamp(milliseconds/1000,timezone.utc).isoformat())
            class AccountQuote:
                def commission_rate(self,symbol):return quote
            with self.subTest(seconds=seconds),self.assertRaisesRegex(Review,'STALE_FEE_EVIDENCE'):
                account_fee_rules(RULES(),'BTCUSDT',AccountQuote())

    def test_small_explicit_commission_is_preserved_in_futures_context(self):
        account=Account();account.taker_fee='0.00000001'
        context,rules=analysis_context(FakeMarket(),'BTCUSDT','7.5',account)
        self.assertEqual(context['source'],'Binance Futures')
        self.assertEqual(set(context['timeframes']),{'1h','15m'})
        self.assertEqual(context['risk_constraints']['entry_fee_rate'],'0.00000001')
        self.assertEqual(context['risk_constraints']['maximum_loss_at_sl_usdt'],'7.5')
        self.assertEqual(rules.taker_fee_rate,D('.00000001'))


if __name__=='__main__':unittest.main()
