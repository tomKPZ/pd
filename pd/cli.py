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

    u = sub.add_parser("usb", help="build the boot UKI")
    u.add_argument("--config", required=True)
    u.add_argument("--env", required=True, help="path to the built testenv/default output")
    u.add_argument("--output", required=True)
    u.add_argument("--cmdline", default=None)

    c = sub.add_parser("install-cpio", help="write the per-install initrd cpio")
    c.add_argument("--config", required=True)
    c.add_argument("--output", required=True)

    args = p.parse_args(argv)

    if args.command == "salt":
        print(secrets.token_hex(64))
        return 0

    from pd.config import Config
    cfg = Config.load(args.config)

    if args.command == "randomize":
        from pd import setup
        setup.randomize(cfg.disk)
    elif args.command == "init-decoy":
        from pd import setup
        (pw,) = read_passwords(args, ["Decoy"])
        setup.init_decoy(cfg, pw, runner(args))
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
        pathlib.Path(args.output).write_bytes(usb.install_cpio(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())

