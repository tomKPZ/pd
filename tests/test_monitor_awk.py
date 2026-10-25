"""Unit test for the frontier monitor's overlap detection (runs the awk
program from plain/hidden/frontier.sh with busybox awk on the host)."""

import pathlib
import re
import subprocess
import tempfile
import unittest

from tests import vm

SCRIPT = vm.REPO / "plain" / "hidden" / "frontier.sh"

LVS = """\
  swap /dev/mapper/fr-decoy:0-15 8192
  [lvol0_pmspare] /dev/mapper/fr-decoy:16-17 8192
  [thinpool_tdata] /dev/mapper/fr-decoy:18-1980 8192
  [thinpool_tmeta] /dev/mapper/fr-decoy:1981-1982 8192
"""


def overlap(lvs: str) -> str:
    src = SCRIPT.read_text()
    prog = re.search(r"-v hidden=/run/fr-hidden-region.table '\n(.*?\n        )}' \| sort", src,
                     re.S).group(1) + "}\n"
    busybox = vm.testenv() / "busybox" / "bin" / "busybox"
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        (d / "prog.awk").write_text(prog)
        # 8 GiB disk: decoy region from 256 MiB; hidden region two chunks
        # below the hidden boot volume.
        (d / "decoy.t").write_text("0 16252928 linear /dev/vda 524288\n")
        (d / "hidden.t").write_text("0 524288 linear /dev/vda 15204352\n"
                                    "524288 524288 linear /dev/vda 14680064\n")
        (d / "lvs").write_text(lvs)
        return subprocess.run(
            [busybox, "awk", "-v", "pe_start=2048", "-v", f"region={d}/decoy.t",
             "-v", f"hidden={d}/hidden.t", "-f", d / "prog.awk", d / "lvs"],
            check=True, capture_output=True, text=True).stdout.split()


class OverlapAwk(unittest.TestCase):
    def test_stock_layout_is_clean(self):
        self.assertEqual(overlap(LVS), [])

    def test_stray_lv_in_hidden_region(self):
        self.assertEqual(overlap(LVS + "  stray /dev/mapper/fr-decoy:1800-1801 8192\n"), ["stray"])


if __name__ == "__main__":
    unittest.main()
