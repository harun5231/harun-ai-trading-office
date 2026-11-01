"""Read-only auth notices use actual browser facts, not a generic outage."""
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent import native_status_bridge as status
import test_dashboard_contract as dashboard


NOW = 1792410000


def research(state, age=0):
    return {'provider': 'NEUROBRO_WEB', 'status': state,
        'checked_at': datetime.fromtimestamp(NOW-age, timezone.utc).isoformat(),
        'profile_persistent': True}


class OwnerNoticeTests(unittest.TestCase):
    def test_login_and_human_verification_are_distinct_and_transport_is_not_login(self):
        login = status.neurobro_owner_action(research('LOGIN_REQUIRED'), now=NOW)
        verify = status.neurobro_owner_action(research('CHALLENGE_REQUIRED'), now=NOW)
        self.assertEqual(login['label'], 'MENUNGGU LOGIN NEUROBRO')
        self.assertEqual(verify['label'], 'MENUNGGU VERIFIKASI MANUSIA')
        self.assertEqual((login['action'], verify['action']), ('LOGIN', 'VERIFY'))
        self.assertIsNone(status.neurobro_owner_action(research('UNAVAILABLE'), now=NOW))
        self.assertIn('SELESAI LOGIN', verify['instructions'])

    def test_notice_survives_manual_or_stale_transport_until_newer_real_ready_fact(self):
        previous = status.neurobro_owner_action(research('CHALLENGE_REQUIRED'), now=NOW)
        for state in ('LOGIN_IN_PROGRESS', 'UNAVAILABLE'):
            self.assertEqual(status.neurobro_owner_action(research(state), previous=previous,
                now=NOW+600), previous)
        self.assertEqual(status.neurobro_owner_action(research('CONNECTED', age=1),
            previous=previous, now=NOW), previous)
        self.assertIsNone(status.neurobro_owner_action(research('CONNECTED'),
            previous=previous, now=NOW))

    def test_office_snapshot_does_not_change_robot_choice_or_native_status(self):
        robot = {'robot_on': True, 'bot_status': 'WAITING', 'wait_reason': 'NEUROBRO_UNAVAILABLE'}
        original = {'robot': robot, 'research': research('CHALLENGE_REQUIRED')}
        namespace = {'office_snapshot': lambda controller, result: result}
        status.install_office_status(namespace,
            control=SimpleNamespace(native_agent_status=lambda: status.unavailable()))
        with patch.object(status.time, 'time', return_value=NOW):
            value = namespace['office_snapshot'](None, original)
        self.assertIs(value['robot'], robot)
        self.assertEqual(value['research'], original['research'])
        self.assertTrue(value['robot']['robot_on'])
        self.assertEqual(value['research_owner_action']['action'], 'VERIFY')
        self.assertNotIn('research_owner_action', original)


class OwnerNoticeDashboardTests(unittest.TestCase):
    run_dom = dashboard.DashboardContract.run_dom

    def test_robot_on_verify_notice_opens_existing_login_panel_without_mutation(self):
        self.run_dom(r"""
data.robot.robot_on=true;data.robot.bot_status='WAITING';data.robot.wait_reason='NEUROBRO_UNAVAILABLE';
data.research.status='CHALLENGE_REQUIRED';await connect();
assert.match(document.getElementById('robotStatus').textContent,/BOT STATUS: MENUNGGU VERIFIKASI MANUSIA/);
assert.match(document.getElementById('robotStatus').textContent,/Diperiksa/);
assert.match(document.getElementById('robotToggle').textContent,/ON/);
assert.match(content.textContent,/SELESAI LOGIN/);
document.getElementById('robotNeurobroAction').click();
assert.equal(document.getElementById('panelTitle').textContent,'LOGIN NEURO');
assert.equal(data.robot.robot_on,true);assert.equal(calls.filter(call=>call.method==='POST').length,0);
""")

    def test_auth_notice_remains_during_outage_and_clears_only_on_fresh_ready_chat(self):
        self.run_dom(r"""
data.robot.robot_on=true;data.robot.bot_status='WAITING';data.research.status='CHALLENGE_REQUIRED';await connect();
data.research.status='UNAVAILABLE';[...intervals.values()][0]();await flush();
assert.match(document.getElementById('robotStatus').textContent,/MENUNGGU VERIFIKASI MANUSIA/);
data.research.status='CONNECTED';clock+=90000;data.generated_at=now();[...intervals.values()][0]();await flush();
assert.match(document.getElementById('robotStatus').textContent,/MENUNGGU VERIFIKASI MANUSIA/);
data.research.checked_at=now();[...intervals.values()][0]();await flush();
assert.doesNotMatch(document.getElementById('robotStatus').textContent,/MENUNGGU (LOGIN|VERIFIKASI)/);
assert.equal(data.robot.robot_on,true);assert.equal(calls.filter(call=>call.method==='POST').length,0);
""")

    def test_internal_unavailability_does_not_fabricate_login_or_turn_robot_off(self):
        self.run_dom(r"""
data.robot.robot_on=true;data.robot.bot_status='WAITING';data.robot.wait_reason='NEUROBRO_UNAVAILABLE';
data.research.status='UNAVAILABLE';await connect();
assert.doesNotMatch(document.getElementById('robotStatus').textContent,/MENUNGGU (LOGIN|VERIFIKASI)/);
assert.equal(document.getElementById('robotNeurobroAction'),null);
assert.match(document.getElementById('robotStatus').textContent,/NEUROBRO_UNAVAILABLE/);
assert.equal(data.robot.robot_on,true);assert.equal(calls.filter(call=>call.method==='POST').length,0);
""")

    def test_monitoring_estimate_shows_actual_difference_without_mutating_order_or_owner_choice(self):
        self.run_dom(r"""
data.research.status='CONNECTED';data.robot.robot_on=false;
data.robot.risk_estimates={'hao-old':{kind:'MONITORING_ESTIMATE_ONLY',source:'RISK_MANAGER',status:'AVAILABLE',
symbol:'BTCUSDT',client_order_id:'hao-old',observed_at:now(),estimated_total_risk:'5.85',
original_risk_target_usdt:'5',estimated_budget_difference_usdt:'0.85',order_parameters_changed:false,
components:{gross_risk:'5',entry_fee_usdt:'0.1',sl_exit_fee_usdt:'0.1',sl_book_impact_usdt:'0.15',sl_liquidity_uncertainty_usdt:'0.5'}}};
await connect();assert.match(content.textContent,/ESTIMASI RISIKO BTCUSDT/);
assert.match(content.textContent,/selisih estimasi 0[.,]85/);assert.match(content.textContent,/fee entry/);
assert.match(content.textContent,/parameter order tidak diubah/);
assert.equal(data.robot.robot_on,false);assert.equal(calls.filter(call=>call.method==='POST').length,0);
""")

    def test_each_position_reports_confirmed_ids_without_claiming_incomplete_protection(self):
        self.run_dom(r"""
data.research.status='CONNECTED';data.robot.robot_on=true;data.robot.bot_status='POSITION_PROTECTED';
data.robot.protection_observations={'hao-one':{source:'BINANCE_FUTURES',state:'PROTECTION_INCOMPLETE',
symbol:'BTCUSDT',client_order_id:'hao-one',order_id:'11',filled_quantity:'0.4',
sl_confirmed:true,sl_order_id:'22',tp_confirmed:false,tp_order_id:null,observed_at:now()}};
await connect();assert.match(document.getElementById('robotStatus').textContent,/PROTEKSI BELUM LENGKAP: SL TERPASANG, TP BELUM TERKONFIRMASI/);
assert.doesNotMatch(content.textContent,/POSISI TERLINDUNGI: SL DAN TP TERKONFIRMASI/);
const row=data.robot.protection_observations['hao-one'];row.tp_confirmed=true;row.tp_order_id='33';row.state='POSITION_PROTECTED';
[...intervals.values()][0]();await flush();assert.match(content.textContent,/BTCUSDT · POSISI TERLINDUNGI: SL DAN TP TERKONFIRMASI/);
row.tp_order_id='22';[...intervals.values()][0]();await flush();assert.match(content.textContent,/TP BELUM TERKONFIRMASI/);
assert.doesNotMatch(content.textContent,/POSISI TERLINDUNGI: SL DAN TP TERKONFIRMASI/);
assert.equal(calls.filter(call=>call.method==='POST').length,0);
""")
