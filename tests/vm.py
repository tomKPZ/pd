"""QEMU test harness.  Runs as an ordinary user; needs /dev/kvm and Nix."""

import os
import pathlib
import shutil
import subprocess
import tempfile

import pexpect

REPO = pathlib.Path(__file__).resolve().parent.parent
NIX = ["nix", "--extra-experimental-features", "nix-command flakes"]


def testenv() -> pathlib.Path:
    """Build (or reuse) the flake's testenv output and return its path."""
    link = REPO / ".testenv"
    if not os.environ.get("PD_SKIP_NIX_BUILD"):
        subprocess.run(
            [*NIX, "build", "-j", "auto", "--out-link", str(link), f"path:{REPO}#testenv"],
            check=True,
        )
    return link.resolve()


def sparse_disk(path: pathlib.Path, size: int) -> pathlib.Path:
    with open(path, "wb") as f:
        f.truncate(size)
    return path


def _qemu_base(memory: str = "4G", smp: int = 4) -> list[str]:
    return [
        "qemu-system-x86_64", "-enable-kvm", "-cpu", "host", "-m", memory, "-smp", str(smp),
        "-nographic", "-no-reboot", "-device", "virtio-rng-pci",
    ]


def _disk_args(disks, interface: str = "virtio", readonly: bool = False) -> list[str]:
    ro = ",readonly=on" if readonly else ""
    args = []
    for i, d in enumerate(disks):
        if interface == "virtio":
            args += ["-drive", f"file={d},if=virtio,format=raw,cache=unsafe,discard=unmap{ro}"]
        else:
            args += ["-drive", f"file={d},if=none,id=d{i},format=raw,cache=unsafe,discard=unmap{ro}",
                     "-device", f"nvme,drive=d{i},serial=pd{i}"]
    return args


class ToolVM:
    """Runs a shell script as root inside a throwaway VM with the given disks.

    The host's /nix/store and the repo are shared read-only; `work` is a
    shared read-write scratch directory (the script's cwd).
    """

    def __init__(self, env: pathlib.Path, work: pathlib.Path):
        self.env = env
        self.work = work

    def run(self, script: str, disks=(), timeout: int = 900, hostname: str = "localhost",
            check: bool = True, log: pathlib.Path | None = None, network: bool = False,
            cache: pathlib.Path | None = None) -> tuple[int, str]:
        """network: QEMU user networking (outbound only).  cache: a host
        directory mounted read-write at /cache, kept across runs."""
        (self.work / "run.sh").write_text("set -euo pipefail\n" + script)
        (self.work / "hostname").write_text(hostname)
        args = _qemu_base() + [
            "-kernel", str(self.env / "bzImage"),
            "-initrd", str(self.env / "toolvm-initrd.cpio.zst"),
            "-append", "console=ttyS0 quiet panic=-1 init_on_free=1" + (" pd.net" if network else ""),
            "-virtfs", "local,path=/nix/store,mount_tag=store,security_model=none,readonly=on",
            "-virtfs", f"local,path={REPO},mount_tag=src,security_model=none,readonly=on",
            "-virtfs", f"local,path={self.work},mount_tag=work,security_model=none",
        ] + _disk_args(disks)
        if network:
            args += ["-nic", "user,model=virtio-net-pci"]
        if cache:
            cache.mkdir(parents=True, exist_ok=True)
            args += ["-virtfs", f"local,path={cache},mount_tag=cache,security_model=none"]
        proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, timeout=timeout)
        out = proc.stdout.decode(errors="replace")
        if log:
            log.write_text(out)
        rc = None
        for line in out.splitlines():
            if line.startswith("PD-EXIT "):
                rc = int(line.split()[1])
        if rc is None:
            rc = -1
        if check and rc != 0:
            raise RuntimeError(f"tool VM script failed (rc={rc}):\n{out[-6000:]}")
        return rc, out


class BootVM:
    """Boots the pd boot environment (kernel + initrds) interactively."""

    def __init__(self, env: pathlib.Path, initrds: list[pathlib.Path], disks, cmdline: str = "",
                 memory: str = "2G", interface: str = "virtio", log=None, uefi: dict | None = None,
                 readonly_disks: bool = False, network: bool = False):
        self.env = env
        if uefi:
            args = _qemu_base(memory) + [
                "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={uefi['code']}",
                "-drive", f"if=pflash,format=raw,unit=1,file={uefi['vars']}",
                "-drive", f"file={uefi['esp']},if=none,id=usb,format=raw",
                "-device", "qemu-xhci", "-device", "usb-storage,drive=usb,bootindex=0",
            ] + _disk_args(disks, interface, readonly_disks)
        else:
            initrd = self._concat_initrds(initrds)
            args = _qemu_base(memory) + [
                "-kernel", str(env / "bzImage"), "-initrd", str(initrd),
                "-append", f"console=ttyS0 panic=-1 init_on_free=1 {cmdline}".strip(),
            ] + _disk_args(disks, interface, readonly_disks)
        if network:
            args += ["-nic", "user,model=virtio-net-pci"]
        self.child = pexpect.spawn(args[0], args[1:], timeout=180, encoding="utf-8",
                                   codec_errors="replace")
        # Log only what the guest prints, not what the test types.
        self.child.logfile_read = log
        # Only the guest should echo; otherwise typed passwords show up in
        # the log even when the guest hides them.
        self.child.setecho(False)

    _tmp = None

    def _concat_initrds(self, initrds) -> pathlib.Path:
        BootVM._tmp = BootVM._tmp or pathlib.Path(tempfile.mkdtemp(prefix="pd-initrd-"))
        out = BootVM._tmp / f"initrd-{os.getpid()}-{id(self)}"
        with open(out, "wb") as f:
            for p in initrds:
                data = pathlib.Path(p).read_bytes()
                f.write(data)
                f.write(b"\0" * (-len(data) % 4))
        return out

    def expect(self, pattern, timeout=180):
        return self.child.expect(pattern, timeout=timeout)

    def sendline(self, s: str):
        self.child.sendline(s)

    def close(self):
        if self.child.isalive():
            self.child.terminate(force=True)


def workdir(prefix: str = "pd-test-") -> pathlib.Path:
    base = os.environ.get("PD_TEST_DIR")
    if base:
        pathlib.Path(base).mkdir(parents=True, exist_ok=True)
    return pathlib.Path(tempfile.mkdtemp(prefix=prefix, dir=base))


def cleanup(path: pathlib.Path):
    if not os.environ.get("PD_KEEP_TEST_DIR"):
        shutil.rmtree(path, ignore_errors=True)

