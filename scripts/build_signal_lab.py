"""Refresh embedded fixtures from the Python contract; no network calls."""
import json
import re
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from common.signal_lab import LabRequest, PERSONAS, TOPICS, compare

path = ROOT / "docs" / "signal-lab.html"
data = {p + ":" + t: compare(LabRequest(p, t)) for p in PERSONAS for t in TOPICS}
encoded = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
text, count = re.subn(r'(<script id="lab-data" type="application/json">).*?(</script>)',
                      lambda m: m.group(1) + encoded + m.group(2), path.read_text(),
                      flags=re.DOTALL)
if count != 1:
    raise ValueError("Expected exactly one embedded data block")
path.write_text(text)
