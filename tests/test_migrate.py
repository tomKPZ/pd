"""Migrate a v1-format disk to v2 in place, then boot both sides with v2."""

import re
import secrets
import unittest

from pd import usb
from pd.config import Config
from tests import vm

DECOY_PW = "decoy password"
HIDDEN_PW = "hidden password"

CONFIG = """
disk = "/dev/vda"
salt = "{salt}"
vg = "sysvg"

[argon2]
t = 1
m_log2 = 12
p = 1
"""


class MigrateV1(unittest.TestCase):
    def test_migrate_and_boot(self):
        env = vm.testenv()
        work = vm.workdir()
        self.addCleanup(vm.cleanup, work)
        disk = vm.sparse_disk(work / "disk.img", 8 << 30)
        (work / "pd.toml").write_text(CONFIG.format(salt=secrets.token_hex(32)))
        v1_salt = secrets.token_hex(32)
        tool = vm.ToolVM(env, work)
        _, out = tool.run(
            f"python3 /src/tests/make_v1.py /dev/vda {v1_salt} '{DECOY_PW}' '{HIDDEN_PW}' "
            f"{env}/bzImage {env}/faketarget-initrd.cpio.zst",
            disks=[disk], log=work / "make-v1.log")
        offset = int(re.search(r"V1-HIDDEN-OFFSET (\d+)", out).group(1))
        tool.run(
            f"printf '%s\\n%s\\n' '{DECOY_PW}' '{HIDDEN_PW}' | python3 -m pd migrate-v1 "
            f"--config pd.toml --v1-salt {v1_salt} --v1-hidden-offset {offset} "
            f"--v1-argon2 1,12,1 --password-stdin",
            disks=[disk], log=work / "migrate.log")

        cfg = Config.load(work / "pd.toml")
        (work / "install.cpio").write_bytes(usb.install_cpio(cfg))
        initrds = [env / "boot-initrd.cpio.zst", work / "install.cpio"]

        for name, steps, marker in [
            ("decoy", [DECOY_PW], "decoy"),
            ("hidden", ["", f"sed -i s/2048/{offset}/ /early_env.sh", "/early.sh", HIDDEN_PW],
             "hidden"),
        ]:
            with open(work / f"boot-{name}.log", "w") as log:
                b = vm.BootVM(env, initrds, [disk], log=log)
                try:
                    b.expect("Password: ")
                    b.sendline(steps[0])
                    if len(steps) > 1:
                        b.expect("recovery shell")
                        b.sendline(steps[1])
                        b.sendline(steps[2])
                        b.expect("Password: ")
                        b.sendline(steps[3])
                    b.expect(f"PD-TARGET: marker={marker}", timeout=180)
                finally:
                    b.close()


if __name__ == "__main__":
    unittest.main()

