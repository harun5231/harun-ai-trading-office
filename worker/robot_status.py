"""Read-only VPS diagnostics: existing localhost GETs, no ledger or research."""
import json
import os
import re
import stat
from pathlib import Path
from urllib.request import Request, urlopen


def safe_value(value):
    if value is None or type(value) in (bool, int):
        return value
    if isinstance(value, str) and len(value) <= 80 and re.fullmatch(r'[A-Za-z0-9_.:+/ -]+', value):
        return value
    return 'VALUE_REDACTED'


def collect(root='/data', token_path='/run/office/read_token', opener=urlopen):
    """Never initialize/migrate state, create locks, or send a POST request."""
    token = Path(token_path).read_text().strip()
    if len(token) < 32:
        raise ValueError('WORKER_AUTH_UNAVAILABLE')
    responses = {}
    for name, path in (('health', '/health'), ('provider', '/neuroapi/status'), ('robot', '/robot/status')):
        request = Request('http://127.0.0.1:8787' + path, headers={'Authorization': 'Bearer ' + token})
        with opener(request, timeout=10) as response:
            value = json.load(response)
        if not isinstance(value, dict):
            raise ValueError('WORKER_STATUS_UNAVAILABLE')
        responses[name] = value
    robot = responses['robot']
    fields = ('mode', 'robot_on', 'bot_status', 'checked_at', 'account_checked_at',
              'wait_reason', 'failure_code', 'account_failure_code', 'risk_target_usdt',
              'running_positions', 'bot_entries_today', 'available_slots',
              'usdt_wallet_balance', 'usdt_available_balance', 'live_enabled',
              'live_execution', 'would_submit', 'scheduler_enabled',
              'simulation_mode', 'manual_submission_required')
    result = {'uid': os.geteuid(), 'gid': os.getegid(),
              'health': safe_value(responses['health'].get('status')),
              'provider': safe_value(responses['provider'].get('status')),
              'robot': {key: safe_value(robot.get(key)) for key in fields}}
    exposure = robot.get('manual_exposure', [])
    result['robot']['manual_exposure'] = [symbol for symbol in exposure[:20]
        if isinstance(symbol, str) and re.fullmatch(r'[A-Z0-9_]{2,30}', symbol)] if isinstance(exposure, list) else []
    result['setups'] = []
    for row in robot.get('setups', [])[:50]:
        if not isinstance(row, dict):
            continue
        result['setups'].append({key: safe_value(row.get(key)) for key in
                                ('symbol', 'status', 'failure_code', 'risk_target_usdt', 'risk')})
    directory = Path(root) / 'trading'
    result['state_writable'] = os.access(directory, os.R_OK | os.W_OK | os.X_OK)
    result['locks'] = {}
    for name in ('migration.lock', 'cycle.lock'):
        path = directory / name
        try:
            value = path.lstat()
        except FileNotFoundError:
            result['locks'][name] = {'status': 'MISSING'}
            continue
        regular = stat.S_ISREG(value.st_mode) and value.st_nlink == 1
        result['locks'][name] = {'status': 'REGULAR' if regular else 'INVALID',
                               'owner_uid': value.st_uid,
                               'mode': format(stat.S_IMODE(value.st_mode), '04o'),
                               'writable': regular and os.access(path, os.R_OK | os.W_OK)}
    return result


def main():
    try:
        result = collect(os.getenv('OFFICE_DATA_DIR', '/data'))
    except Exception:
        print(json.dumps({'status': 'WORKER_STATUS_UNAVAILABLE'}))
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result['health'] == 'ONLINE' else 1


if __name__ == '__main__':
    raise SystemExit(main())
