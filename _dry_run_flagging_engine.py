import importlib.util
import pathlib
import sys
import pandas as pd

workspace = pathlib.Path(r'\\orshfs.intel.com\ORAnalysis$\1276_MAODATA\Config\etch\AME\tbatson\sDTT\sDTT_rev01')
module_path = workspace / 'sDTT_flagging_engine.py'
spec = importlib.util.spec_from_file_location('sDTT_flagging_engine', module_path)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

original_to_csv = pd.DataFrame.to_csv

def _noop_to_csv(self, *args, **kwargs):
    target = args[0] if args else kwargs.get('path_or_buf', '<unknown>')
    print(f'[dry-run] suppressed write to {target}')

pd.DataFrame.to_csv = _noop_to_csv
mod.pd.DataFrame.to_csv = _noop_to_csv

try:
    result = mod.run_flagging_engine()
    print('\n[dry-run] rows returned:', len(result))
    if not result.empty:
        print('[dry-run] flag types:', sorted(result['FLAG_TYPE'].dropna().unique().tolist()))
except Exception as exc:
    print(f'\n[dry-run] engine failed: {exc}')
    raise
finally:
    pd.DataFrame.to_csv = original_to_csv
