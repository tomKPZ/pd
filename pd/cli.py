"""pd command-line interface."""

import argparse
import getpass
import os
import pathlib
import secrets
import subprocess
import sys

from pd import layout as L


def read_passwords(args, names: list[str]) -> list[bytes]:
    if args.password_stdin:
        lines = sys.stdin.buffer.read().split(b"\n")
        return lines[: len(names)]
    out = []
    for n in names:
        while True:
            a = getpass.getpass(f"{n} password: ")
            b = getpass.getpass(f"Confirm {n} password: ")
            if a == b:
                out.append(a.encode())
                break
            print("Passwords do not match.")
    return out


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
    p = argparse.ArgumentParser(prog="pd")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("salt", help="print a new random salt")

    r = sub.add_parser("randomize", help="fill the disk with random data")
    r.add_argument("--config", required=True)

    for side in ("decoy", "hidden"):
        s = sub.add_parser(f"init-{side}", help=f"create the {side} side")
        s.add_argument("--config", required=True)
        s.add_argument("--password-stdin", action="store_true",
                       help="read password(s) from stdin, one per line (tests)")
        s.add_argument("--run", nargs="?", const="",
                       help="run a command (default: a shell) with the roots mounted")

    o = sub.add_parser("open", help="open a side and run a command with its roots mounted")
    o.add_argument("--config", required=True)
    o.add_argument("--offset", type=int, default=None,
                   help="boot volume start sector (default: the decoy's)")
    o.add_argument("--rw", action="store_true", help="mount read-write")
    o.add_argument("--password-stdin", action="store_true")
    o.add_argument("--run", nargs="?", const="")

    k = sub.add_parser("rekey", help="change a side's password and/or the salt")
    k.add_argument("--config", required=True)
    k.add_argument("--offset", type=int, default=None,
                   help="boot volume start sector (default: the decoy's)")
    k.add_argument("--new-salt", default=None,
                   help="re-key under this salt (rekey both sides, then build a new USB)")
    k.add_argument("--password-stdin", action="store_true")

    m = sub.add_parser("migrate-v1", help="convert a v1 install to v2 in place")
    m.add_argument("--config", required=True, help="v2 config (new salt)")
    m.add_argument("--v1-salt", required=True)
    m.add_argument("--v1-hidden-offset", type=int, default=None,
                   help="start sector of the v1 hidden boot volume (omit if there is none)")
    m.add_argument("--v1-argon2", default=None,
                   help="v1 Argon2 parameters t,m_log2,p (default 14,18,8)")
    m.add_argument("--password-stdin", action="store_true")

    d = sub.choices["init-decoy"]
    d.add_argument("--force", action="store_true",
                   help="re-create an existing decoy (never re-randomizes the disk)")
    g = d.add_mutually_exclusive_group()
    g.add_argument("--no-hidden", action="store_true", help="with --force: there is no hidden side")
    g.add_argument("--hidden-offset", type=int, default=None,
                   help="with --force: the hidden boot volume's start sector (asks for its password)")

    u = sub.add_parser("usb", help="build the boot UKI")
    u.add_argument("--config", required=True)
    u.add_argument("--env", required=True, help="path to the built testenv/default output")
    u.add_argument("--output", required=True)
    u.add_argument("--cmdline", default=None)
    u.add_argument("--salt-from", default=None,
                   help="take the salt from an existing UKI instead of the config")

    c = sub.add_parser("install-cpio", help="write the per-install initrd cpio")
    c.add_argument("--config", required=True)
    c.add_argument("--env", default=None, help="the built testenv (for keymaps)")
    c.add_argument("--output", required=True)

    args = p.parse_args(argv)

    if args.command == "salt":
        print(secrets.token_hex(64))
        return 0

    from pd.config import Config
    cfg = Config.load(args.config)
    if getattr(args, "salt_from", None):
        from pd import usb
        cfg.salt = usb.read_salt(pathlib.Path(args.salt_from))
    cfg.check_salt()

    if args.command == "randomize":
        from pd import setup
        setup.randomize(cfg.disk)
    elif args.command in ("init-decoy", "init-hidden") and not cfg.systems:
        raise SystemExit("config needs at least one [[systems]] entry")
    elif args.command == "rekey":
        from pd import rekey
        if args.new_salt:
            Config(disk=cfg.disk, salt=args.new_salt).check_salt()
        old, new = read_passwords(args, ["Current", "New"])
        rekey.rekey(cfg, rekey.default_offset(cfg, args.offset), old, new, args.new_salt)
        print("Re-keyed.")
        if args.new_salt:
            print("Re-key the other side with the same --new-salt, put the new salt in the config,")
            print("and build a new boot USB.")
    elif args.command == "migrate-v1":
        from pd import migrate
        from pd.util import Argon2Params
        hidden = args.v1_hidden_offset is not None
        pws = read_passwords(args, ["Decoy", "Hidden"] if hidden else ["Decoy"])
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
            pw, hpw = read_passwords(args, ["Decoy", "Hidden"])
            hidden = (args.hidden_offset * L.SECTOR, hpw)
        else:
            (pw,) = read_passwords(args, ["Decoy"])
            hidden = "none" if args.no_hidden else None
        setup.init_decoy(cfg, pw, runner(args), force=args.force, hidden=hidden)
    elif args.command == "init-hidden":
        from pd import setup
        decoy, hidden = read_passwords(args, ["Decoy", "Hidden"])
        hl = setup.init_hidden(cfg, decoy, hidden, runner(args))
        # Printed once.  Never store it on the USB or in the decoy.
        print("Hidden boot: press Enter at the password prompt, then run:")
        print(f"  {L.hidden_boot_command(hl)}")
        print("  /early.sh")
    elif args.command == "open":
        from pd import setup
        (pw,) = read_passwords(args, ["Disk"])
        offset = L.DECOY_PLAIN_OFFSET if args.offset is None else args.offset * L.SECTOR
        with setup.open_side(cfg, pw, offset, readonly=not args.rw) as base:
            run = runner(args) or runner(argparse.Namespace(run=""))
            run(base)
    elif args.command == "usb":
        from pd import usb
        usb.build_uki(cfg, pathlib.Path(args.env), pathlib.Path(args.output),
                      args.cmdline or usb.BOOT_CMDLINE)
    elif args.command == "install-cpio":
        from pd import usb
        pathlib.Path(args.output).write_bytes(
            usb.install_cpio(cfg, pathlib.Path(args.env) if args.env else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())

