"""Exercise the real existing Office DOM and transport without any service IO."""
import unittest

import test_dashboard_contract as dashboard_contract


AGENT = r"""
data.agent={schema_version:1,service:'harun-neurobro-agent',source:'LOCAL_AGENT_HEALTH',
status:'AVAILABLE',checked_at:now(),state:'PAUSED',phase:'IDLE',reason:'ROBOT_OFF',
integration_verified:false,dependencies:{backend:{verified:false,checked_at:null},
browser:{verified:false,checked_at:null},model:{verified:false,checked_at:null}}};
"""


class OfficeAgentDashboardTests(unittest.TestCase):
    run_dom = dashboard_contract.DashboardContract.run_dom

    def test_missing_optional_agent_does_not_break_existing_office_login_or_controls(self):
        self.run_dom(r"""
await connect();open('neurobro');
assert.match(document.getElementById('agentStatus').textContent,/Status agent belum tersedia/);
assert.equal(document.getElementById('neurobroStatus').dataset.status,'LOGIN_REQUIRED');
assert.equal(document.getElementById('neurobroLogin').disabled,false);
open('robot');assert.equal(document.getElementById('robotToggle').disabled,false);
assert.equal(document.getElementById('robotRisk').value,'5');
assert.equal(data.robot.robot_on,false);assert.equal(calls.filter(call=>call.method==='POST').length,0);
assert.equal(calls.some(call=>/127\.0\.0\.1|localhost|\/health|\/agent\//.test(call.url)),false);
""")

    def test_fresh_off_process_is_visible_without_false_model_or_integration_readiness(self):
        self.run_dom(AGENT + r"""
await connect();open('neurobro');
assert.equal(document.getElementById('agentStatus').dataset.status,'PAUSED');
assert.match(content.textContent,/Agent dijeda/);
assert.match(content.textContent,/Belum terverifikasi/);
assert.doesNotMatch(content.textContent,/Integrasi agent terverifikasi pada pemeriksaan terakhir/);
assert.equal(data.robot.robot_on,false);assert.equal(calls.filter(call=>call.method==='POST').length,0);
""")

    def test_current_complete_dependency_evidence_and_neurobro_login_are_separate(self):
        self.run_dom(AGENT + r"""
data.agent.integration_verified=true;
for(const name of ['backend','browser','model'])data.agent.dependencies[name]={verified:true,checked_at:now()};
await connect();open('neurobro');
assert.match(content.textContent,/Panggilan API terverifikasi/);
assert.doesNotMatch(content.textContent,/Integrasi agent terverifikasi pada pemeriksaan terakhir/);
data.research.status='CONNECTED';[...intervals.values()][0]();await flush();
assert.match(content.textContent,/Integrasi agent terverifikasi pada pemeriksaan terakhir/);
data.agent.dependencies.model.checked_at='2020-01-01T00:00:00Z';[...intervals.values()][0]();await flush();
assert.doesNotMatch(content.textContent,/Integrasi agent terverifikasi pada pemeriksaan terakhir/);
assert.equal(data.robot.robot_on,false);
""")

    def test_stale_or_future_agent_status_does_not_make_account_offline(self):
        self.run_dom(AGENT + r"""
data.agent.checked_at=new ClockDate(clock-60000).toISOString();await connect();open('neurobro');
assert.equal(document.getElementById('agentStatus').dataset.status,'UNAVAILABLE');
assert.match(content.textContent,/Status agent perlu diperbarui/);assert.equal(statValues()[0],'116.93');
data.agent.checked_at=new ClockDate(clock+6000).toISOString();[...intervals.values()][0]();await flush();
assert.equal(document.getElementById('agentStatus').dataset.status,'UNAVAILABLE');
assert.equal(document.getElementById('neurobroLogin').disabled,false);
""")

    def test_untrusted_optional_telemetry_never_renders_html_or_secret_fields(self):
        self.run_dom(AGENT + r"""
data.agent.state=['RUNNING'];data.agent.reason='<img src=x onerror="pwned=true">';
data.agent.api_key='DO_NOT_RENDER_SECRET';await connect();open('neurobro');
assert.equal(document.getElementById('agentStatus').dataset.status,'UNAVAILABLE');
assert.doesNotMatch(content.textContent,/DO_NOT_RENDER_SECRET|onerror|pwned/);
assert.equal(walk(content).some(node=>node.tagName==='IMG'),false);
assert.equal(document.getElementById('neurobroLogin').disabled,false);
""")

    def test_disconnect_clears_optional_agent_evidence_and_worker_snapshot_stays_unchanged(self):
        self.run_dom(AGENT + r"""
await connect();open('neurobro');
assert.equal(snapshots.at(-1).robot_on,false);assert.equal(Object.hasOwn(snapshots.at(-1),'agent'),false);
assert.deepEqual(snapshots.at(-1).employees,data.employees);
document.getElementById('apiDisconnect').click();
assert.equal(document.getElementById('agentStatus').dataset.status,'UNAVAILABLE');
assert.equal(document.getElementById('neurobroLogin').disabled,true);
assert.equal(snapshots.at(-1).status,'OFFLINE');assert.equal(calls.filter(call=>call.method==='POST').length,0);
""")


if __name__ == '__main__':
    unittest.main()
