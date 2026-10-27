"""FIDO2 security keys (design.md §4.1), with QEMU's emulated CanoKey.

One scenario per mode, since the fixture is slow to build: enroll two
security keys for both sides (swapping the emulated key, as a user with one
USB port would), open a side with `pd open`, boot both sides with either
key, fail without one, survive a wrong PIN, change a password, and remove a
key.  Boot mode keeps the slots on the USB; data mode in the boot volume.
"""

import re
import unittest

from pd import usb
from pd.config import Config
from tests import fixture, vm

PIN = "123456"
NEW_DECOY = "new decoy password"

# Runs a pd command with --password-stdin as a coprocess: feeds it the given
# lines, and answers "Next security key?" by plugging in each of the listed
# emulated keys, then "done".
WITH_KEYS = r"""
pd_with_keys() {
    local input=$1 keys=$2
    shift 2
    coproc PD { python3 -m pd "$@" --password-stdin 2>&1; }
    local pid=$PD_PID
    exec 3>&"${PD[1]}" 4<&"${PD[0]}"
    printf '%b' "$input" >&3
    set -- $keys
    while IFS= read -r line <&4; do
        echo "$line"
        case $line in
        "Next security key?"*)
            if [ $# -gt 0 ]; then
                plug_security_key "$1"
                shift
                echo next >&3
            else
                echo done >&3
            fi ;;
        esac
    done
    exec 3>&- 4<&-
    wait $pid
}
"""


class Fido2(unittest.TestCase):
    def boot(self, fx, name, key):
        b = fx.boot(name, security_key=key)
        self.addCleanup(b.logfile.close)
        self.addCleanup(b.close)
        return b

    def test_fido2(self):
        fx = fixture.make()
        self.addCleanup(vm.cleanup, fx.work)
        toml = fx.work / "pd.toml"
        # Top level, before any table.
        toml.write_text('fido2_mode = "boot"\nfido2 = "fido2.txt"\n' + toml.read_text())
        keys = [fx.work / "key0", fx.work / "key1"]
        offset = fixture.hidden_offset_sectors(fx)
        dpw, hpw = fixture.DECOY_PW, fixture.HIDDEN_PW

        for k in keys:
            vm.init_security_key(fx.env, fx.work, k, PIN)
        _, out = fx.toolvm().run(WITH_KEYS + f"""
        wait_security_key

        # The decoy: key 0, then key 1.  Then the hidden side, starting with
        # key 1 (which the decoy's enrollment left plugged in).
        pd_with_keys '{dpw}\\n{PIN}\\n' 1 fido2 enroll --config pd.toml
        pd_with_keys '{hpw}\\n{PIN}\\n' 0 fido2 enroll --config pd.toml --offset {offset}
        echo "LINES $(wc -l < fido2.txt)"
        echo "FIELDS $(awk '{{print NF, length($2), length($3)}}' fido2.txt | sort -u)"

        printf '%s\\n%s\\n' '{dpw}' {PIN} | python3 -m pd open --config pd.toml --password-stdin \\
            --run 'echo "OPENED $(cat fake/etc/pd-marker)"'
        python3 -m pd fido2 list --config pd.toml
        """, disks=[fx.disk], security_keys=keys, log=fx.work / "enroll.log")
        self.assertIn("LINES 2", out)
        self.assertIn("FIELDS 3 128 128", out)
        self.assertIn("OPENED decoy", out)
        self.assertEqual(len(re.findall(r"Enrolled 2 security key", out)), 2, out[-3000:])

        cfg = Config.load(fx.work / "pd.toml")
        (fx.work / "install.cpio").write_bytes(usb.install_cpio(cfg))

        # Decoy with key 1, after one wrong PIN.
        b = self.boot(fx, "decoy-key1", keys[1])
        b.expect("Password: ")
        b.sendline(dpw)
        b.expect("Security key PIN: ")
        b.sendline("000000")
        b.expect("Wrong PIN")
        b.expect("Security key PIN: ")
        b.sendline(PIN)
        b.expect("PD-TARGET: marker=decoy", timeout=120)
        b.close()

        # Hidden with key 0.
        b = self.boot(fx, "hidden-key0", keys[0])
        b.expect("Password: ")
        b.sendline("")
        b.expect("recovery shell")
        b.sendline(fx.sed)
        b.sendline("/early.sh")
        b.expect("Password: ")
        b.sendline(hpw)
        b.expect("Security key PIN: ")
        b.sendline(PIN)
        b.expect("PD-TARGET: marker=hidden", timeout=120)
        b.close()

        # A security key that was never enrolled: the password alone doesn't
        # open the decoy any more.
        stranger = fx.work / "stranger"
        vm.init_security_key(fx.env, fx.work, stranger, PIN)
        b = self.boot(fx, "decoy-stranger", stranger)
        b.expect("Password: ")
        b.sendline(dpw)
        b.expect("Security key PIN: ")
        b.sendline(PIN)
        b.expect("No security key found", timeout=120)
        b.expect("No bootable disk found")
        b.close()

        # Change the decoy password: both keys are re-wrapped, one at a time,
        # around a new boot-volume key, so the old fido2.txt with the old
        # password no longer opens it.
        _, out = fx.toolvm().run(WITH_KEYS + f"""
        wait_security_key
        cp fido2.txt fido2.old
        sed 's/^fido2 = "fido2.txt"/fido2 = "fido2.old"/' pd.toml > old.toml
        pd_with_keys '{dpw}\\n{NEW_DECOY}\\n{PIN}\\n' 1 rekey --config pd.toml
        printf '%s\\n%s\\n' '{dpw}' {PIN} | python3 -m pd open --config old.toml --password-stdin \\
            --run 'echo OLD-OPENED' || echo OLD-REFUSED
        """, disks=[fx.disk], security_keys=keys, log=fx.work / "rekey.log")
        self.assertIn("Re-keyed", out)
        self.assertNotIn("Warning", out)
        self.assertIn("OLD-REFUSED", out)
        self.assertNotIn("OLD-OPENED", out)
        cfg = Config.load(fx.work / "pd.toml")
        (fx.work / "install.cpio").write_bytes(usb.install_cpio(cfg))
        b = self.boot(fx, "decoy-new-password", keys[0])
        b.expect("Password: ")
        b.sendline(NEW_DECOY)
        b.expect("Security key PIN: ")
        b.sendline(PIN)
        b.expect("PD-TARGET: marker=decoy", timeout=120)
        b.close()

        # Remove key 1; removing the last one is refused.
        _, out = fx.toolvm().run("""
        python3 -m pd fido2 remove --config pd.toml 1
        echo "LINES $(wc -l < fido2.txt)"
        python3 -m pd fido2 remove --config pd.toml 0 || echo REFUSED
        """, disks=[fx.disk], log=fx.work / "remove.log")
        self.assertIn("LINES 1", out)
        self.assertIn("REFUSED", out)

    def test_data_mode(self):
        """The default mode: the boot volume stays keyed by the password, and
        the security key unwraps the data key (and the hidden side's copy of
        the decoy's) in late.sh.  Nothing goes on the USB, and the plaintext
        data key doesn't survive in the boot volume."""
        fx = fixture.make()
        self.addCleanup(vm.cleanup, fx.work)
        keys = [fx.work / "key0", fx.work / "key1"]
        offset = fixture.hidden_offset_sectors(fx)
        dpw, hpw = fixture.DECOY_PW, fixture.HIDDEN_PW
        for k in keys:
            vm.init_security_key(fx.env, fx.work, k, PIN)
        # Prints whether the decoy's data key, read before enrolling, still
        # appears anywhere in its decrypted boot volume, and the files there.
        probe = f"""
        python3 - <<'PY'
import pathlib
from pd import layout as L, setup
from pd.config import Config
from pd.util import argon2, plain_crypt
cfg = Config.load("pd.toml")
a = argon2(b"{dpw}", setup.header.salt(cfg), cfg.argon2)
with setup.boot_volume(cfg.disk, L.DECOY_PLAIN_OFFSET, a, create=False) as mnt:
    print("FILES", " ".join(sorted(p.name for p in mnt.iterdir())))
    if (mnt / "data.key").exists():
        pathlib.Path("/work/decoy.key.bin").write_bytes((mnt / "data.key").read_bytes())
key = pathlib.Path("/work/decoy.key.bin").read_bytes()
with plain_crypt("probe", cfg.disk, a, setup.CIPHER_ARGS, offset=L.sectors(L.DECOY_PLAIN_OFFSET),
                 size=L.sectors(L.PLAIN_SIZE), readonly=True) as dev:
    print("PLAINTEXT-KEY", key in open(dev, "rb").read())
PY
        """
        _, out = fx.toolvm().run(WITH_KEYS + probe + f"""
        wait_security_key
        pd_with_keys '{dpw}\\n{PIN}\\n' 1 fido2 enroll --config pd.toml
        pd_with_keys '{hpw}\\n{PIN}\\n' 0 fido2 enroll --config pd.toml --offset {offset}
        """ + probe + f"""
        printf '%s\\n%s\\n' '{dpw}' {PIN} | python3 -m pd open --config pd.toml --password-stdin \\
            --run 'echo "OPENED $(cat fake/etc/pd-marker)"'
        printf '%s\\n' '{dpw}' | python3 -m pd fido2 list --config pd.toml --password-stdin
        """, disks=[fx.disk], security_keys=keys, log=fx.work / "enroll.log")
        before, after = out.split("PLAINTEXT-KEY", 2)[1:3] if out.count("PLAINTEXT-KEY") == 2 else ("", "")
        self.assertIn("True", before.splitlines()[0])
        self.assertIn("False", after.splitlines()[0])
        files = re.findall(r"^FILES (.*)$", out, re.M)
        self.assertIn("data.key", files[0].split())
        self.assertNotIn("data.key", files[1].split())
        self.assertIn("fido2.txt", files[1].split())
        self.assertIn("OPENED decoy", out)
        self.assertEqual(len(re.findall(r"Enrolled 2 security key", out)), 2, out[-3000:])
        self.assertEqual(len(re.findall(r"^\d+: [0-9a-f]{16}", out, re.M)), 2)
        # Nothing for the USB: the boot image alone.
        cfg = Config.load(fx.work / "pd.toml")
        self.assertNotIn("fido2", usb.credentials(cfg))

        # Decoy with key 1, after one wrong PIN: the PIN is asked after the
        # boot volume opens.
        b = self.boot(fx, "data-decoy-key1", keys[1])
        b.expect("Password: ")
        b.sendline(dpw)
        b.expect("Security key PIN: ")
        b.sendline("000000")
        b.expect("Wrong PIN")
        b.expect("Security key PIN: ")
        b.sendline(PIN)
        b.expect("PD-TARGET: marker=decoy", timeout=120)
        b.close()

        # Hidden with key 0: the frontier monitor gets the decoy's data key
        # from the second slot.
        b = self.boot(fx, "data-hidden-key0", keys[0])
        b.expect("Password: ")
        b.sendline("")
        b.expect("recovery shell")
        b.sendline(fx.sed)
        b.sendline("/early.sh")
        b.expect("Password: ")
        b.sendline(hpw)
        b.expect("Security key PIN: ")
        b.sendline(PIN)
        b.expect(r"Free space between the two sides: \d+ MiB", timeout=120)
        b.expect("PD-TARGET: marker=hidden", timeout=120)
        b.close()

        # A key that was never enrolled: the boot volume opens, the data doesn't.
        stranger = fx.work / "stranger"
        vm.init_security_key(fx.env, fx.work, stranger, PIN)
        b = self.boot(fx, "data-stranger", stranger)
        b.expect("Password: ")
        b.sendline(dpw)
        b.expect("Security key PIN: ")
        b.sendline(PIN)
        b.expect("No security key found", timeout=120)
        b.expect("recovery shell")
        b.close()

        # A new decoy password: both keys re-wrapped; then remove key 1.
        _, out = fx.toolvm().run(WITH_KEYS + f"""
        wait_security_key
        pd_with_keys '{dpw}\\n{NEW_DECOY}\\n{PIN}\\n' 1 rekey --config pd.toml
        printf '%s\\n' '{NEW_DECOY}' | python3 -m pd fido2 remove --config pd.toml --password-stdin 1
        printf '%s\\n' '{NEW_DECOY}' | python3 -m pd fido2 list --config pd.toml --password-stdin
        printf '%s\\n' '{NEW_DECOY}' | python3 -m pd fido2 remove --config pd.toml --password-stdin 0 \\
            || echo REFUSED
        """, disks=[fx.disk], security_keys=keys, log=fx.work / "rekey.log")
        self.assertIn("Re-keyed", out)
        self.assertNotIn("Warning", out)
        self.assertIn("Removed.", out)
        self.assertEqual(len(re.findall(r"^\d+: [0-9a-f]{16}", out, re.M)), 1)
        self.assertIn("REFUSED", out)
        b = self.boot(fx, "data-decoy-new-password", keys[0])
        b.expect("Password: ")
        b.sendline(NEW_DECOY)
        b.expect("Security key PIN: ")
        b.sendline(PIN)
        b.expect("PD-TARGET: marker=decoy", timeout=120)
        b.close()


if __name__ == "__main__":
    unittest.main()
