"""
launch_1278_sDTT.py  —  stable scheduler entry point for the 1278 sDTT pipeline.

Point your scheduler at THIS file instead of 1278sDTT_PIPELINE.py directly.
Edits to the pipeline (logic, config, new flags) take effect on the next
scheduled run without any rescheduling required.

Nightly default args baked in: --days 5
Override at the command line as needed, e.g.:
    python launch_1278_sDTT.py --days 120
    python launch_1278_sDTT.py --skip-apc
    python launch_1278_sDTT.py --apc-only

Scheduler entry (Windows Task Scheduler):
    Program : C:\\Users\\tbatson\\My Programs\\SQLPathFinder3\\Python3\\python.exe
    Args    : //orshfs.intel.com/ORAnalysis$/1276_MAODATA/Config/etch/AME/tbatson/sDTT/sDTT_rev01/launch_1278_sDTT.py
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location('_1278_pipeline', ROOT / '1278sDTT_PIPELINE.py')
_mod  = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

raise SystemExit(_mod.main(['--days', '5', *sys.argv[1:]]))
