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


def with_security_key(args: list[str], env: pathlib.Path, key: pathlib.Path | None,
                      qmp: pathlib.Path | None = None) -> list[str]:
    """Add an emulated FIDO2 security key (CanoKey, with hmac-secret) whose
    state (PIN, secrets) persists in the file `key`.  Needs the testenv's
    QEMU, built with CanoKey support.  QEMU emulates only one CanoKey at a
    time; with `qmp`, tests can swap it for another (swap_security_key)."""
    if key is None:
        return args
    out = [str(env / "qemu-canokey" / "bin" / "qemu-system-x86_64")] + args[1:]
    out += ["-device", "qemu-xhci,id=fidoxhci",
            "-device", f"canokey,id=fidokey,bus=fidoxhci.0,file={key}"]
    if qmp:
        out += ["-qmp", f"unix:{qmp},server=on,wait=off"]
    return out


def qmp_command(path: pathlib.Path, command: str, **arguments) -> dict:
    """Run one QMP command on the socket at path and return its reply."""
    import json
    import socket
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(str(path))
        f = s.makefile("rw")
        f.readline()  # greeting
        for msg in ({"execute": "qmp_capabilities"}, {"execute": command, "arguments": arguments}):
            f.write(json.dumps(msg) + "\n")
            f.flush()
            while True:
                reply = json.loads(f.readline())
                if "return" in reply or "error" in reply:
                    break
        return reply


def swap_security_key(qmp: pathlib.Path, key: pathlib.Path) -> None:
    """Unplug the emulated security key and plug in the one stored in `key`."""
    import time
    qmp_command(qmp, "device_del", id="fidokey")
    for _ in range(100):  # device_del completes asynchronously
        reply = qmp_command(qmp, "device_add", driver="canokey", id="fidokey",
                            bus="fidoxhci.0", file=str(key))
        if "error" not in reply:
            return
        time.sleep(0.1)
    raise RuntimeError(f"couldn't plug in {key}: {reply}")


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
            cache: pathlib.Path | None = None, readonly_disks: bool = False,
            security_keys: list[pathlib.Path] | None = None) -> tuple[int, str]:
        """network: QEMU user networking (outbound only).  cache: a host
        directory mounted read-write at /cache, kept across runs.

        security_keys: emulated FIDO2 security keys (state files, see
        with_security_key).  The first is plugged in; the script can run
        `plug_security_key N` to swap in the Nth (0-based) instead."""
        prelude = "set -euo pipefail\n"
        if security_keys:
            prelude += PLUG_SECURITY_KEY
        (self.work / "run.sh").write_text(prelude + script)
        (self.work / "hostname").write_text(hostname)
        args = _qemu_base() + [
            "-kernel", str(self.env / "bzImage"),
            "-initrd", str(self.env / "toolvm-initrd.cpio.zst"),
            "-append", "console=ttyS0 quiet panic=-1 init_on_free=1" + (" pd.net" if network else ""),
            "-virtfs", "local,path=/nix/store,mount_tag=store,security_model=none,readonly=on",
            "-virtfs", f"local,path={REPO},mount_tag=src,security_model=none,readonly=on",
            "-virtfs", f"local,path={self.work},mount_tag=work,security_model=none",
        ] + _disk_args(disks, readonly=readonly_disks)
        if network:
            args += ["-nic", "user,model=virtio-net-pci"]
        if cache:
            cache.mkdir(parents=True, exist_ok=True)
            args += ["-virtfs", f"local,path={cache},mount_tag=cache,security_model=none"]
        qmp = None
        if security_keys:
            qmp = pathlib.Path(tempfile.mkdtemp(prefix="pd-qmp-")) / "qmp"
            args = with_security_key(args, self.env, security_keys[0], qmp)
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
        server = None
        if qmp:
            import threading
            server = threading.Thread(target=self._serve_plug_requests,
                                      args=(proc, qmp, security_keys), daemon=True)
            server.start()
        try:
            stdout, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
        finally:
            if server:
                server.join()
            if qmp:
                shutil.rmtree(qmp.parent, ignore_errors=True)
        out = stdout.decode(errors="replace")
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


    def _serve_plug_requests(self, proc, qmp: pathlib.Path, keys: list[pathlib.Path]) -> None:
        import time
        request, done = self.work / "plug-request", self.work / "plug-done"
        while proc.poll() is None:
            if request.exists():
                text = request.read_text().strip()
                if text:  # the guest writes it with mv, but be safe
                    request.unlink()
                    swap_security_key(qmp, keys[int(text)])
                    done.write_text(text)
            time.sleep(0.1)


def init_security_key(env: pathlib.Path, work: pathlib.Path, key: pathlib.Path, pin: str) -> None:
    """Create an emulated security key's state file and set its PIN.  A
    CanoKey hot-plugged with a new state file doesn't finish initializing
    (making credentials fails), so each one starts out in its own VM."""
    ToolVM(env, work).run(f"wait_security_key\necho {pin} | fido2-hmac setpin $(fido2-hmac list)\n",
                          security_keys=[key], timeout=120)


# Shell function for ToolVM scripts: swap the emulated security key and wait
# until the new one is usable.
PLUG_SECURITY_KEY = """
plug_security_key() {
    rm -f /work/plug-done
    echo "$1" >/work/plug-request.tmp
    mv /work/plug-request.tmp /work/plug-request
    while [ ! -e /work/plug-done ]; do sleep 0.1; done
    rm -f /work/plug-done
    sleep 1
    for _ in $(seq 100); do [ -n "$(fido2-hmac list)" ] && return; sleep 0.1; done
    echo "no security key after plugging in $1" >&2
    return 1
}
wait_security_key() {
    for _ in $(seq 100); do [ -n "$(fido2-hmac list)" ] && return; sleep 0.1; done
    echo "no security key" >&2
    return 1
}
"""


class BootVM:
    """Boots the pd boot environment (kernel + initrds) interactively."""

    def __init__(self, env: pathlib.Path, initrds: list[pathlib.Path], disks, cmdline: str = "",
                 memory: str = "2G", interface: str = "virtio", log=None, uefi: dict | None = None,
                 readonly_disks: bool = False, network: bool = False,
                 extra_args: list[str] | None = None, qmp: pathlib.Path | None = None,
                 security_key: pathlib.Path | None = None):
        """security_key: an emulated FIDO2 security key (with_security_key)."""
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
        if qmp:
            args += ["-qmp", f"unix:{qmp},server=on,wait=off"]
        args += extra_args or []
        args = with_security_key(args, env, security_key)
        self.qmp_path = qmp
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

    def qmp(self, command: str, **arguments):
        """Run a QMP command (e.g. device_add) and return its reply."""
        import json
        import socket
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(str(self.qmp_path))
            f = s.makefile("rw")
            f.readline()  # greeting
            for msg in ({"execute": "qmp_capabilities"},
                        {"execute": command, "arguments": arguments}):
                f.write(json.dumps(msg) + "\n")
                f.flush()
                while True:
                    reply = json.loads(f.readline())
                    if "return" in reply or "error" in reply:
                        break
            return reply

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

