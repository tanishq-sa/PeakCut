"""JavaScript behavior checks and a syntax check of the app's inline script."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

node = shutil.which("node")
if not node:
    print("SKIP JavaScript checks: install Node.js to run these checks (the app itself needs no Node.js).")
    sys.exit(0)
root = Path(__file__).resolve().parent.parent
source = (root / "web/index.html").read_text(encoding="utf-8").split("<script>")[-1].split("</script>")[0]
with tempfile.TemporaryDirectory(prefix="pc-js-") as folder:
    script = Path(folder) / "app.js"
    script.write_text(source, encoding="utf-8")
    syntax = subprocess.run([node, "--check", str(script)])
behavior = subprocess.run([node, "--test", str(root / "tests/test_ui.cjs")])
print("PASS JavaScript syntax and editor behavior" if syntax.returncode == behavior.returncode == 0 else "FAIL JavaScript checks")
sys.exit(int(bool(syntax.returncode or behavior.returncode)))
