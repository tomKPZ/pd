"""Shared test fixture: a disk image with a decoy and a hidden side, each
holding the fake target OS."""

import dataclasses
import pathlib
import re
import secrets

from pd import usb
from pd.config import Config
from tests import vm

DECOY_PW = "decoy password"
HIDDEN_PW = "hidden password"

CONFIG = """
disk = "/dev/vda"
salt = "{salt}"
vg = "sysvg"
swap_size = "64M"
hidden_cap = 0.4

[argon2]
t = 1
m_log2 = 12
p = 1

[[systems]]
name = "fake"
lv = "root"
kernel = "/boot/vmlinuz"
initrd = "/boot/initrd"
cmdline = "console=ttyS0 panic=-1 root=/dev/sysvg/root rootflags=subvol=@"

[[volumes]]
name = "data"
fs = "ext4"
"""

INSTALL = """
set -eu
side=$1
root=$PD_ROOT/fake
cp {kernel} $root/boot/vmlinuz
cp {initrd} $root/boot/initrd
mkdir -p $root/etc
echo "$side" > $root/etc/pd-marker
"""


@dataclasses.dataclass
class Fixture:
    env: pathlib.Path
    work: pathlib.Path
    disk: pathlib.Path
    cfg: Config
    sed: str  # the hidden-boot command
    setup_log: str

    def boot(self, name: str, **kwargs) -> vm.BootVM:
        log = open(self.work / f"boot-{name}.log", "w")
        b = vm.BootVM(self.env, [self.env / "boot-initrd.cpio.zst", self.work / "install.cpio"],
                      [self.disk], log=log, **kwargs)
        b.logfile = log
        return b

    def toolvm(self) -> vm.ToolVM:
        return vm.ToolVM(self.env, self.work)


def make(size: int = 8 << 30, kernel=None, initrd=None, randomize: bool = True) -> Fixture:
    """Set up a disk.  `kernel`/`initrd` are the target OS's boot files
    (default: the fake target)."""
    env = vm.testenv()
    kernel = kernel or env / "bzImage"
    initrd = initrd or env / "faketarget-initrd.cpio.zst"
    work = vm.workdir()
    disk = vm.sparse_disk(work / "disk.img", size)
    (work / "pd.toml").write_text(CONFIG.format(salt=secrets.token_hex(32)))
    (work / "install.sh").write_text(INSTALL.format(kernel=kernel, initrd=initrd))
    cfg = Config.load(work / "pd.toml")
    script = f"""
    {"python3 -m pd randomize --config pd.toml" if randomize else ""}
    printf '%s\\n' '{DECOY_PW}' | python3 -m pd init-decoy --config pd.toml --password-stdin --run 'sh /work/install.sh decoy'
    printf '%s\\n%s\\n' '{DECOY_PW}' '{HIDDEN_PW}' | python3 -m pd init-hidden --config pd.toml --password-stdin --run 'sh /work/install.sh hidden' | tee hidden.txt
    """
    _, out = vm.ToolVM(env, work).run(script, disks=[disk], timeout=900, log=work / "setup.log")
    hidden = (work / "hidden.txt").read_text()
    sed = re.search(r"(sed -i s/2048/\d+/ /early_env.sh)", hidden).group(1)
    (work / "install.cpio").write_bytes(usb.install_cpio(cfg))
    return Fixture(env=env, work=work, disk=disk, cfg=cfg, sed=sed, setup_log=out)


def hidden_offset_sectors(fx: Fixture) -> int:
    return int(re.search(r"s/2048/(\d+)/", fx.sed).group(1))

