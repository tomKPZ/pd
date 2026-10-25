"""pd open / pd close: read-only by default (checked with the disk attached
read-only), read-write with --rw, the hidden side with --offset, and
everything torn down by close.  Runs pd as a zipapp (pd.pyz), the way it's
used from a live ISO."""

import pathlib
import shutil
import tempfile
import unittest
import zipapp

from tests import fixture, vm


def build_pyz(out: pathlib.Path) -> pathlib.Path:
    with tempfile.TemporaryDirectory() as d:
        shutil.copytree(vm.REPO / "pd", pathlib.Path(d) / "pd",
                        ignore=shutil.ignore_patterns("__pycache__"))
        zipapp.create_archive(d, out, interpreter="/usr/bin/env python3", main="pd.cli:entry")
    return out


class OpenClose(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = fixture.make()
        build_pyz(cls.fx.work / "pd.pyz")

    @classmethod
    def tearDownClass(cls):
        vm.cleanup(cls.fx.work)

    def run_vm(self, script, **kwargs):
        return vm.ToolVM(self.fx.env, self.fx.work).run(script, disks=[self.fx.disk], **kwargs)

    def test_read_only_open_and_close(self):
        offset = fixture.hidden_offset_sectors(self.fx)
        _, out = self.run_vm(f"""
        pd="python3 /work/pd.pyz"
        printf '%s\\n' '{fixture.DECOY_PW}' | $pd open --config pd.toml --password-stdin
        echo "MARKER $(cat /mnt/pd/fake/etc/pd-marker)"
        echo "ROOT-OPTS $(findmnt -n -o OPTIONS /mnt/pd/fake)"
        echo "VOLUME $(findmnt -n -o FSTYPE /mnt/pd/volumes/data)"
        $pd close
        echo "AFTER-CLOSE $(dmsetup info -c --noheadings -o name 2>/dev/null | grep -vc 'No devices' || true) $(findmnt -n /mnt/pd | wc -l) $(test -e /mnt/pd && echo dir || echo nodir)"
        printf '%s\\n' '{fixture.HIDDEN_PW}' | $pd open --config pd.toml --password-stdin --offset {offset}
        echo "MARKER $(cat /mnt/pd/fake/etc/pd-marker)"
        $pd close
        """, readonly_disks=True)
        self.assertIn("MARKER decoy", out)
        self.assertIn("MARKER hidden", out)
        self.assertRegex(out, r"ROOT-OPTS ro,")
        self.assertIn("VOLUME ext4", out)
        self.assertIn("AFTER-CLOSE 0 0 nodir", out)

    def test_read_write_open(self):
        _, out = self.run_vm(f"""
        pd="python3 /work/pd.pyz"
        printf '%s\\n' '{fixture.DECOY_PW}' | $pd open --config pd.toml --password-stdin --rw
        echo hello > /mnt/pd/fake/home/note
        echo hello > /mnt/pd/volumes/data/note
        $pd close
        printf '%s\\n' '{fixture.DECOY_PW}' | $pd open --config pd.toml --password-stdin \\
            --run 'echo "READ $(cat $PD_ROOT/fake/home/note) $(cat $PD_ROOT/volumes/data/note)"'
        echo "AFTER-RUN $(dmsetup info -c --noheadings -o name 2>/dev/null | grep -vc 'No devices' || true)"
        """)
        self.assertIn("READ hello hello", out)
        self.assertIn("AFTER-RUN 0", out)

    def test_wrong_password(self):
        rc, out = self.run_vm("""
        printf 'nope\\n' | python3 /work/pd.pyz open --config pd.toml --password-stdin
        """, check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("doesn't open a boot volume", out)


if __name__ == "__main__":
    unittest.main()
