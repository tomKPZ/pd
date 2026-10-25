"""FIDO2 security keys (design.md §4.1), with QEMU's emulated CanoKey.

One scenario, since the fixture is slow to build: enroll two security keys
for both sides (swapping the emulated key, as a user with one USB port
would), open a side with `pd open`, boot both sides with either key, fail
without one, survive a wrong PIN, change a password, and remove a key.
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
        toml.write_text('fido2 = "fido2.txt"\n' + toml.read_text())  # top level, before any table
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

        # Change the decoy password: both keys are re-wrapped, one at a time.
        _, out = fx.toolvm().run(WITH_KEYS + f"""
        wait_security_key
        pd_with_keys '{dpw}\\n{NEW_DECOY}\\n{PIN}\\n' 1 rekey --config pd.toml
        """, disks=[fx.disk], security_keys=keys, log=fx.work / "rekey.log")
        self.assertIn("Re-keyed", out)
        self.assertNotIn("Warning", out)
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


if __name__ == "__main__":
    unittest.main()
