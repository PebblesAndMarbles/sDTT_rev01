"""
launch_1278_NCD_sDTT.py  -  stable scheduler entry point for 1278 NCD MT5+MT6.

Point your scheduler at THIS file instead of 1278sDTT_NCD_PIPELINE.py directly.
Edits to the pipeline (logic, config, flags) are picked up next run without
rescheduling.

Nightly default args baked in: --days 3
Override at the command line as needed, e.g.:
    python launch_1278_NCD_sDTT.py --days 60
    python launch_1278_NCD_sDTT.py --verbose

Scheduler entry (ScriptHost / Task Scheduler):
    Program : C:\\Users\\tbatson\\My Programs\\SQLPathFinder3\\Python3\\python.exe
    Args    : //orshfs.intel.com/ORAnalysis$/1276_MAODATA/Config/etch/AME/tbatson/sDTT/sDTT_rev01/launch_1278_NCD_sDTT.py
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("_1278_ncd_pipeline", ROOT / "1278sDTT_NCD_PIPELINE.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

raise SystemExit(_mod.main(["--days", "3", *sys.argv[1:]]))
