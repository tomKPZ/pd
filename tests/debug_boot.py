"""Interactive debugging helper: boot the boot environment against a disk
image and drop to its recovery shell, then run commands given on stdin."""

import pathlib
import sys

from tests import vm

work = pathlib.Path(sys.argv[1])
cmds = sys.stdin.read().splitlines()
env = vm.testenv()
b = vm.BootVM(env, [env / "boot-initrd.cpio.zst", work / "install.cpio"], [work / "disk.img"],
              log=sys.stdout)
b.child.setecho(False)
b.expect("Password: ")
b.sendline("")
b.expect("~ # ")
for c in cmds:
    b.sendline(c)
    b.expect("~ # ", timeout=120)
b.close()

