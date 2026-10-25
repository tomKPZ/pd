"""Stock boot from a real live image (docs/stock-boot.md): opt-in with
PD_TEST_LIVE=1, since it downloads Debian Live (Xfce, about 3.8 GB, into
~/.cache/pd-tests/live, checked against Debian's SHA256SUMS).

Like test_stock, but the hidden boot runs in the stock live system: its
kernel, systemd and udev, its cryptsetup and OpenSSL.  The ISO's kernel and
initrd are booted directly (to get a serial console), with the ISO attached
as the live medium; the test logs in on the serial console, becomes root,
and types the commands `pd init-hidden --stock-decoy` printed.

The Xfce image is used because Debian's "standard" live image has no
cryptsetup.  The graphical session isn't started (multi-user target).
"""

import hashlib
import os
import pathlib
import subprocess
import unittest
import urllib.request

import pexpect

from tests import fixture, test_stock, vm

MIRROR = "https://cdimage.debian.org/debian-cd/current-live/amd64/iso-hybrid/"
FLAVOR = "xfce"
CACHE = pathlib.Path(os.environ.get("PD_TEST_CACHE", pathlib.Path.home() / ".cache" / "pd-tests")) / "live"


def live_image() -> pathlib.Path:
    """The current Debian Live image, downloaded once and verified."""
    CACHE.mkdir(parents=True, exist_ok=True)
    sums = urllib.request.urlopen(MIRROR + "SHA256SUMS", timeout=60).read().decode()
    name, digest = next((line.split()[1], line.split()[0]) for line in sums.splitlines()
                        if line.endswith(f"-amd64-{FLAVOR}.iso"))
    iso = CACHE / name
    if not iso.exists():
        part = iso.with_suffix(".part")
        subprocess.run(["curl", "-fL", "--retry", "3", "-o", part, MIRROR + name], check=True)
        part.rename(iso)
    stamp = iso.with_suffix(".verified")
    if not stamp.exists():
        h = hashlib.sha256()
        with open(iso, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        if h.hexdigest() != digest:
            iso.unlink()
            raise RuntimeError(f"{name}: checksum mismatch (deleted; run again)")
        stamp.write_text(digest)
    return iso


def extract(env: pathlib.Path, iso: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """The live system's kernel and initrd, from the ISO (no root needed)."""
    out = iso.with_suffix(".boot")
    kernel, initrd = out / "vmlinuz", out / "initrd.img"
    if not (kernel.exists() and initrd.exists()):
        out.mkdir(exist_ok=True)
        subprocess.run([env / "xorriso" / "bin" / "xorriso", "-osirrox", "on", "-indev", iso,
                        "-extract", "/live/vmlinuz", kernel,
                        "-extract", "/live/initrd.img", initrd],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return kernel, initrd


@unittest.skipUnless(os.environ.get("PD_TEST_LIVE"), "set PD_TEST_LIVE=1 (downloads Debian Live)")
class LiveBoot(unittest.TestCase):
    def test_hidden_boot_from_debian_live(self):
        iso = live_image()
        st = test_stock.make_stock(self)
        kernel, initrd = extract(st.env, iso)

        args = vm._qemu_base("4G") + [
            "-kernel", str(kernel), "-initrd", str(initrd),
            "-append", "boot=live components console=ttyS0 systemd.unit=multi-user.target",
            "-drive", f"file={iso},media=cdrom,readonly=on",
        ] + vm._disk_args([st.disk])
        log = open(st.work / "live.log", "w")
        self.addCleanup(log.close)
        child = pexpect.spawn(args[0], args[1:], timeout=300, encoding="utf-8", codec_errors="replace")
        child.logfile_read = log
        child.setecho(False)
        self.addCleanup(lambda: child.terminate(force=True))

        # Debian Live's account: user "user", password "live", sudo without a password.
        child.expect("login: ", timeout=600)
        child.sendline("user")
        child.expect("Password: ")
        child.sendline("live")
        child.expect(r"\$ ")
        child.sendline("sudo -i")
        child.expect(r"# ")
        child.sendline("openssl version; cryptsetup --version")
        child.expect(r"OpenSSL 3\.\d+")
        child.expect(r"# ")

        # The commands, as a user types them; `read -rs P` waits for the password.
        child.sendline(st.commands[0])
        child.sendline(fixture.HIDDEN_PW)
        for line in st.commands[1:]:
            child.sendline(line)
        child.expect(r"Free space between the two sides: \d+ MiB", timeout=300)
        i = child.expect(["PD-TARGET: marker=hidden", "WARNING: decoy volume", "WARNING: less than",
                          "Frontier check failed", "awk:", pexpect.EOF], timeout=300)
        self.assertEqual(i, 0, f"hidden boot from the live system failed; see {st.work / 'live.log'}")


if __name__ == "__main__":
    unittest.main()

