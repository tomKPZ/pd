"""pd command-line interface."""

import argparse
import getpass
import os
import pathlib
import secrets
import subprocess
import sys

from pd import layout as L


def stdin_line() -> bytes:
    """The next line of stdin (--password-stdin: passwords, then the
    security key PIN if one is needed, then answers to "next security key"
    prompts).  Read one line at a time, so a test can answer prompts."""
    return sys.stdin.buffer.readline().rstrip(b"\n")


def read_passwords(args, names: list[str], new: tuple[str, ...] = ()) -> list[bytes]:
    """Prompt for each password in `names` ("Decoy password", ...).  Only
    the ones in `new` are asked twice; the others are checked against the
    disk anyway."""
    if args.password_stdin:
        return [stdin_line() for _ in names]
    out = []
    for n in names:
        while True:
            a = getpass.getpass(f"{n}: ")
            if n not in new or a == getpass.getpass(f"Confirm {n.lower()}: "):
                out.append(a.encode())
                break
            print("Passwords do not match.")
    return out


def key_prompter(args):
    """next_key for enrolling and re-keying: ask for the next security key."""
    if args.password_stdin:
        def next_key_stdin() -> bool:
            print("Next security key? (next/done)", flush=True)
            return stdin_line().strip() == b"next"
        return next_key_stdin

    def next_key() -> bool:
        answer = input("Plug in the next security key and press Enter, or type \"done\": ")
        return answer.strip().lower() not in ("done", "d", "q")
    return next_key


def runner(args):
    if args.run is None:
        return None

    def run(base: pathlib.Path):
        env = dict(os.environ, PD_ROOT=str(base))
        cmd = args.run or os.environ.get("SHELL", "/bin/sh")
        print(f"Roots are mounted under {base}/<system>.  Exit when done.")
        subprocess.run(cmd, shell=True, check=True, env=env, cwd=base)
    return run


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pd", description="Plain Disguise: set up and maintain a pd disk")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("salt", help="print a new random salt (for a config's `salt`, e.g. with a stock decoy)")

    r = sub.add_parser("randomize", help="fill the disk with random data")
    r.add_argument("--config", required=True)

    for side in ("decoy", "hidden"):
        s = sub.add_parser(f"init-{side}", help=f"create the {side} side")
        s.add_argument("--config", required=True)
        s.add_argument("--password-stdin", action="store_true",
                       help="read password(s) from stdin, one per line (tests)")
        s.add_argument("--run", nargs="?", const="",
                       help="run a command (default: a shell) with the roots mounted")

    o = sub.add_parser("open", help="open a side and mount it (read-only unless --rw)")
    o.add_argument("--config", required=True)
    o.add_argument("--offset", type=int, default=None,
                   help="boot volume start sector (default: the decoy's)")
    o.add_argument("--rw", action="store_true", help="activate the VG and mount read-write")
    o.add_argument("--mountpoint", default="/mnt/pd", help="where to mount (default /mnt/pd)")
    o.add_argument("--password-stdin", action="store_true")
    o.add_argument("--run", nargs="?", const="",
                   help="run a command (default: a shell) with the side open, then close it")

    sub.add_parser("close", help="close what `pd open` opened")

    k = sub.add_parser("rekey", help="change a side's password and/or the salt")
    k.add_argument("--config", required=True)
    k.add_argument("--offset", type=int, default=None,
                   help="boot volume start sector (default: the decoy's)")
    k.add_argument("--new-salt", default=None,
                   help="with a `salt` in the config: re-key under this salt (rekey both sides, then "
                        "build a new USB).  The disk's own salt changes with `pd header`")
    k.add_argument("--new-argon2", default=None, metavar="T,M_LOG2,P",
                   help="re-key with these Argon2 parameters (rekey both sides, put them in the "
                        "config's [argon2], then build a new USB)")
    k.add_argument("--password-stdin", action="store_true")

    cp = sub.add_parser("compact", help="move a side's data to the front of its thin pool, in place")
    cp.add_argument("--config", required=True)
    cp.add_argument("--offset", type=int, default=None,
                    help="boot volume start sector (default: the decoy's)")
    cp.add_argument("--stock-decoy", metavar="DEVICE", default=None,
                    help="compact a stock decoy (docs/stock-boot.md) whose LUKS volume is on DEVICE")
    cp.add_argument("--decoy-header", default=None, help="with --stock-decoy: a detached LUKS header")
    cp.add_argument("--below", default=None, metavar="SIZE",
                    help="keep the data within the first SIZE of the pool (default: decoy: room for "
                         "a hidden side at its cap; hidden side: as tight as possible)")
    cp.add_argument("--no-trim", action="store_true",
                    help="don't fstrim the filesystems first (only blocks the pool knows are free move)")
    cp.add_argument("--recover", action="store_true",
                    help="after an interrupted compaction: restore the pool's metadata from the spare")
    cph = cp.add_mutually_exclusive_group()
    cph.add_argument("--no-hidden", action="store_true",
                     help="(decoy) there is no hidden side yet: refill all free space above the boundary")
    cph.add_argument("--hidden-offset", type=int, default=None,
                     help="(decoy) the hidden side's start sector (asks for its password): the refill "
                          "stops below it")
    cp.add_argument("--password-stdin", action="store_true")

    mv = sub.add_parser("move", help="move both sides to another disk (bigger or smaller)")
    mv.add_argument("--config", required=True, help="the config for the old disk")
    mv.add_argument("--to", required=True, metavar="DISK", help="the new disk (erased)")
    mvh = mv.add_mutually_exclusive_group(required=True)
    mvh.add_argument("--no-hidden", action="store_true", help="there is no hidden side")
    mvh.add_argument("--hidden-offset", type=int, default=None,
                     help="the hidden boot volume's start sector (asks for its password)")
    mv.add_argument("--no-randomize", action="store_true",
                    help="skip randomizing the new disk (only if it's already randomized)")
    mv.add_argument("--password-stdin", action="store_true")

    f = sub.add_parser("fido2", help="enroll or remove FIDO2 security keys")
    fsub = f.add_subparsers(dest="fido2_command", required=True)
    fe = fsub.add_parser("enroll", help="enroll every plugged-in security key for one side")
    fe.add_argument("--offset", type=int, default=None,
                    help="boot volume start sector (default: the decoy's)")
    fe.add_argument("--password-stdin", action="store_true")
    fr = fsub.add_parser("remove", help="remove security keys (by number, from `pd fido2 list`)")
    fr.add_argument("index", type=int, nargs="+")
    fl = fsub.add_parser("list", help="list the enrolled security keys")
    for x in (fe, fr, fl):
        x.add_argument("--config", required=True)
    for x in (fr, fl):
        # The keys' slots are in each side's boot volume.
        x.add_argument("--offset", type=int, default=None,
                       help="boot volume start sector (default: the decoy's)")
        x.add_argument("--password-stdin", action="store_true")

    m = sub.add_parser("migrate-v1", help="convert a v1 install to v2 in place")
    m.add_argument("--config", required=True, help="v2 config (new salt)")
    m.add_argument("--v1-salt", required=True)
    m.add_argument("--v1-hidden-offset", type=int, default=None,
                   help="start sector of the v1 hidden boot volume (omit if there is none)")
    m.add_argument("--v1-argon2", default=None,
                   help="v1 Argon2 parameters t,m_log2,p (default 14,18,8)")
    m.add_argument("--password-stdin", action="store_true")

    h = sub.choices["init-hidden"]
    h.add_argument("--stock-decoy", metavar="DEVICE", default=None,
                   help="the decoy is a stock LUKS + LVM thin install whose LUKS volume is on DEVICE "
                        "(docs/stock-boot.md); its password is asked for as the decoy password")
    h.add_argument("--decoy-header", default=None,
                   help="with --stock-decoy: the decoy's detached LUKS header")

    d = sub.choices["init-decoy"]
    d.add_argument("--force", action="store_true",
                   help="re-create an existing decoy (never re-randomizes the disk)")
    g = d.add_mutually_exclusive_group()
    g.add_argument("--no-hidden", action="store_true", help="with --force: there is no hidden side")
    g.add_argument("--hidden-offset", type=int, default=None,
                   help="with --force: the hidden boot volume's start sector (asks for its password)")

    u = sub.add_parser("usb", help="put the boot image (and per-install files) on a USB stick's ESP")
    u.add_argument("--output", required=True, metavar="ESP",
                   help="the mounted EFI system partition of the boot stick")
    u.add_argument("--config", default=None,
                   help="add this install's files (keymap, other Argon2 parameters, a salt)")
    u.add_argument("--env", default=os.environ.get("PD_BOOT_ENV"),
                   help="directory with BOOTX64.EFI and keymaps/ (the testenv, or the setup "
                        "environment's; default: $PD_BOOT_ENV)")
    u.add_argument("--image", default=None, help="the boot image (default: ENV/BOOTX64.EFI)")

    hd = sub.add_parser("header", help="write a new random header (the disk's salt) and re-key both "
                                       "sides to it; also moves an install's salt from the USB to the "
                                       "disk")
    hd.add_argument("--config", required=True)
    hdh = hd.add_mutually_exclusive_group(required=True)
    hdh.add_argument("--no-hidden", action="store_true", help="there is no hidden side")
    hdh.add_argument("--hidden-offset", type=int, default=None,
                     help="the hidden boot volume's start sector (asks for its password)")
    hds = hd.add_mutually_exclusive_group()
    hds.add_argument("--old-salt", default=None, help="the current salt, if not in the config")
    hds.add_argument("--old-salt-from", default=None, metavar="UKI",
                     help="read the current salt from an old boot image")
    hd.add_argument("--password-stdin", action="store_true")

    c = sub.add_parser("install-cpio", help="write the per-install initrd cpio")
    c.add_argument("--config", required=True)
    c.add_argument("--env", default=None, help="the built testenv (for keymaps)")
    c.add_argument("--output", required=True)

    args = p.parse_args(argv)

    if args.command == "salt":
        print(secrets.token_hex(64))
        return 0
    if args.command == "close":
        from pd import opener
        opener.close()
        print("Closed.")
        return 0

    from pd import fido2
    from pd.config import Config
    if args.command == "usb":
        from pd import usb
        if not (args.image or args.env):
            raise SystemExit("pass --env or --image (the boot image)")
        image = pathlib.Path(args.image) if args.image else pathlib.Path(args.env) / usb.IMAGE
        cfg = Config.load(args.config) if args.config else None
        keymaps = pathlib.Path(args.env) / "keymaps" if args.env else None
        for f in usb.write_usb(cfg, image, pathlib.Path(args.output), keymaps):
            print(f"wrote {f}")
        return 0
    cfg = Config.load(args.config)
    if getattr(args, "password_stdin", False):
        # One PIN line, after the passwords, for every security key.
        pin: list[str] = []

        def pin_from_stdin(dev: str) -> str:
            if not pin:
                pin.append(stdin_line().decode())
            return pin[0]
        fido2.pin_source = pin_from_stdin

    if args.command == "randomize":
        from pd import setup
        setup.randomize(cfg.disk)
    elif args.command in ("init-decoy", "init-hidden") and not cfg.systems:
        raise SystemExit("config needs at least one [[systems]] entry")
    elif args.command == "rekey":
        from pd import rekey
        if args.new_salt:
            if not cfg.salt:
                raise SystemExit("this install uses the disk's salt: `pd header` gives it a new one")
            Config(disk=cfg.disk, salt=args.new_salt).check_salt()
        new_argon2 = None
        if args.new_argon2:
            from pd.util import Argon2Params
            try:
                t_, m_, p_ = (int(x) for x in args.new_argon2.split(","))
            except ValueError:
                raise SystemExit("--new-argon2 takes T,M_LOG2,P, e.g. 1,20,4")
            new_argon2 = Argon2Params(t=t_, m_log2=m_, p=p_, length=cfg.argon2.length)
        old, new = read_passwords(args, ["Current password", "New password"], new=("New password",))
        for w in rekey.rekey(cfg, rekey.default_offset(cfg, args.offset), old, new, args.new_salt,
                             key_prompter(args), new_argon2):
            print(f"Warning: {w}")
        print("Re-keyed.")
        if args.new_salt or new_argon2:
            changed = " and ".join(x for x, v in (("--new-salt", args.new_salt),
                                                  ("--new-argon2", new_argon2)) if v)
            print(f"Re-key the other side with the same {changed}, update the config to match,")
            print("and build a new boot USB.")
    elif args.command == "move":
        from pd import move
        if args.hidden_offset is not None:
            dpw, hpw = read_passwords(args, ["Decoy password", "Hidden password"])
            hidden = (args.hidden_offset * L.SECTOR, hpw)
        else:
            (dpw,) = read_passwords(args, ["Decoy password"])
            hidden = None
        hl = move.move(cfg, args.to, dpw, hidden, randomize=not args.no_randomize)
        print(f"Moved.  Set disk = \"{args.to}\" in the config.")
        if hl:
            # Printed once.  Never store it on the USB or in the decoy.
            print("The hidden side has a new offset.  Hidden boot: press Enter at the password "
                  "prompt, then run:")
            print(f"  {L.hidden_boot_command(hl)}")
            print("  /early.sh")
        print("The boot USB still works" + (" (same salt)." if cfg.salt else
                                            " (the new disk has its own salt in its first MiB).")
              + "  Wipe the old disk (pd randomize) once the new one boots: two disks with the same "
              "sides are two snapshots.")
    elif args.command == "compact":
        from pd import compact
        from pd.config import parse_size
        below = parse_size(args.below) if args.below else None
        decoy = bool(args.stock_decoy) or args.offset is None
        if args.recover:
            (pw,) = read_passwords(args, ["Decoy password" if decoy else "Password"])
            print(compact.recover_side(cfg, args, pw))
            return 0
        if decoy and not (args.no_hidden or args.hidden_offset is not None):
            raise SystemExit("compacting the decoy needs --no-hidden or --hidden-offset N")
        if decoy and args.hidden_offset is not None:
            pw, hpw = read_passwords(args, ["Decoy password", "Hidden password"])
            hidden = (args.hidden_offset * L.SECTOR, hpw)
        else:
            (pw,) = read_passwords(args, ["Decoy password" if decoy else "Password"])
            hidden = "none"
        if args.stock_decoy:
            limit = (None if hidden == "none"
                     else compact.hidden_low(cfg, hidden[0], hidden[1]) - L.REVERSE_CHUNK)
            before, after, n = compact.compact_stock(cfg, args.stock_decoy, args.decoy_header, pw,
                                                     below=below, do_trim=not args.no_trim,
                                                     hidden_limit=limit)
        else:
            from pd import rekey
            before, after, n = compact.compact(cfg, rekey.default_offset(cfg, args.offset), pw,
                                               below=below, do_trim=not args.no_trim,
                                               hidden=hidden if decoy else None)
        mib = lambda blocks: blocks * before.block_size // L.MIB  # noqa: E731
        print(f"Data: {mib(before.mapped)} MiB.  Highest block: {mib(before.high_water)} MiB into the "
              f"pool before, {mib(after.high_water)} MiB after (boundary {mib(n)} MiB).")
    elif args.command == "fido2":
        from pd import rekey
        if args.fido2_command == "enroll":
            (pw,) = read_passwords(args, ["Password"])
            n, created = rekey.enroll(cfg, rekey.default_offset(cfg, args.offset), pw,
                                      key_prompter(args))
            print(f"Enrolled {n} security key(s) for this side; its data now opens only with one "
                  "of them.")
            if created:
                print(f"{created} new key(s): enroll them for the other side too, if there is one.")
        else:
            (pw,) = read_passwords(args, ["Password"])
            offset = rekey.default_offset(cfg, args.offset)
            if args.fido2_command == "remove":
                rekey.remove(cfg, offset, pw, args.index)
                print("Removed.")
            else:
                print(rekey.status(cfg, offset, pw))
    elif args.command == "migrate-v1":
        from pd import migrate
        from pd.util import Argon2Params
        hidden = args.v1_hidden_offset is not None
        pws = read_passwords(args, ["Decoy password", "Hidden password"] if hidden else ["Decoy password"])
        params = migrate.V1_ARGON2
        if args.v1_argon2:
            t_, m_, p_ = (int(x) for x in args.v1_argon2.split(","))
            params = Argon2Params(t=t_, m_log2=m_, p=p_, length=512)
        migrate.migrate(cfg, args.v1_salt, pws[0], pws[1] if hidden else None,
                        args.v1_hidden_offset * L.SECTOR if hidden else None, params)
        print("Migrated.  Install the v2 target hook in every OS, then boot with a v2 USB.")
    if args.command == "init-decoy":
        from pd import setup
        if args.hidden_offset is not None:
            pw, hpw = read_passwords(args, ["Decoy password", "Hidden password"], new=("Decoy password",))
            hidden = (args.hidden_offset * L.SECTOR, hpw)
        else:
            (pw,) = read_passwords(args, ["Decoy password"], new=("Decoy password",))
            hidden = "none" if args.no_hidden else None
        setup.init_decoy(cfg, pw, runner(args), force=args.force, hidden=hidden)
    elif args.command == "init-hidden" and args.stock_decoy:
        from pd import setup
        decoy, hidden = read_passwords(args, ["Decoy password", "Hidden password"], new=("Hidden password",))
        hl = setup.init_hidden_stock(cfg, args.stock_decoy, args.decoy_header, decoy, hidden,
                                     runner(args))
        if cfg.salt == setup.luks_uuid(args.stock_decoy, args.decoy_header):
            salt = (f'"$(cryptsetup luksUUID {args.decoy_header or args.stock_decoy})"')
            note = "The salt is the decoy's LUKS UUID, read back at boot."
        else:
            salt = cfg.salt
            note = "The salt isn't the decoy's LUKS UUID, so it's typed at boot."
        # Printed once.  Never store it anywhere near the machine.
        print(f"Hidden boot from a live system, as root (docs/stock-boot.md). {note}")
        for line in setup.stock_boot_commands(cfg, hl, salt):
            print(f"  {line}")
    elif args.command == "init-hidden":
        from pd import setup
        if args.decoy_header:
            raise SystemExit("--decoy-header needs --stock-decoy")
        decoy, hidden = read_passwords(args, ["Decoy password", "Hidden password"], new=("Hidden password",))
        hl = setup.init_hidden(cfg, decoy, hidden, runner(args))
        # Printed once.  Never store it on the USB or in the decoy.
        print("Hidden boot: press Enter at the password prompt, then run:")
        print(f"  {L.hidden_boot_command(hl)}")
        print("  /early.sh")
    elif args.command == "open":
        from pd import opener
        (pw,) = read_passwords(args, ["Password"])
        offset = L.DECOY_PLAIN_OFFSET if args.offset is None else args.offset * L.SECTOR
        mnt = pathlib.Path(args.mountpoint)
        session = opener.open_side(cfg, pw, offset, rw=args.rw, mnt=mnt)
        if args.run is None:
            print(f"Opened {'read-write' if args.rw else 'read-only'} at {mnt}.  `pd close` when done.")
        else:
            try:
                runner(args)(mnt)
            finally:
                session.close()
    elif args.command == "header":
        from pd import header
        old_salt = args.old_salt
        if args.old_salt_from:
            from pd import usb
            old_salt = usb.read_salt(pathlib.Path(args.old_salt_from))
        if old_salt:
            Config(disk=cfg.disk, salt=old_salt).check_salt()
        if args.hidden_offset is not None:
            dpw, hpw = read_passwords(args, ["Decoy password", "Hidden password"])
            sides = [header.Side(L.DECOY_PLAIN_OFFSET, dpw), header.Side(args.hidden_offset * L.SECTOR, hpw)]
        else:
            (dpw,) = read_passwords(args, ["Decoy password"])
            sides = [header.Side(L.DECOY_PLAIN_OFFSET, dpw)]
        header.change(cfg, args.config, sides, old_salt=old_salt, next_key=key_prompter(args))
        print("Done: both sides are keyed under the salt in the disk's first MiB.")
        if cfg.salt:
            print("Remove `salt` from the config, and rewrite the boot USB (`pd usb`): it no longer "
                  "needs one.")
    elif args.command == "install-cpio":
        from pd import usb
        pathlib.Path(args.output).write_bytes(
            usb.install_cpio(cfg, pathlib.Path(args.env) if args.env else None))
    return 0


def run(argv=None) -> int:
    """Entry point: errors pd anticipates are printed without a traceback."""
    try:
        return main(argv)
    except RuntimeError as e:
        print(f"pd: {e}", file=sys.stderr)
        return 1


def entry():
    """Console entry point (also the zipapp's main)."""
    sys.exit(run())


if __name__ == "__main__":
    entry()

