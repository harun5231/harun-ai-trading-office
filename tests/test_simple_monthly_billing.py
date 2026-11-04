"""Owner billing revisions only; no real provider, payment or trading calls."""
import copy
from pathlib import Path
import tempfile
import unittest
from worker.monthly_billing import Store, summarize, validate


def example_document(month='2027-02'):
    # Controlled samples, not owner invoices or a startup seed.
    return dict(month=month,coverage_confirmed=False,fx_idr_per_usdt=None,fx_source=None,
        cashflow=dict(payment_fee_actual_idr=None,payment_fee_estimated_idr=None,
            withdrawal_fee_actual_usdt=None,withdrawal_fee_estimated_usdt=None,withdrawal_fee_percent=None),
        simple=dict(version=1,usd_per_usdt=None,usd_rate_source=None,installment_idr=None,
            installment_due_date=None,debt_principal_idr='1000000'),bills=[
            dict(id='sample-service-'+month,name='Sample service',kind='subscription',amount='160000',currency='IDR',
                actual_idr='160000',estimated_idr=None,due_date=month+'-15',payment_url='https://billing.sample-service.com/portal',paid=False,evidence='Controlled sample'),
            dict(id='sample-credit-'+month,name='Sample credit',kind='prepaid_topup',amount='20',currency='USD',
                actual_idr=None,estimated_idr=None,due_date=month+'-15',payment_url=None,paid=False,evidence='Controlled sample')])


def ready(month='2027-02'):
    value=example_document(month)
    value.update(fx_idr_per_usdt='16000',fx_source='Owner quote fixture')
    value['simple'].update(usd_per_usdt='1',usd_rate_source='Owner USD quote fixture')
    value['cashflow'].update(payment_fee_estimated_idr='2000',withdrawal_fee_estimated_usdt='1',withdrawal_fee_percent='1')
    return value


def installment(month,amount='100000',paid=False):
    return dict(id='office-debt-'+month,name='Cicilan utang kantor',kind='debt_installment',
        amount=amount,currency='IDR',actual_idr=amount,estimated_idr=None,due_date=month+'-15',
        payment_url=None,paid=paid,evidence='Owner confirmation fixture',debt_id='office-initial')


class SimpleMonthlyBilling(unittest.TestCase):
    def test_generic_document_has_no_default_rates_fees_or_installment(self):
        value=example_document()
        validate(value)
        self.assertIsNone(value['fx_idr_per_usdt']);self.assertIsNone(value['simple']['usd_per_usdt'])
        self.assertIsNone(value['simple']['installment_idr']);self.assertIsNone(value['simple']['installment_due_date'])
        self.assertTrue(all(v is None for v in value['cashflow'].values()))
        self.assertTrue(all(row['paid'] is False for row in value['bills']))
        self.assertIsNone(summarize(value,{})['withdrawal_estimate_usdt'])

    def test_mixed_currency_unpaid_and_fees_exact_once(self):
        value=ready();validate(value)
        # 160000/16000 + 20/1 + 2000/16000 + fixed1, grossed by1%.
        self.assertEqual(summarize(value,{})['withdrawal_estimate_usdt'],'31.43939394')
        self.assertTrue(summarize(value,{})['withdrawal_estimate_complete'])
        value['bills'][1]['paid']=True
        self.assertEqual(summarize(value,{})['withdrawal_estimate_usdt'],'11.23737374')
        value['bills'][0]['paid']=True
        self.assertEqual(summarize(value,{})['withdrawal_estimate_usdt'],'0.00000000')

    def test_unknown_rate_fee_and_no_parity_assumption(self):
        value=ready();value['simple']['usd_per_usdt']=None;value['simple']['usd_rate_source']=None
        self.assertIn('Kurs USD/USDT',summarize(value,{})['withdrawal_missing'])
        self.assertIsNone(summarize(value,{})['withdrawal_estimate_usdt'])
        value=ready();value['cashflow']['withdrawal_fee_estimated_usdt']=None
        result=summarize(value,{})
        self.assertEqual(result['withdrawal_estimate_usdt'],'30.42929293')
        self.assertFalse(result['withdrawal_estimate_complete'])
        self.assertIn('Fee WD USDT',result['withdrawal_missing'])
        value=ready();value['simple']['usd_per_usdt']='0.99'
        self.assertEqual(summarize(value,{})['withdrawal_estimate_usdt'],'31.64345475')

    def test_unknown_fees_allow_provisional_base_and_only_known_fees_are_added(self):
        value=ready()
        for name in value['cashflow']:value['cashflow'][name]=None
        result=summarize(value,{})
        self.assertEqual(result['withdrawal_estimate_usdt'],'30.00000000')
        self.assertFalse(result['withdrawal_estimate_complete'])
        self.assertEqual(result['withdrawal_missing'],['Fee WD %','Fee WD USDT','Fee pembayaran IDR'])
        self.assertIsNone(result['estimated_withdrawal_fee_usdt'])
        value['cashflow']['withdrawal_fee_estimated_usdt']='1'
        self.assertEqual(summarize(value,{})['withdrawal_estimate_usdt'],'31.00000000')
        self.assertEqual(summarize(value,{})['withdrawal_missing'],['Fee WD %','Fee pembayaran IDR'])
        with tempfile.TemporaryDirectory()as directory:
            Store(directory).save(value,0)
            saved=Store(directory).read('2027-02')['document']
            self.assertIsNone(saved['cashflow']['payment_fee_estimated_idr'])
            self.assertIsNone(saved['cashflow']['withdrawal_fee_percent'])
            self.assertEqual(saved['cashflow']['withdrawal_fee_estimated_usdt'],'1')
            self.assertEqual(summarize(saved,{})['withdrawal_estimate_usdt'],'31.00000000')

    def test_owner_https_links_are_editable_without_changing_legacy_portal_contract(self):
        from worker.monthly_billing import payment_url_allowed
        value=ready();value['bills'][0]['payment_url']='https://billing.owner-vendor.com/pay/123?invoice=abc'
        validate(value)
        self.assertFalse(payment_url_allowed(value['bills'][0]['payment_url']))
        for link in ['http://billing.owner-vendor.com/pay', 'javascript:alert(1)',
                     'https://user:password@billing.owner-vendor.com/pay', 'https://localhost/pay',
                     'https://127.0.0.1/pay', 'https://10.0.0.1/pay']:
            value['bills'][0]['payment_url']=link
            with self.subTest(link=link),self.assertRaises(ValueError):validate(value)

    def test_paid_usd_needs_no_unnecessary_conversion_and_zero_fees_are_explicit(self):
        value=ready();value['bills'][1]['paid']=True
        value['simple'].update(usd_per_usdt=None,usd_rate_source=None)
        value['cashflow'].update(payment_fee_estimated_idr='0',withdrawal_fee_estimated_usdt='0',withdrawal_fee_percent='0')
        self.assertEqual(summarize(value,{})['withdrawal_estimate_usdt'],'10.00000000')
        value['fx_idr_per_usdt']=None;value['fx_source']=None
        self.assertIsNone(summarize(value,{})['withdrawal_estimate_usdt'])

    def test_debt_unique_monthly_revisions_edit_undo_delete_and_restart(self):
        with tempfile.TemporaryDirectory()as directory:
            store=Store(Path(directory));self.assertIsNone(store.debt_summary()['remaining_idr']);self.assertEqual(store.debt_summary()['principal_status'],'UNKNOWN')
            nov=ready('2027-01');nov['bills'].append(installment('2027-01'))
            store.save(nov,0)
            self.assertEqual(store.debt_summary()['remaining_idr'],'1000000')
            nov['bills'][-1]['paid']=True;store.save(nov,1)
            self.assertEqual(store.debt_summary()['remaining_idr'],'900000')
            # A new revision/ordinary invoice edit must not count the same payment twice.
            nov['bills'][0]['name']='Neurobro updated';store.save(nov,2)
            self.assertEqual(Store(Path(directory)).debt_summary()['remaining_idr'],'900000')
            dec=ready();dec['bills'].append(installment('2027-02','200000',True));store.save(dec,0)
            self.assertEqual(store.debt_summary()['remaining_idr'],'700000')
            nov['bills'][-1]['paid']=False;store.save(nov,3)
            self.assertEqual(store.debt_summary()['remaining_idr'],'800000')
            dec['bills'].pop();store.save(dec,1)
            self.assertEqual(store.debt_summary()['remaining_idr'],'1000000')
            self.assertEqual(len(store.history('2027-01')),4)
            self.assertFalse((Path(directory)/'ledger.sqlite3').exists())

    def test_debt_principal_is_authenticated_data_inherited_or_explicitly_unknown(self):
        with tempfile.TemporaryDirectory()as directory:
            store=Store(directory)
            self.assertIsNone(store.debt_summary()['principal_idr'])
            self.assertIsNone(store.debt_summary()['remaining_idr'])
            jan=example_document('2027-01');jan['bills'].append(installment('2027-01','100000',True));store.save(jan,0)
            feb=example_document('2027-02');feb['simple']['debt_principal_idr']=None;store.save(feb,0)
            state=Store(directory).debt_summary(feb)
            self.assertEqual(state['principal_idr'],'1000000')
            self.assertEqual(state['remaining_idr'],'900000')
            self.assertEqual(state['principal_status'],'AVAILABLE')
            feb['simple']['debt_principal_idr']='2000000';store.save(feb,1)
            state=store.debt_summary()
            self.assertEqual(state['principal_status'],'CONFLICT')
            self.assertIsNone(state['principal_idr']);self.assertIsNone(state['remaining_idr'])
            feb['simple']['debt_principal_idr']='1000000';store.save(feb,2)
            self.assertEqual(Store(directory).debt_summary()['remaining_idr'],'900000')

    def test_installment_wd_is_selected_month_only_no_full_principal(self):
        value=ready('2027-01');value['bills']=[installment('2027-01','500000')]
        value['cashflow'].update(payment_fee_estimated_idr='0',withdrawal_fee_estimated_usdt='0',withdrawal_fee_percent='0')
        self.assertEqual(summarize(value,{})['withdrawal_estimate_usdt'],'31.25000000')
        value['bills'][0]['due_date']='2027-02-15'
        with self.assertRaises(ValueError):validate(value)

    def test_crud_revisions_roundtrip_and_stale_seed_never_overwrites(self):
        with tempfile.TemporaryDirectory()as directory:
            store=Store(Path(directory));value=ready();first=store.save(value,0)
            self.assertEqual(store.read('2027-02'),first)
            value['bills'][0]['name']='VPS edited';store.save(value,1)
            with self.assertRaisesRegex(ValueError,'VERSION_CONFLICT'):store.save(example_document('2027-02'),0)
            value['bills'].pop();store.save(value,2)
            self.assertEqual(len(Store(Path(directory)).read('2027-02')['document']['bills']),1)
            self.assertEqual(store.read('2027-01')['version'],0)

    def test_duplicate_ids_and_incomplete_paid_installment_rejected(self):
        value=ready();value['bills'][1]['id']=value['bills'][0]['id']
        with self.assertRaisesRegex(ValueError,'INVALID_SIMPLE_BILL'):validate(value)
        value=ready();row=installment('2027-02');row['amount']=row['actual_idr']=None;row['paid']=True;value['bills'].append(row)
        with self.assertRaisesRegex(ValueError,'INCOMPLETE_PAID_BILL'):validate(value)
        value=ready();value['simple']['version']=True
        with self.assertRaisesRegex(ValueError,'INVALID_SIMPLE_BILLING'):validate(value)

    def test_topup_and_debt_are_funding_not_double_operating_expense(self):
        from tests.test_monthly_billing import document,pnl
        value=document();value['simple']=ready('2026-10')['simple']
        row=value['bills'][0];row.update(id='old-subscription',amount=row['actual_idr'],currency='IDR',estimated_idr=None)
        # Original monthly usage evidence is retained; planned top-up is funding only.
        topup=copy.deepcopy(ready()['bills'][1]);topup['due_date']='2026-10-05'
        value['bills'] += [topup,installment('2026-10','500000',True)]
        validate(value)
        result=summarize(value,pnl())
        self.assertEqual(result['actual_cost_idr'],'192000')
        self.assertEqual(result['net_profit_usdt'],'-2.00000000')
        self.assertEqual(result['trading_net_usdt'],'10')
        self.assertEqual(result['withdrawal_estimate_usdt'],'30.00000000')


if __name__=='__main__':unittest.main()
