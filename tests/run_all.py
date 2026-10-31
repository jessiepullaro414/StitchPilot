"""Run every test script, each with a time limit, and summarise.   python tests/run_all.py"""
import subprocess
import sys
import time
from pathlib import Path

here = Path(__file__).parent
TESTS = ["run_pipeline", "features", "ai_mock", "gui_smoke", "edit_smoke", "edit_keep_smoke", "simplify_smoke",
         "mac_bits", "project_smoke", "quality_smoke", "fine_smoke", "shape_smoke", "delight_smoke", "web_smoke",
         "lambda_smoke", "imagegen_smoke", "account_smoke", "update_smoke", "theme_shots"]
failed = []
for name in TESTS:
    t = time.time()
    try:
        r = subprocess.run([sys.executable, str(here / f"{name}.py")], capture_output=True, text=True,
                           timeout=300, encoding="utf-8", errors="replace")
        ok = r.returncode == 0 and "Traceback" not in r.stderr
        last = (r.stdout.strip().splitlines() or ["(no output)"])[-1]
    except subprocess.TimeoutExpired:
        ok, last, r = False, "TIMED OUT (a dialog waiting for a click?)", None
    print(f"{'PASS' if ok else 'FAIL'}  {name:15s} {time.time() - t:5.1f}s  {last[:90]}")
    if not ok:
        failed.append(name)
        if r is not None:
            print("      " + "\n      ".join(r.stderr.strip().splitlines()[-8:]))
print("\nall passed" if not failed else f"\nFAILED: {', '.join(failed)}")
sys.exit(1 if failed else 0)
