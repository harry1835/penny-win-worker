"""Windows PowerShell 5.1 reads BOM-less .ps1 files as Windows-1252: a UTF-8
em dash contains byte 0x94, which it takes as a closing quote and the whole
script fails to parse. Keep the scripts plain ASCII."""
from __future__ import annotations

import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class TestScriptsAreAscii(unittest.TestCase):
    def test_powershell_scripts_are_ascii(self):
        for path in sorted(SCRIPTS.glob("*.ps1")):
            for n, line in enumerate(path.read_bytes().splitlines(), 1):
                bad = [b for b in line if b > 0x7F]
                self.assertFalse(bad, f"{path.name}:{n} has non-ASCII bytes: {line!r}")


if __name__ == "__main__":
    unittest.main()
