"""VPS-only source patch: safe failure diagnostics; no SDK import or execution."""
import ast, hashlib, json, os, stat, tempfile
from pathlib import Path

OLD_R = "with gateway_transaction_reads():result=self.gateway.reconcile(intent)\n            except Exception:return self.mark_unknown(row['id'],row['candidate_id'],account)\n            observed="
NEW_R = OLD_R.replace('except Exception:return', 'except Exception as error:return').replace("row['candidate_id'],account)", "row['candidate_id'],account,reason=self.gateway_failure_code(error))")
OLD_S = "except Exception:return self.mark_unknown(row['id'],row['id'],account)\n        return self.record_gateway_observation(row['id'],row['id'],result,account)"
NEW_S = OLD_S.replace('except Exception:return', 'except Exception as error:return').replace("row['id'],account)", "row['id'],account,reason=self.gateway_failure_code(error))")
ANCHOR = '    def record_gateway_observation(self,intent_id,candidate_id,result,account):'
HELPER = '''    def gateway_failure_code(self,error):
        # Gateway diagnostics v1; uncertainty remains NEEDS_REVIEW and never retries.
        known=frozenset("ACCOUNT_MODE_INVALID CALLBACK_DEADLINE CAPACITY_FULL CLOCK_INVALID CONFIG_NOT_CONFIRMED ENDPOINT_DENIED ENTRY_ALREADY_EXISTS ENTRY_CANCEL_RACE ENTRY_PROOF_INVALID EXIT_CANCEL_OUTCOME_UNKNOWN EXIT_OUTCOME_UNKNOWN EXIT_PROOF_INVALID EXIT_RACE EXIT_RESAMPLE FEE_CHANGED FILL_PROOF_INVALID FILL_RESAMPLE FOREIGN_EXPOSURE FOREIGN_PENDING_ORDER HISTORY_INCOMPLETE INTENT_INVALID LEVERAGE_CONFIG_INVALID LEVERAGE_UNSUPPORTED LIFECYCLE_UNSTABLE LIQUIDATION_BEFORE_SL MARGIN_CONFIG_INVALID MARGIN_INSUFFICIENT NOT_CONFIGURED ORIGIN_DENIED OUTCOME_UNKNOWN PARAMETERS_INVALID PROOF_INVALID PROTECTION_OUTCOME_UNKNOWN PROTECTION_PROOF_INVALID REQUEST_FAILED SYMBOL_OCCUPIED".split())
        phases={'VALIDATE':{'_validate_intent'},'PROTECTION':{'_algo_proof','_ensure_algo','_cancel_algo','_cancel_exit_remainder','_exits','_ownership','_inventory','_reconcile_filled'},'PREFLIGHT':{'_preflight','_bracket','_account_config'},'CONFIGURE':{'_configure'},'ENTRY':{'_submit_once','_entry','_cancel_entry_remainder','_cleanup_entry'}}
        found=set();trace=error.__traceback__
        expected=Path(__file__).with_name('order_gateway.py').absolute()
        while trace is not None:
            code=trace.tb_frame.f_code
            if Path(code.co_filename).absolute()==expected:
                found.update(phase for phase,names in phases.items() if code.co_name in names)
            trace=trace.tb_next
        phase=next((value for value in ('VALIDATE','PROTECTION','PREFLIGHT','CONFIGURE','ENTRY') if value in found),None)
        if phase is None:return 'ORDER_OUTCOME_UNKNOWN'
        exchange=getattr(error,'code',None)
        if type(exchange) is int and -999999<=exchange<=999999 and exchange!=0:
            return 'ORDER_UNKNOWN_'+phase+'_C'+str(abs(exchange))
        if isinstance(error,Review):
            text=str(error)
            if text.startswith('BINANCE_ORDER_') and text[14:] in known:
                return text+('_'+phase if text[14:] in {'OUTCOME_UNKNOWN','REQUEST_FAILED','PROOF_INVALID'} else '')
        return 'ORDER_UNKNOWN_'+phase
'''

def patch_source(source):
    tree=ast.parse(source)
    classes=[n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Coordinator']
    if len(classes)!=1:raise ValueError('COORDINATOR_SHAPE_CHANGED')
    methods={n.name for n in classes[0].body if isinstance(n,ast.FunctionDef)}
    if not {'advance_execution','mark_unknown','record_gateway_observation'}.issubset(methods):raise ValueError('COORDINATOR_SHAPE_CHANGED')
    if 'gateway_failure_code' in methods:
        if source.count(HELPER)!=1 or source.count(NEW_R)!=1 or source.count(NEW_S)!=1 or OLD_R in source or OLD_S in source:raise ValueError('DIAGNOSTIC_PATCH_CONFLICT')
        return source
    if any(source.count(value)!=1 for value in (OLD_R,OLD_S,ANCHOR)):raise ValueError('SOURCE_ANCHOR_CHANGED')
    result=source.replace(OLD_R,NEW_R).replace(OLD_S,NEW_S).replace(ANCHOR,HELPER+ANCHOR)
    ast.parse(result)
    return result

def apply(project,write=False):
    path=Path(project).resolve()/'worker/robot.py'
    if path.is_symlink() or not path.is_file():raise ValueError('ROBOT_SOURCE_UNAVAILABLE')
    raw=path.read_bytes();before=hashlib.sha256(raw).hexdigest();info=path.stat()
    text=raw.decode('utf-8');newline='\r\n' if '\r\n' in text else '\n'
    normalized=text.replace('\r\n','\n')
    patched=patch_source(normalized).replace('\n',newline).encode('utf-8')
    after=hashlib.sha256(patched).hexdigest()
    if raw==patched:return {'status':'UNCHANGED','sha256':before}
    if not write:return {'status':'PATCH_READY','before_sha256':before,'after_sha256':after}
    directory=path.parents[2]/'.harun-gateway-diagnostics-backups';directory.mkdir(mode=0o700,exist_ok=True)
    if directory.is_symlink() or stat.S_IMODE(directory.stat().st_mode)!=0o700:raise ValueError('BACKUP_DIRECTORY_INVALID')
    backup=directory/('robot-'+before+'.py')
    if backup.exists():
        if backup.is_symlink() or backup.read_bytes()!=raw or stat.S_IMODE(backup.stat().st_mode)!=0o600:raise ValueError('BACKUP_CONFLICT')
    else:
        with backup.open('xb') as output:os.fchmod(output.fileno(),0o600);output.write(raw);output.flush();os.fsync(output.fileno())
    fd,name=tempfile.mkstemp(prefix='.robot-diagnostics-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as output:
            os.fchmod(output.fileno(),stat.S_IMODE(info.st_mode));os.fchown(output.fileno(),info.st_uid,info.st_gid)
            output.write(patched);output.flush();os.fsync(output.fileno())
        if path.read_bytes()!=raw or path.stat().st_ino!=info.st_ino:raise ValueError('ROBOT_SOURCE_CHANGED')
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)
    return {'status':'PATCHED','before_sha256':before,'after_sha256':after,'backup':str(backup)}

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--project',default='/root/harun-ai-trading-office');parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    try:result=apply(args.project,args.apply)
    except Exception as error:result={'error':str(error) if isinstance(error,ValueError) and str(error).isupper() else 'DIAGNOSTIC_PATCH_FAILED'}
    print(json.dumps(result,sort_keys=True));raise SystemExit(1 if 'error' in result else 0)
