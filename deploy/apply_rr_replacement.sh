#!/usr/bin/env bash
# Apply only after the existing ETH entry and both protective orders are verified.
(
set -Eeuo pipefail
umask 077
cd /root/harun-ai-trading-office
HARUN_STAGE=PREFLIGHT
HARUN_RESTORE_READY=0
HARUN_WORKER_STOPPED=0

HARUN_CONFIG="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' harun-office-worker-1)"
[ "$HARUN_CONFIG" = /root/harun-ai-trading-office/compose.yaml ] || { printf '%s\n' COMPOSE_CONFIGURATION_CHANGED; exit 1; }
HARUN_BACKUP="$(mktemp -d /root/harun-rr-target-last-price.XXXXXX)"

cat > "$HARUN_BACKUP/check.py" <<'HARUN_CHECK'
import hashlib, json, sqlite3, sys
from pathlib import Path
expected=json.loads(sys.argv[1]);root=Path(sys.argv[2])
for name,value in expected.items():
    p=root/name
    assert p.is_file() and not p.is_symlink() and hashlib.sha256(p.read_bytes()).hexdigest()==value, 'SOURCE_MISMATCH:'+name
if len(sys.argv)>3:
    db=sqlite3.connect('file:/data/trading/ledger.sqlite3?mode=ro',uri=True)
    db.execute('PRAGMA query_only=ON')
    assert not db.execute("SELECT 1 FROM order_intents WHERE state IN ('SUBMITTING','NEEDS_REVIEW') LIMIT 1").fetchone(), 'UNRESOLVED_ORDER'
    for table in ('api_requests','robot_jobs'):
        assert not db.execute('SELECT 1 FROM '+table+" WHERE state IN ('PENDING','NEEDS_REVIEW') LIMIT 1").fetchone(), 'UNRESOLVED_RESEARCH'
    db.close()
print('SOURCE_MATCH')
HARUN_CHECK

HARUN_OLD_MAP="$(python3 -I -B -S - <<'HARUN_MAP'
import hashlib,json
from pathlib import Path
paths=list(Path('worker').rglob('*.py'))+[Path('deploy/container_boot.py'),Path('deploy/runtime_permissions.py')]
assert all(p.is_file() and not p.is_symlink() for p in paths), 'SOURCE_NOT_REGULAR'
values={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
for name,value in {'worker/core.py': '11df540717e6e89a7da2777fa373723562f4ca05b87e3540c36c1902dd7cf9df', 'worker/robot.py': '4bb72c65d8eb3bada7c3739058e270cf648d129de64d2042ecee90a4627bbbef', 'worker/robot_provenance.py': 'f539eecb35ca57f8a8f91e9cbbcae2900f31e9dcfcd72246070d3d967582cb15', 'worker/analysis.py': 'f9712ce490f99061a45e32962d6489dd372a74aa18213001cc5d167208543a6d', 'worker/diagnostics.py': 'cf6802f0b5692e69f6573a36509828c70cdf226acd1894aff3b068a9c98fe190'}.items():
    assert values[name]==value, 'SOURCE_CHANGED:'+name
assert values['worker/order_gateway.py']=='7714469daa3c529ee228d8e049cea3b3a52484a46a8429b2b31e9e233573c422', 'GATEWAY_SOURCE_CHANGED'
print(json.dumps(values,sort_keys=True))
HARUN_MAP
)"
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B -S - "$HARUN_OLD_MAP" /app preflight < "$HARUN_BACKUP/check.py"
docker compose -f compose.yaml config --quiet
for HARUN_NAME in robot core robot_provenance analysis diagnostics order_gateway; do
  cp -p "worker/$HARUN_NAME.py" "$HARUN_BACKUP/$HARUN_NAME.before.py"
done
HARUN_OLD_IMAGE="$(docker inspect --format '{{.Image}}' harun-office-worker-1)"
HARUN_IMAGE_BACKUP="harun-office-worker:before-rr-target-last-$(date -u +%Y%m%dT%H%M%SZ)"
docker image tag "$HARUN_OLD_IMAGE" "$HARUN_IMAGE_BACKUP"
printf 'Backup: %s\nImage backup: %s\n' "$HARUN_BACKUP" "$HARUN_IMAGE_BACKUP"

harun_restore_source_image() {
  for HARUN_NAME in robot core robot_provenance analysis diagnostics order_gateway; do
    cp -p "$HARUN_BACKUP/$HARUN_NAME.before.py" "worker/$HARUN_NAME.py" || return 1
  done
  docker image tag "$HARUN_OLD_IMAGE" harun-office-worker:latest
}
harun_abort() {
  local HARUN_STATUS="$1"
  trap - ERR INT TERM
  if [ "$HARUN_RESTORE_READY" = 1 ]; then
    if harun_restore_source_image; then
      printf '%s\n' SOURCE_AND_IMAGE_RESTORED
      if [ "$HARUN_WORKER_STOPPED" = 1 ]; then
        docker compose -f compose.yaml up -d --no-build --no-deps --force-recreate --wait --wait-timeout 180 worker || printf '%s\n' OLD_IMAGE_RESTART_FAILED
      fi
    else
      printf '%s\n' RESTORE_FAILED
    fi
  fi
  printf 'DEPLOY_NOT_COMPLETED stage=%s backup=%s\n' "$HARUN_STAGE" "$HARUN_BACKUP"
  exit "$HARUN_STATUS"
}
HARUN_RESTORE_READY=1
trap 'harun_abort "$?"' ERR
trap 'harun_abort 130' INT
trap 'harun_abort 143' TERM

cat > "$HARUN_BACKUP/gateway_patch.py" <<'HARUN_GATEWAY_PATCH'
import ast, hashlib, json, os, stat, sys, tempfile
from pathlib import Path

GATEWAY_SHA='7714469daa3c529ee228d8e049cea3b3a52484a46a8429b2b31e9e233573c422'
OLD_BUILD_SHAPE='4eea51d84c2184e750635f9880b4eb92384dac217b61589a26c95809c712118a'
NEW_BUILD_SHAPE='0f43888062ce6a4d8baa97a5f23c0bce442fdc83927b436025facea0fd7f8470'
OLD_CLASS_SHAPE='c16bdef5a6284bb3d4ae424c4f03f81bdfc20cdf1d18572c4d04e31586dcc570'
NEW_CLASS_SHAPE='827abf63f86f6684ebeaa82a2885277aa4cf477bff5914e37d2e73ab1e259fb0'
NEW_BUILD = '''
def build_intent(plan, operation):
    """Immutable internal order command, never a user ticket or exchange receipt."""
    from .core import validated_protection_working_type
    if not isinstance(plan, dict) or plan.get('mode') != 'ORDER_INTENT' or plan.get('symbol') == 'HYPEUSDT':
        raise Review('INVALID_ORDER_CONTRACT')
    try:
        target = validated_risk_target(plan['risk_target_usdt'])
        risk = number(plan['risk'])
        working_type = validated_protection_working_type(plan.get('protection_working_type', 'MARK_PRICE'))
    except (KeyError, TypeError, Review): raise Review('INVALID_ORDER_CONTRACT') from None
    if plan.get('side') not in ('LONG', 'SHORT') or not D('0') < risk <= target:
        raise Review('INVALID_ORDER_CONTRACT')
    entry_side = 'BUY' if plan['side'] == 'LONG' else 'SELL'
    client_id = 'hao-' + hashlib.sha256(operation.encode()).hexdigest()[:28]
    intent = dict(intent_id=operation, client_order_id=client_id,
        symbol=plan['symbol'], position_side='BOTH', side=plan['side'],
        entry=dict(order_type='LIMIT', side=entry_side, price=plan['entry'],
                   quantity=plan['execution_quantity'], time_in_force='GTC'),
        protection=dict(exit_side='SELL' if entry_side == 'BUY' else 'BUY',
                        stop_loss=plan['sl'], take_profit=plan['tp'], working_type=working_type),
        margin_mode=plan['margin_mode'], leverage=plan['leverage'],
        risk_target_usdt=plan['risk_target_usdt'], risk_usdt=plan['risk'],
        gross_risk_usdt=plan['gross_risk'],entry_fee_usdt=plan['entry_fee_usdt'],
        sl_exit_fee_usdt=plan['sl_exit_fee_usdt'],tp_exit_fee_usdt=plan['tp_exit_fee_usdt'],
        net_reward_usdt=plan['net_reward'],net_reward_risk=plan['net_rr'],
        fee_evidence=dict(source=plan['fee_source'],symbol=plan['fee_symbol'],
            observed_at=plan['fee_observed_at'],taker_rate=plan['entry_fee_rate']),
        excluded_costs=plan['excluded_costs'],
        evidence_sha256=plan['provenance']['payload_sha256'])
    if 'reward_risk_policy' in plan:
        from .core import Rules, validate_reward_risk_policy
        from dataclasses import fields
        try:
            sizing = plan['sizing_rules']
            if not isinstance(sizing, dict) or set(sizing) != {field.name for field in fields(Rules)}:
                raise ValueError
            rules = Rules(**{key: (D(value) if isinstance(value, str) and
                key not in ('fee_source', 'fee_symbol') else value) for key, value in sizing.items()})
            validate_reward_risk_policy(plan, rules)
            intent['reward_risk_policy'] = plan['reward_risk_policy']
            intent['tp_tick_size'] = format(number(rules.tick), 'f')
        except Exception:
            raise Review('INVALID_ORDER_CONTRACT') from None
    return intent
'''
NEW_VALIDATE = r'''def _validate_intent(self, intent):
        import re
        from fractions import Fraction
        try:
            if not isinstance(intent, dict):
                raise ValueError()
            symbol, client = intent['symbol'], intent['client_order_id']
            if not isinstance(symbol, str) or not re.fullmatch(r'[A-Z0-9]{2,18}USDT', symbol) or symbol == 'HYPEUSDT':
                raise ValueError()
            if not isinstance(client, str) or not re.fullmatch(r'hao-[a-f0-9]{28}', client):
                raise ValueError()
            operation = intent['intent_id']
            if not isinstance(operation, str) or client != 'hao-' + hashlib.sha256(operation.encode()).hexdigest()[:28]:
                raise ValueError()
            if intent['position_side'] != 'BOTH' or intent['margin_mode'] != 'CROSS' or type(intent['leverage']) is not int or intent['leverage'] != 75:
                raise ValueError()
            if intent['side'] not in ('LONG', 'SHORT'):
                raise ValueError()
            entry, protection = intent['entry'], intent['protection']
            side = 'BUY' if intent['side'] == 'LONG' else 'SELL'
            if entry['side'] != side or entry['order_type'] != 'LIMIT' or entry['time_in_force'] != 'GTC':
                raise ValueError()
            if protection['exit_side'] != ('SELL' if side == 'BUY' else 'BUY') or protection['working_type'] not in ('MARK_PRICE', 'CONTRACT_PRICE'):
                raise ValueError()
            e, q, sl, tp = (Fraction(self._decimal(v, positive=True)) for v in
                             (entry['price'], entry['quantity'], protection['stop_loss'], protection['take_profit']))
            if not (sl < e < tp if side == 'BUY' else tp < e < sl):
                raise ValueError()
            fees = intent['fee_evidence']
            rate = Fraction(self._decimal(fees['taker_rate']))
            if fees['source'] != 'BINANCE_FUTURES_COMMISSION_RATE' or fees['symbol'] != symbol or not 0 <= rate < 1:
                raise ValueError()
            target = Fraction(validated_risk_target(intent['risk_target_usdt']))
            gross = q * abs(e - sl)
            entry_fee, sl_fee, tp_fee = q*e*rate, q*sl*rate, q*tp*rate
            risk, reward = gross + entry_fee + sl_fee, q*abs(tp-e) - entry_fee - tp_fee
            if ('reward_risk_policy' in intent) != ('tp_tick_size' in intent):
                raise ValueError()
            if 'reward_risk_policy' in intent:
                if intent['reward_risk_policy'] != 'NET_1_TO_2_NEAREST_TICK':
                    raise ValueError()
                tick = Fraction(self._decimal(intent['tp_tick_size'], positive=True))
                if reward < 2*risk:
                    raise Review('NET_RISK_REWARD_BELOW_2')
                loss = risk/q
                tp_target = (e*(1+rate)+2*loss)/(1-rate) if side == 'BUY' else (e*(1-rate)-2*loss)/(1+rate)
                steps = tp_target/tick
                count = -(-steps.numerator//steps.denominator) if side == 'BUY' else steps.numerator//steps.denominator
                if count*tick <= 0 or tp != count*tick:
                    raise Review('NET_RISK_REWARD_NOT_TARGET_2')
            expected = {'risk_usdt': risk, 'gross_risk_usdt': gross,
                        'entry_fee_usdt': entry_fee, 'sl_exit_fee_usdt': sl_fee,
                        'tp_exit_fee_usdt': tp_fee, 'net_reward_usdt': reward}
            if not 0 < risk <= target or reward < 2*risk:
                raise ValueError()
            if any(Fraction(self._decimal(intent[key])) != val for key, val in expected.items()):
                raise ValueError()
            ratio = intent['net_reward_risk']
            if not isinstance(ratio, str) or len(ratio) > 256 or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?(?:E[+-]?[0-9]{1,3})?', ratio):
                raise ValueError()
            if not Decimal(ratio).is_finite() or Fraction(Decimal(ratio)) < 2 or intent['excluded_costs'] != ['SLIPPAGE', 'FUNDING', 'GAPS']:
                raise ValueError()
            if not re.fullmatch(r'[a-f0-9]{64}', intent['evidence_sha256']):
                raise ValueError()
        except Review as error:
            if str(error) in ('NET_RISK_REWARD_BELOW_2', 'NET_RISK_REWARD_NOT_TARGET_2'):
                raise
            raise Review('BINANCE_ORDER_INTENT_INVALID') from None
        except Exception:
            raise Review('BINANCE_ORDER_INTENT_INVALID') from None'''


def shape(node):
    if isinstance(node,ast.AST):
        return [type(node).__name__,{k:shape(v) for k,v in ast.iter_fields(node) if k!='type_params' or v}]
    if isinstance(node,list):return [shape(v) for v in node]
    if isinstance(node,bytes):return {"constant_bytes_hex":node.hex()}
    return node


def shape_digest(node):
    return hashlib.sha256(json.dumps(shape(node),sort_keys=True,separators=(',',':')).encode()).hexdigest()


def span(raw,node):
    lines=raw.split(b'\n');offsets=[0]
    for line in lines[:-1]:offsets.append(offsets[-1]+len(line)+1)
    return offsets[node.lineno-1]+node.col_offset,offsets[node.end_lineno-1]+node.end_col_offset


def transform_gateway(raw,expected_sha=GATEWAY_SHA):
    assert len(raw)<262144 and hashlib.sha256(raw).hexdigest()==expected_sha, 'GATEWAY_SOURCE_CHANGED'
    tree=ast.parse(raw)
    builds=[n for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name=='build_intent']
    classes=[n for n in ast.walk(tree) if isinstance(n,ast.ClassDef) and n.name=='OrderGateway']
    assert len(builds)==len(classes)==1 and builds[0] in tree.body and classes[0] in tree.body, 'GATEWAY_BOUNDARIES_CHANGED'
    build,cls=builds[0],classes[0]
    assert not build.decorator_list and not cls.decorator_list and not build.col_offset and not cls.col_offset, 'GATEWAY_BOUNDARIES_CHANGED'
    assert shape_digest(build)==OLD_BUILD_SHAPE and shape_digest(cls)==OLD_CLASS_SHAPE, 'GATEWAY_SHAPE_CHANGED'
    start,end=span(raw,cls);block=raw[start:end]
    methods=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_validate_intent']
    assert len(methods)==1, 'GATEWAY_VALIDATION_BOUNDARY_CHANGED'
    first,last=span(raw,methods[0])
    newline=b'\r\n' if b'\r\n' in raw else b'\n'
    validation=NEW_VALIDATE.encode().replace(b'\n',newline)
    block=block[:first-start]+validation+block[last-start:]
    changes=(
        (b"workingType='MARK_PRICE', reduceOnly=True",b"workingType=intent['protection']['working_type'], reduceOnly=True"),
        (b"'workingType': 'MARK_PRICE', 'clientAlgoId': client",b"'workingType': intent['protection']['working_type'], 'clientAlgoId': client"),
    )
    for before,after in changes:
        assert block.count(before)==1, 'GATEWAY_ANCHOR_CHANGED'
        block=block.replace(before,after,1)
    assert shape_digest(ast.parse(block).body[0])==NEW_CLASS_SHAPE, 'GATEWAY_TARGET_SHAPE_CHANGED'
    newline=b'\r\n' if b'\r\n' in raw else b'\n'
    function=NEW_BUILD.encode().replace(b'\n',newline)
    assert shape_digest(ast.parse(function).body[0])==NEW_BUILD_SHAPE, 'BUILD_INTENT_TARGET_CHANGED'
    replacements=[(start,end,block),(*span(raw,build),function)]
    changed=raw
    for first,last,value in sorted(replacements,reverse=True):
        changed=changed[:first]+value+changed[last:]
    final=ast.parse(changed)
    before_other=[shape(n) for n in tree.body if n is not build and n is not cls]
    after_other=[shape(n) for n in final.body if not isinstance(n,(ast.FunctionDef,ast.ClassDef)) or n.name not in ('build_intent','OrderGateway')]
    assert before_other==after_other, 'OTHER_GATEWAY_CODE_CHANGED'
    compile(changed,'worker/order_gateway.py','exec')
    return changed


def main():
    gateway=Path('worker/order_gateway.py');info=gateway.lstat()
    assert stat.S_ISREG(info.st_mode) and info.st_nlink==1, 'GATEWAY_NOT_REGULAR'
    old=gateway.read_bytes();new=transform_gateway(old)
    backup=Path(sys.argv[1]);expected=json.loads(sys.argv[2])
    new_pins={'worker/core.py': '4b08349cc49470c47d2d29f0e6e2d72e086356f42d90b1afab92f98a4f0c6b5e', 'worker/robot.py': 'cc0e1c6b68581fa23402dd61c9de023941a1f13489137282c83ae32ccf0dfa8d', 'worker/robot_provenance.py': '2975732f7f3888147ae50c81b2b53e85c8c0930c47310f88246378a53ceac082', 'worker/analysis.py': '1777b60c663fd971969b526a7c2c785eed3cd057d0edc70fb55453377428b97e', 'worker/diagnostics.py': '2b28a42b0f03c76c97dc5111a33b2be167d346f8aba1103a7c0be10f08382f41'}
    for name,value in new_pins.items():
        p=Path(name)
        assert p.is_file() and not p.is_symlink() and hashlib.sha256(p.read_bytes()).hexdigest()==value, 'SOURCE_MISMATCH:'+name
        compile(p.read_bytes(),name,'exec')
        expected[name]=value
    assert expected['worker/order_gateway.py']==GATEWAY_SHA, 'MANIFEST_GATEWAY_CHANGED'
    expected['worker/order_gateway.py']=hashlib.sha256(new).hexdigest()
    for name,value in expected.items():
        if name=='worker/order_gateway.py':continue
        p=Path(name)
        assert p.is_file() and not p.is_symlink() and hashlib.sha256(p.read_bytes()).hexdigest()==value, 'OTHER_SOURCE_CHANGED:'+name
    fd,temp=tempfile.mkstemp(prefix='.harun-last-price-',dir=gateway.parent)
    try:
        with os.fdopen(fd,'wb') as output:
            output.write(new);output.flush();os.fsync(output.fileno())
            os.fchmod(output.fileno(),stat.S_IMODE(info.st_mode))
            os.fchown(output.fileno(),info.st_uid,info.st_gid)
        assert gateway.read_bytes()==old, 'GATEWAY_CHANGED_DURING_PATCH'
        os.replace(temp,gateway)
    finally:
        if os.path.exists(temp):os.unlink(temp)
    with (backup/'source.after.json').open('x') as output:
        json.dump(expected,output,sort_keys=True)
    with (backup/'manifest.json').open('x') as output:
        json.dump(dict(gateway_before_sha256=GATEWAY_SHA,gateway_after_sha256=expected['worker/order_gateway.py'],
                       changed_sources=list(new_pins)+['worker/order_gateway.py']),output,sort_keys=True)
    print('GATEWAY_PATCH_VERIFIED')


if __name__=='__main__':main()
HARUN_GATEWAY_PATCH

cat > "$HARUN_BACKUP/replacement.patch" <<'HARUN_PATCH'
diff --git a/worker/analysis.py b/worker/analysis.py
index 609d6dd..f80e9a2 100644
--- a/worker/analysis.py
+++ b/worker/analysis.py
@@ -3,9 +3,11 @@ import time
 import re
 from dataclasses import replace
 from datetime import datetime
-from .core import D,Review,number,RISK,validated_risk_target,validated_fee_rate,FEE_SOURCE
+from .core import D,Review,number,RISK,validated_risk_target,validated_fee_rate,FEE_SOURCE,REWARD_RISK_POLICY

-SIZING_CONTRACT='Target planned net loss at stop loss is 5 USDT including entry and stop-loss exit fees. The worker computes the largest legal Binance base-asset quantity such that quantity × (abs(limit_entry - stop_loss) + limit_entry × taker_fee_rate + stop_loss × taker_fee_rate) <= 5 USDT. Use the supplied per-symbol account taker commission for entry, SL exit, and TP exit. Net TP reward after entry and TP exit fees must be at least twice the fee-inclusive SL loss. Slippage, funding, and price gaps are outside this calculation. The provider determines entry, TP, SL, or HOLD; the worker alone sizes quantity.'
+SIZING_CONTRACT='Target planned net loss at stop loss is 5 USDT including entry and stop-loss exit fees. The worker computes the largest legal Binance base-asset quantity such that quantity × (abs(limit_entry - stop_loss) + limit_entry × taker_fee_rate + stop_loss × taker_fee_rate) <= 5 USDT. Use the supplied per-symbol account taker commission for entry, SL exit, and TP exit. Select TP targeting net reward/risk 1:2 after entry and exit fees, with only the unavoidable legal price-tick rounding: the first LONG TP tick at or above the exact net 1:2 target, or the first SHORT TP tick at or below it. Do not select an arbitrarily larger reward/risk. Slippage, funding, and price gaps are outside this calculation. The provider determines entry, TP, SL, or HOLD; the worker alone sizes quantity and rejects noncanonical TP without rewriting model prices.'
+
+TP_CONTRACT='Let E=limit_entry, S=stop_loss, f=the supplied account taker_fee_rate, and L=abs(E-S)+f*(E+S). LONG exact TP=(E*(1+f)+2*L)/(1-f), rounded up to the next valid tickSize. SHORT exact TP=(E*(1-f)-2*L)/(1+f), rounded down to the previous valid tickSize. Return that first legal TP tick only; net reward after entry and TP fees must reach twice the fee-inclusive SL loss. If no accurate profitable setup meets this policy, return HOLD. The worker does not rewrite TP.'

 def account_fee_rules(rules,symbol,client):
     """Attach a fresh account commission GET, without assuming a fallback rate."""
@@ -51,6 +53,8 @@ def analysis_context(market,symbol,risk_target=RISK,account_client=None):
     return {**data,'contract_rules':contract,'risk_constraints':{'quantity_unit':'base_asset',
         'margin_mode_target':'CROSS','leverage_target':75,'maximum_loss_at_sl_usdt':str(risk_target),
         'minimum_actual_reward_risk':2,'reward_risk_basis':'NET_AFTER_ENTRY_AND_EXIT_FEES',
+        'target_actual_reward_risk':2,'reward_risk_policy':REWARD_RISK_POLICY,
+        'take_profit_contract':TP_CONTRACT,'take_profit_tick_rounding':{'LONG':'CEILING','SHORT':'FLOOR'},
         'entry_fee_rate':format(rules.taker_fee_rate,'f'),'sl_exit_fee_rate':format(rules.taker_fee_rate,'f'),
         'tp_exit_fee_rate':format(rules.taker_fee_rate,'f'),'fee_source':rules.fee_source,
         'fee_symbol':rules.fee_symbol,'fee_observed_at':rules.fee_observed_at,
diff --git a/worker/core.py b/worker/core.py
index 3f26596..5d6036a 100644
--- a/worker/core.py
+++ b/worker/core.py
@@ -16,6 +16,7 @@ TZ = ZoneInfo('Asia/Bangkok')
 RISK = D('5')
 FEE_SOURCE = 'BINANCE_FUTURES_COMMISSION_RATE'
 SIZING_METHOD = 'MAX_LOT_ENTRY_SL_TAKER_FEES_V2'
+REWARD_RISK_POLICY = 'NET_1_TO_2_NEAREST_TICK'
 class Review(Exception): pass

 def now(): return datetime.now(timezone.utc).isoformat()
@@ -73,6 +74,11 @@ def validated_risk_target(value):
     if value>D("100"):raise Review("INVALID_RISK_TARGET")
     return value

+def validated_protection_working_type(value):
+    if not isinstance(value,str) or value not in ('MARK_PRICE','CONTRACT_PRICE'):
+        raise Review('INVALID_ORDER_CONTRACT')
+    return value
+
 def _exact_decimal(value):
     """Convert finite decimal fractions exactly, independently of Decimal context."""
     value=Fraction(value);denominator=value.denominator;twos=fives=0
@@ -156,7 +162,33 @@ def risk_costs(entry,tp,sl,quantity,rules,*,symbol=None,check_fresh=True):
         excluded_costs=['SLIPPAGE','FUNDING','GAPS'])


-def risk_check(signal, rules, risk_target=RISK):
+def target_reward_risk_tp(entry,stop_loss,side,rules,*,symbol=None):
+    """Exact first legal tick reaching net 1:2; never change an analyzed level."""
+    e,sl,tick=(Fraction(number(value)) for value in (entry,stop_loss,rules.tick))
+    fee=Fraction(validated_fee_rate(rules,symbol,check_fresh=False))
+    if side not in ('LONG','SHORT'):raise Review('INVALID_SIDE')
+    if not (sl<e if side=='LONG' else sl>e):raise Review('INVALID_ENTRY_TP_SL')
+    loss=abs(e-sl)+fee*(e+sl)
+    target=(e*(1+fee)+2*loss)/(1-fee) if side=='LONG' else (e*(1-fee)-2*loss)/(1+fee)
+    steps=target/tick
+    count=-(-steps.numerator//steps.denominator) if side=='LONG' else steps.numerator//steps.denominator
+    if count*tick<=0:raise Review('INVALID_ENTRY_TP_SL')
+    return _exact_decimal(count*tick)
+
+def validate_reward_risk_policy(plan,rules):
+    if 'reward_risk_policy' not in plan:return True
+    if not isinstance(plan['reward_risk_policy'],str) or plan['reward_risk_policy']!=REWARD_RISK_POLICY:
+        raise Review('INVALID_RISK_REWARD')
+    e,tp,sl=(Fraction(number(plan[key])) for key in ('entry','tp','sl'))
+    fee=Fraction(validated_fee_rate(rules,plan['symbol'],check_fresh=False))
+    target=target_reward_risk_tp(plan['entry'],plan['sl'],plan['side'],rules,symbol=plan['symbol'])
+    reward=(tp-e if plan['side']=='LONG' else e-tp)-fee*(e+tp)
+    loss=abs(e-sl)+fee*(e+sl)
+    if reward<2*loss:raise Review('NET_RISK_REWARD_BELOW_2')
+    if tp!=Fraction(target):raise Review('NET_RISK_REWARD_NOT_TARGET_2')
+    return True
+
+def risk_check(signal, rules, risk_target=RISK,*,reward_risk_policy=None):
     risk_target=validated_risk_target(risk_target)
     if not 0 <= time.time()-rules.observed_at <= 300: raise Review('NEEDS_REVIEW: filter pasar kedaluwarsa')
     for v in (rules.step,rules.minimum,rules.maximum,rules.tick): number(v)
@@ -180,13 +212,18 @@ def risk_check(signal, rules, risk_target=RISK):
             raise Review('REJECT_PERCENT_PRICE')
     costs=risk_costs(signal.entry,signal.tp,signal.sl,qty,rules,symbol=signal.symbol)
     if not Fraction(0)<Fraction(D(costs['risk']))<=Fraction(risk_target):raise Review('NEEDS_REVIEW: risiko melampaui batas')
-    return dict(symbol=signal.symbol,side=signal.side,entry=format(signal.entry,'f'),tp=format(signal.tp,'f'),sl=format(signal.sl,'f'),
+    plan=dict(symbol=signal.symbol,side=signal.side,entry=format(signal.entry,'f'),tp=format(signal.tp,'f'),sl=format(signal.sl,'f'),
                 quantity=format(qty,'f'),execution_quantity=format(qty,'f'),margin_mode='CROSS',leverage=75,order_type='LIMIT',
                 mode='ORDER_INTENT',risk_target_usdt=format(risk_target,'f'),rules_checked_at=rules.observed_at,**costs)
+    if reward_risk_policy is not None:
+        plan['reward_risk_policy']=reward_risk_policy
+        validate_reward_risk_policy(plan,rules)
+    return plan


 def preflight(plan, rules, risk_target=RISK):
     if not isinstance(plan,dict) or plan.get('mode')!='ORDER_INTENT': raise Review('INVALID_ORDER_CONTRACT')
+    validated_protection_working_type(plan.get('protection_working_type','MARK_PRICE'))
     if plan.get('margin_mode')!='CROSS' or plan.get('leverage')!=75 or plan.get('order_type')!='LIMIT':
         raise Review('NEEDS_REVIEW: konfigurasi order salah')
     sig=Signal(plan['symbol'],plan['side'],number(plan['entry']),number(plan['tp']),number(plan['sl']),number(plan['quantity']))
@@ -203,6 +240,7 @@ def preflight(plan, rules, risk_target=RISK):
     if not 0<=time.time()-intent_observed<=300:raise Review('STALE_FEE_EVIDENCE')
     for key in (*risk_costs(sig.entry,sig.tp,sig.sl,D(plan['quantity']),rules,symbol=sig.symbol), 'risk_target_usdt'):
         if key!='fee_observed_at' and plan.get(key)!=verified[key]:raise Review('ORDER_COSTS_CHANGED')
+    validate_reward_risk_policy(plan,rules)

 def verified_rr(plan):
     """Verify derived RR against exact levels without repricing or resizing.
diff --git a/worker/diagnostics.py b/worker/diagnostics.py
index fbe3088..deeafdc 100644
--- a/worker/diagnostics.py
+++ b/worker/diagnostics.py
@@ -12,7 +12,7 @@ CODES=frozenset(('NETWORK_UNCERTAIN','INVALID_RESPONSE_ENVELOPE','INVALID_OUTPUT
  'MARKET_DATA_REJECTED','LOCAL_PROCESSING_FAILED','STALE_CONTRACT_CONTEXT','INVALID_CONTRACT_CONTEXT',
  'INVALID_ORDER_CONTRACT','INVALID_RISK_REWARD','INVALID_RISK_TARGET','INVALID_EXECUTION_QUANTITY',
  'INVALID_SIDE','INVALID_RULES','RISK_ABOVE_TARGET','FEE_EVIDENCE_UNAVAILABLE','INVALID_FEE_EVIDENCE',
- 'STALE_FEE_EVIDENCE','NET_RISK_REWARD_BELOW_2','ORDER_COSTS_CHANGED','INVALID_COST_MODEL','RESEARCH_PAUSED'))
+ 'STALE_FEE_EVIDENCE','NET_RISK_REWARD_BELOW_2','NET_RISK_REWARD_NOT_TARGET_2','ORDER_COSTS_CHANGED','INVALID_COST_MODEL','RESEARCH_PAUSED'))
 # Old operation IDs remain readable audit metadata, never executable requests.
 OPERATION_PATTERN=(r'\d{4}-\d{2}-\d{2}:(?:robot-v8:[0-2]:(?:screening:[0-3]:[12]|analysis-v8:[A-Z0-9]{2,18}USDT)'
     r'|robot-v9:(?:0|[1-9]\d{0,11}):(?:screening:[0-3]:[12]|analysis-v9:[A-Z0-9]{2,18}USDT))')
diff --git a/worker/robot.py b/worker/robot.py
index db818ca..1307549 100644
--- a/worker/robot.py
+++ b/worker/robot.py
@@ -7,7 +7,7 @@ import time
 from pathlib import Path
 from datetime import datetime,timezone
 from zoneinfo import ZoneInfo
-from .core import D,Review,day,now,risk_check,preflight
+from .core import D,Review,day,now,risk_check,preflight,REWARD_RISK_POLICY
 from .account_state import (account_state,screening_contract,MANUAL_ONLY_SYMBOLS,MAX_REPLACEMENTS)
 from .robot_store import RobotStore
 from .order_gateway import OrderGateway,GatewayUnavailable,NOT_CONNECTED,build_intent,require_implementation
@@ -43,17 +43,24 @@ class Coordinator:
                 return self.store.report('REJECTED',reason=reason if reason in allowed else 'ROBOT_PREFLIGHT_NEEDS_REVIEW')
     def save_cycle(self,cycle,data,state='ACTIVE'):
         self.db.execute('UPDATE robot_cycles SET data=?,state=? WHERE id=?',(json.dumps(data),state,cycle))
+    def replaceable_result(self,row):
+        if row['status']=='HOLD':return True
+        # A local fee/RR rejection may select another coin, never replay an order.
+        return (row['status']=='REJECTED' and row['failure_code'] in ('NET_RISK_REWARD_BELOW_2','NET_RISK_REWARD_NOT_TARGET_2')
+            and row['plan'] is None and not self.db.execute(
+                'SELECT 1 FROM order_intents WHERE candidate_id=? LIMIT 1',(row['id'],)).fetchone())
+    def replacement_count(self,data,results):
+        return sum(r['symbol'] in data.get('round_symbols',[]) and self.replaceable_result(r) for r in results)
     def research_budget(self,data,results):
         ready=sum(r['status'] in ('READY_FOR_EXECUTION','EXECUTION_BLOCKED','ENTRY_PENDING','POSITION_PROTECTED','CLOSED') for r in results)
-        technical=sum(r['status']=='REJECTED' for r in results)
+        technical=sum(r['status']=='REJECTED' and not self.replaceable_result(r) for r in results)
         return data['target']-ready-technical
     def unfinished_cycle(self,row):
         data=json.loads(row['data']);results=self.store.results(row['id'])
         if self.research_budget(data,results)<=0:return False
         if data['queue']:return True
         if row['state']=='ACTIVE' and data['screen']==-1:return True
-        held=any(r['status']=='HOLD' and r['symbol'] in data.get('round_symbols',[]) for r in results)
-        return held and data['replacements']<MAX_REPLACEMENTS
+        return self.replacement_count(data,results)>0 and data['replacements']<MAX_REPLACEMENTS
     def reject_queued_symbol(self,cycle,data,symbol,account,reason):
         self.db.execute('BEGIN IMMEDIATE')
         try:
@@ -303,8 +310,8 @@ class Coordinator:
         ready=sum(r['status'] in ('READY_FOR_EXECUTION','EXECUTION_BLOCKED','ENTRY_PENDING','POSITION_PROTECTED','CLOSED') for r in results)
         unsent=self.db.execute("SELECT symbol FROM robot_candidates WHERE status IN ('READY_FOR_EXECUTION','EXECUTION_BLOCKED')")
         ready_unexposed=sum(r['symbol'] not in account['running_symbols'] for r in unsent)
-        technical=sum(r['status']=='REJECTED' for r in results)
-        # A rejected research opportunity uses budget, not a live account slot.
+        technical=sum(r['status']=='REJECTED' and not self.replaceable_result(r) for r in results)
+        # Other rejected research opportunities use budget, not a live account slot.
         # Ready intents reserve current free slots; the original target stays fixed.
         # A matching running position already occupies its real account slot.
         budget=self.research_budget(data,results)
@@ -323,17 +330,16 @@ class Coordinator:
                 return self.reject_queued_symbol(cycle,data,symbol,account,'ROBOT_SYMBOL_EXPOSED')
             return self.analyze(cycle,data,symbol,account,today)
         initial=data['screen']==-1
-        # Only HOLD decisions from the just-analyzed round are replaceable.
-        # A rejected/duplicate/exposed screening result is not a new HOLD, and
-        # historical HOLDs must not trigger another paid round after rejection.
-        held=sum(r['status']=='HOLD' and r['symbol'] in data.get('round_symbols',[]) for r in results)
-        if not initial and not held:
+        # HOLD and local fee/RR rejections share the same three replacement rounds.
+        # Only the latest round triggers screening; other failures consume budget.
+        replaceable=self.replacement_count(data,results)
+        if not initial and not replaceable:
             self.save_cycle(cycle,data,'COMPLETE')
             return blocked or self.store.report('REJECTED' if technical else 'INSUFFICIENT_ACTIONABLE_SETUPS',account,wait_reason='ROBOT_CYCLE_COMPLETE')
         if not initial and data['replacements']>=MAX_REPLACEMENTS:
             self.save_cycle(cycle,data,'COMPLETE')
             return self.store.report('INSUFFICIENT_ACTIONABLE_SETUPS',account,wait_reason='ROBOT_CYCLE_COMPLETE')
-        return self.screen(cycle,data,remaining if initial else min(remaining,held),account,today,initial)
+        return self.screen(cycle,data,remaining if initial else min(remaining,replaceable),account,today,initial)
     def claim(self,operation,cycle,kind,symbol,today,target=None):
         self.db.execute('BEGIN IMMEDIATE')
         try:
@@ -354,7 +360,10 @@ class Coordinator:
             self.unclaim_unsent_research(operation)
             return self.store.report('WAITING',account,wait_reason=self.pause_reason())
         try:
-            value=self.neuro.ask(operation,prompt,schema,lambda v:selections(v,catalog,count),catalog=catalog,
+            context=None if initial else dict(requested_count=count,
+                excluded_symbols=sorted(set(data['seen'])|self.exposed_symbols(account)|MANUAL_ONLY_SYMBOLS),
+                replacement_instruction='Choose different Binance USD-M USDT perpetual coins that are not in excluded_symbols.')
+            value=self.neuro.ask(operation,prompt,schema,lambda v:selections(v,catalog,count),context,catalog=catalog,
                 continue_if=lambda:self.allowed(today))
             coins=selections(value,catalog,count)
             data['screen']=index
@@ -416,7 +425,10 @@ class Coordinator:
                     try:_,refreshed=analysis_context(self.market,symbol,target,self.account_factory())
                     except ResearchReadPaused:pass
                     else:rules=refreshed
-                plan=risk_check(signal,rules,target);plan['risk_target_usdt']=target
+                plan=risk_check(signal,rules,target,reward_risk_policy=REWARD_RISK_POLICY);plan['risk_target_usdt']=target
+                # New Binance TP/SL use the requested Last Price trigger.
+                # Historical plans remain immutable and retain their own trigger.
+                plan['protection_working_type']='CONTRACT_PRICE'
                 plan['sizing_rules']={k:str(v) if isinstance(v,D) else v for k,v in asdict(rules).items()}
                 preflight(plan,rules,target);stamp(self.db,plan,operation);verify(self.db,plan,operation)
                 state='READY_FOR_EXECUTION'
diff --git a/worker/robot_provenance.py b/worker/robot_provenance.py
index d1b7548..c5e631e 100644
--- a/worker/robot_provenance.py
+++ b/worker/robot_provenance.py
@@ -2,7 +2,7 @@
 import json
 import re
 from dataclasses import fields as rule_fields
-from .core import Review,D,number,validated_risk_target,Rules,maximum_risk_quantity,verified_rr,risk_costs
+from .core import Review,D,number,validated_risk_target,validated_protection_working_type,Rules,maximum_risk_quantity,verified_rr,risk_costs,validate_reward_risk_policy
 from .provenance import digest,request_digest
 from .neuroapi import setup

@@ -13,6 +13,13 @@ OPERATION=r'\d{4}-\d{2}-\d{2}:robot-v9:(?:0|[1-9]\d{0,11}):analysis-v9:'
 def stamp(db,plan,operation):
     try:
         symbol=plan['symbol']
+        validated_protection_working_type(plan.get('protection_working_type','MARK_PRICE'))
+        if 'reward_risk_policy' in plan:
+            sizing=plan['sizing_rules']
+            if not isinstance(sizing,dict) or set(sizing)!={field.name for field in rule_fields(Rules)}:raise ValueError
+            rules=Rules(**{key:(D(value) if isinstance(value,str) and key not in ('fee_source','fee_symbol') else value)
+                for key,value in sizing.items()})
+            validate_reward_risk_policy(plan,rules)
         if plan.get('mode')!='ORDER_INTENT' or not isinstance(symbol,str) or not re.fullmatch(r'[A-Z0-9]{2,18}USDT',symbol):raise ValueError
         if not isinstance(operation,str) or not re.fullmatch(OPERATION+re.escape(symbol),operation):raise ValueError
         previous=plan.get('provenance')
@@ -30,6 +37,7 @@ def stamp(db,plan,operation):

 def verify(db,plan,operation):
     try:
+        validated_protection_working_type(plan.get('protection_working_type','MARK_PRICE'))
         proof=plan['provenance'];copy={k:v for k,v in plan.items() if k!='provenance'}
         stamp(db,copy,operation)
         if proof!=copy['provenance'] or not proof['request_output_sha256'] or not proof['request_body_sha256']:raise ValueError
@@ -46,6 +54,7 @@ def verify(db,plan,operation):
         rules=Rules(**{k:(D(v) if isinstance(v,str) and k not in ('fee_source','fee_symbol') else v) for k,v in fields.items()})
         for v in (rules.step,rules.minimum,rules.maximum,rules.tick):number(v)
         if not rules.min_notional.is_finite() or rules.min_notional<0:raise ValueError
+        validate_reward_risk_policy(plan,rules)
         if qty!=maximum_risk_quantity(signal.entry,signal.sl,rules,target,check_fresh=False):raise ValueError
         costs=risk_costs(signal.entry,signal.tp,signal.sl,qty,rules,symbol=signal.symbol,check_fresh=False)
         if any(plan.get(key)!=value for key,value in costs.items()):raise ValueError
HARUN_PATCH
HARUN_STAGE=SOURCE_PATCH
git apply --check "$HARUN_BACKUP/replacement.patch"
git apply "$HARUN_BACKUP/replacement.patch"
python3 -I -B -S "$HARUN_BACKUP/gateway_patch.py" "$HARUN_BACKUP" "$HARUN_OLD_MAP"
HARUN_NEW_MAP="$(cat "$HARUN_BACKUP/source.after.json")"
python3 -I -B -S "$HARUN_BACKUP/check.py" "$HARUN_NEW_MAP" .
HARUN_STAGE=BUILD
docker compose -f compose.yaml build worker
HARUN_NEW_IMAGE="$(docker image inspect --format '{{.Id}}' harun-office-worker:latest)"
HARUN_STAGE=OFFLINE_IMAGE_CHECK
docker run --rm -i --network none --read-only --user 10001:10001 --entrypoint python "$HARUN_NEW_IMAGE" -I -B -S - "$HARUN_NEW_MAP" /app < "$HARUN_BACKUP/check.py"
python3 -I -B -S "$HARUN_BACKUP/check.py" "$HARUN_NEW_MAP" .
# The old worker can keep protecting a started fill while the new image builds.
# Refuse an unresolved exchange outcome before stopping that worker.
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B -S - "$HARUN_OLD_MAP" /app preflight < "$HARUN_BACKUP/check.py"
HARUN_STAGE=GRACEFUL_RESTART
HARUN_WORKER_STOPPED=1
docker compose -f compose.yaml stop -t 660 worker
docker compose -f compose.yaml up -d --no-build --no-deps --force-recreate --wait --wait-timeout 180 worker
# A healthy new worker may now submit/protect orders; do not automatically roll
# it back on a later diagnostic failure. Existing volumes and ON/OFF persist.
HARUN_RESTORE_READY=0
HARUN_STAGE=RUNNING_SOURCE_CHECK
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -I -B -S - "$HARUN_NEW_MAP" /app < "$HARUN_BACKUP/check.py"
printf '%s\n' VPS_RR_TARGET_LAST_PRICE_VERIFIED
docker compose -f compose.yaml exec -T --user 10001:10001 worker python -B -m worker.robot_status
)
