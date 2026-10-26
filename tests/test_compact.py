"""pd compact (docs/design.md §10.4): a decoy whose pool was once nearly full
has allocations spread over the whole disk, so there's no room for a hidden
side until its data is moved to the front.

For a pd decoy and for a stock LUKS decoy: fill the decoy, delete most of it,
check that init-hidden refuses, compact, check the remaining files, then
create the hidden side and boot both sides.
"""

import re
import unittest

from tests import fixture, test_stock, vm

# 90 incompressible 64 MiB files (5.6 GiB of an 8 GiB disk), then every file
# but each sixth deleted: what's left (960 MiB) is spread over the pool, which
# still maps everything (nothing trimmed yet).
FILL = r"""
fill() {{
    dir=$1
    i=0
    while [ $i -lt {count} ]; do head -c 64M /dev/urandom > $dir/f$i; i=$((i + 1)); done
    sync
    i=0
    while [ $i -lt {count} ]; do [ $((i % 6)) -eq 0 ] || rm $dir/f$i; i=$((i + 1)); done
    sync
    (cd $dir && sha256sum f* > /work/sums.txt)
}}
"""


def high_water(out: str) -> tuple[int, int, int]:
    m = re.search(r"Highest block: (\d+) MiB into the pool before, (\d+) MiB after \(boundary (\d+) MiB\)", out)
    return tuple(int(x) for x in m.groups())


class Compact(unittest.TestCase):
    def test_v1_decoy_gets_a_hidden_side(self):
        """The path for a deployed v1 install without a hidden side whose
        decoy was once nearly full: migrate, compact, add a hidden side."""
        env = vm.testenv()
        work = vm.workdir()
        self.addCleanup(vm.cleanup, work)
        disk = vm.sparse_disk(work / "disk.img", 8 << 30)
        (work / "pd.toml").write_text(fixture.CONFIG.format(salt="0123456789abcdef0123456789abcdef"))
        (work / "install.sh").write_text(fixture.INSTALL.format(
            kernel=env / "bzImage", initrd=env / "faketarget-initrd.cpio.zst"))
        dpw, hpw = fixture.DECOY_PW, fixture.HIDDEN_PW
        v1_salt = "fedcba9876543210fedcba9876543210"
        _, out = vm.ToolVM(env, work).run(f"""
        python3 /src/tests/make_v1.py /dev/vda {v1_salt} '{dpw}' - {env}/bzImage {env}/faketarget-initrd.cpio.zst 48
        printf '%s\n' '{dpw}' | python3 -m pd migrate-v1 --config pd.toml --v1-salt {v1_salt} \
            --v1-argon2 1,12,1 --password-stdin
        printf '%s\n%s\n' '{dpw}' '{hpw}' | python3 -m pd init-hidden --config pd.toml --password-stdin \
            && echo HIDDEN-CREATED || echo HIDDEN-REFUSED
        printf '%s\n' '{dpw}' | python3 -m pd compact --config pd.toml --password-stdin
        printf '%s\n' '{dpw}' | python3 -m pd open --config pd.toml --password-stdin \
            --run 'cd fake/fill && sha256sum -c --quiet /work/sums.txt && echo SUMS-OK'
        printf '%s\n%s\n' '{dpw}' '{hpw}' | python3 -m pd init-hidden --config pd.toml --password-stdin \
            --run 'sh /work/install.sh hidden' | tee hidden.txt
        """, disks=[disk], timeout=1800, log=work / "v1.log")
        self.assertIn("HIDDEN-REFUSED", out)
        before, after, boundary = high_water(out)
        self.assertGreater(before, boundary)
        self.assertLessEqual(after, boundary)
        self.assertIn("SUMS-OK", out)
        sed = re.search(r"(sed -i s/2048/\d+/ /early_env.sh)", (work / "hidden.txt").read_text()).group(1)
        self.boot_both(env, work, disk, sed)

    def boot_both(self, env, work, disk, sed):
        from pd import usb
        from pd.config import Config
        cfg = Config.load(work / "pd.toml")
        (work / "install.cpio").write_bytes(usb.install_cpio(cfg))
        fx = fixture.Fixture(env=env, work=work, disk=disk, cfg=cfg, sed=sed, setup_log="")
        b = fx.boot("decoy")
        self.addCleanup(b.logfile.close)
        b.expect("Password: ")
        b.sendline(fixture.DECOY_PW)
        b.expect("PD-TARGET: marker=decoy", timeout=120)
        b.close()
        b = fx.boot("hidden")
        self.addCleanup(b.logfile.close)
        b.expect("Password: ")
        b.sendline("")
        b.expect("recovery shell")
        b.sendline(sed)
        b.sendline("/early.sh")
        b.expect("Password: ")
        b.sendline(fixture.HIDDEN_PW)
        b.expect(r"Free space between the two sides: \d+ MiB", timeout=120)
        b.expect("PD-TARGET: marker=hidden", timeout=120)
        b.close()

    def test_pd_decoy(self):
        env = vm.testenv()
        work = vm.workdir()
        self.addCleanup(vm.cleanup, work)
        disk = vm.sparse_disk(work / "disk.img", 8 << 30)
        (work / "pd.toml").write_text(fixture.CONFIG.format(salt="0123456789abcdef0123456789abcdef"))
        (work / "install.sh").write_text(fixture.INSTALL.format(
            kernel=env / "bzImage", initrd=env / "faketarget-initrd.cpio.zst"))
        (work / "fill.sh").write_text(FILL.format(count=90) + 'mkdir -p $PD_ROOT/fake/home/t\nfill $PD_ROOT/fake/home/t\n')
        dpw, hpw = fixture.DECOY_PW, fixture.HIDDEN_PW
        _, out = vm.ToolVM(env, work).run(f"""
        printf '%s\\n' '{dpw}' | python3 -m pd init-decoy --config pd.toml --password-stdin \\
            --run 'sh /work/install.sh decoy && bash /work/fill.sh'
        printf '%s\\n%s\\n' '{dpw}' '{hpw}' | python3 -m pd init-hidden --config pd.toml --password-stdin \\
            && echo HIDDEN-CREATED || echo HIDDEN-REFUSED
        printf '%s\\n' '{dpw}' | python3 -m pd compact --config pd.toml --password-stdin
        printf '%s\\n' '{dpw}' | python3 -m pd compact --config pd.toml --password-stdin --recover
        printf '%s\\n' '{dpw}' | python3 -m pd open --config pd.toml --password-stdin \\
            --run 'cd fake/home/t && sha256sum -c --quiet /work/sums.txt && echo SUMS-OK'
        printf '%s\\n%s\\n' '{dpw}' '{hpw}' | python3 -m pd init-hidden --config pd.toml --password-stdin \\
            --run 'sh /work/install.sh hidden' | tee hidden.txt
        """, disks=[disk], timeout=1800, log=work / "compact.log")
        self.assertIn("HIDDEN-REFUSED", out)
        self.assertIn("Run `pd compact` on the decoy first", out)
        before, after, boundary = high_water(out)
        self.assertGreater(before, boundary)
        self.assertLessEqual(after, boundary)
        self.assertIn("nothing to recover", out)
        self.assertIn("SUMS-OK", out)

        # An interrupted metadata write: the spare holds the good metadata and
        # the pool's own copy is damaged.  --recover restores it.
        _, out = vm.ToolVM(env, work).run(f"""
        python3 - <<'PY'
from pd import setup, thinpool
from pd.config import Config
from pd.util import cmd
cfg = Config.load("pd.toml")
key = setup.boot_key(cfg, 1 << 20, b"{dpw}")
with setup.boot_volume(cfg.disk, 1 << 20, key, create=False) as mnt:
    data_key, table = (mnt / "data.key").read_bytes(), (mnt / "table.txt").read_text()
with setup.data_region(cfg.disk, table, data_key, readonly=False) as dev:
    pool = thinpool.find_pool(cfg.vg, str(dev))
    with thinpool.mapped(pool, writable=True) as devs:
        cmd("dd", f"if={{devs['tmeta']}}", f"of={{devs['spare']}}", "bs=4M", "conv=fsync", "status=none")
        with open(devs["tmeta"], "r+b") as f:
            f.write(bytes(4096))
        print("DAMAGED", thinpool.check(devs["tmeta"]), thinpool.check(devs["spare"]))
PY
        printf '%s\\n' '{dpw}' | python3 -m pd compact --config pd.toml --password-stdin || echo COMPACT-REFUSED
        printf '%s\\n' '{dpw}' | python3 -m pd compact --config pd.toml --password-stdin --recover
        printf '%s\\n' '{dpw}' | python3 -m pd open --config pd.toml --password-stdin \\
            --run 'cd fake/home/t && sha256sum -c --quiet /work/sums.txt && echo SUMS-OK'
        """, disks=[disk], timeout=600, log=work / "recover.log")
        self.assertIn("DAMAGED False True", out)
        self.assertIn("COMPACT-REFUSED", out)
        self.assertIn("pd compact --recover", out)
        self.assertIn("restored the pool's metadata from the spare", out)
        self.assertIn("SUMS-OK", out)
        sed = re.search(r"(sed -i s/2048/\d+/ /early_env.sh)", (work / "hidden.txt").read_text()).group(1)

        # Both sides boot.
        from pd import usb
        from pd.config import Config
        cfg = Config.load(work / "pd.toml")
        (work / "install.cpio").write_bytes(usb.install_cpio(cfg))
        fx = fixture.Fixture(env=env, work=work, disk=disk, cfg=cfg, sed=sed, setup_log=out)
        b = fx.boot("decoy")
        self.addCleanup(b.logfile.close)
        b.expect("Password: ")
        b.sendline(dpw)
        b.expect("PD-TARGET: marker=decoy", timeout=120)
        b.close()
        b = fx.boot("hidden")
        self.addCleanup(b.logfile.close)
        b.expect("Password: ")
        b.sendline("")
        b.expect("recovery shell")
        b.sendline(sed)
        b.sendline("/early.sh")
        b.expect("Password: ")
        b.sendline(hpw)
        b.expect(r"Free space between the two sides: \d+ MiB", timeout=120)
        b.expect("PD-TARGET: marker=hidden", timeout=120)
        b.close()

    def test_stock_decoy(self):
        env = vm.testenv()
        work = vm.workdir()
        self.addCleanup(vm.cleanup, work)
        disk = vm.sparse_disk(work / "disk.img", 8 << 30)
        (work / "install.sh").write_text(fixture.INSTALL.format(
            kernel=env / "bzImage", initrd=env / "faketarget-initrd.cpio.zst"))
        dpw, hpw = fixture.DECOY_PW, fixture.HIDDEN_PW
        _, out = vm.ToolVM(env, work).run(test_stock.STOCK_DECOY + FILL.format(count=80) + f"""
        printf %s '{dpw}' | cryptsetup open --key-file - /dev/vda2 cryptroot
        lvm vgchange -ay -qq vg0
        mount -o nodiscard /dev/vg0/root /mnt/decoy
        mkdir -p /mnt/decoy/t && fill /mnt/decoy/t
        umount /mnt/decoy
        lvm vgchange -an -qq vg0
        cryptsetup close cryptroot
        echo "UUID $(cryptsetup luksUUID /dev/vda2)"
        """, disks=[disk], timeout=900, log=work / "decoy.log")
        uuid = re.search(r"^UUID (\S+)", out, re.M).group(1)
        (work / "pd.toml").write_text(fixture.CONFIG.format(salt=uuid))
        _, out = vm.ToolVM(env, work).run(f"""
        printf '%s\\n%s\\n' '{dpw}' '{hpw}' | python3 -m pd init-hidden --config pd.toml \\
            --stock-decoy /dev/vda2 --password-stdin && echo HIDDEN-CREATED || echo HIDDEN-REFUSED
        printf '%s\\n' '{dpw}' | python3 -m pd compact --config pd.toml --stock-decoy /dev/vda2 --password-stdin
        printf %s '{dpw}' | cryptsetup open --readonly --key-file - /dev/vda2 cryptroot
        lvm vgchange -ay -qq vg0
        mkdir -p /mnt/decoy && mount -o ro,norecovery /dev/vg0/root /mnt/decoy
        (cd /mnt/decoy/t && sha256sum -c --quiet /work/sums.txt && echo SUMS-OK)
        umount /mnt/decoy
        lvm vgchange -an -qq vg0
        cryptsetup close cryptroot
        printf '%s\\n%s\\n' '{dpw}' '{hpw}' | python3 -m pd init-hidden --config pd.toml \\
            --stock-decoy /dev/vda2 --password-stdin --run 'sh /work/install.sh hidden' | tee hidden.txt
        """, disks=[disk], timeout=1800, log=work / "compact.log")
        self.assertIn("HIDDEN-REFUSED", out)
        self.assertIn("Run `pd compact --stock-decoy` first", out)
        before, after, boundary = high_water(out)
        self.assertGreater(before, boundary)
        self.assertLessEqual(after, boundary)
        self.assertIn("SUMS-OK", out)

        printed = (work / "hidden.txt").read_text()
        commands = [line[2:] for line in printed.split("Hidden boot from a live system", 1)[1]
                    .splitlines()[1:] if line.startswith("  ")]
        script = f"P='{hpw}'\n" + "\n".join(commands[1:]) + "\n"
        _, out = vm.ToolVM(env, work).run(script, disks=[disk], check=False, log=work / "boot.log")
        self.assertRegex(out, r"Free space between the two sides: \d+ MiB")
        self.assertIn("PD-TARGET: marker=hidden", out)


if __name__ == "__main__":
    unittest.main()
