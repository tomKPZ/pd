"""Smoke test: the tool VM boots, sees its disk, and has the tools."""

import unittest

from tests import vm


class ToolVMSmoke(unittest.TestCase):
    def test_tools_and_disk(self):
        env = vm.testenv()
        work = vm.workdir()
        try:
            disk = vm.sparse_disk(work / "disk.img", 1 << 30)
            rc, out = vm.ToolVM(env, work).run(
                """
                for t in python3 cryptsetup lvm dmsetup thin_check mkfs.btrfs argon2 kexec; do
                    command -v $t >/dev/null || { echo "missing $t"; exit 1; }
                done
                blockdev --getsize64 /dev/vda
                python3 -c 'import pd.layout; print("layout ok")'
                """,
                disks=[disk],
                timeout=300,
            )
            self.assertIn(str(1 << 30), out)
            self.assertIn("layout ok", out)
        finally:
            vm.cleanup(work)


if __name__ == "__main__":
    unittest.main()

