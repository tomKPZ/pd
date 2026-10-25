"""Per-install configuration (TOML, kept outside the repo)."""

import dataclasses
import pathlib
import re
import tomllib

from pd.util import Argon2Params

CIPHER_ARGS = ["--cipher=aes-xts-plain64", "--key-size=512", "--sector-size=4096"]


def parse_size(s: str | int) -> int:
    if isinstance(s, int):
        return s
    m = re.fullmatch(r"(\d+)\s*([KMGT]?)i?B?", s.strip(), re.I)
    if not m:
        raise ValueError(f"bad size: {s}")
    n, unit = int(m.group(1)), m.group(2).upper()
    return n * {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}[unit]


@dataclasses.dataclass
class System:
    name: str
    lv: str
    kernel: str
    initrd: str
    cmdline: str
    extra: str | None = None  # name of a plain/systems/<extra>/extra.sh hook


@dataclasses.dataclass
class Volume:
    """An extra thin LV, e.g. for bulk data (like v1's `data` LV)."""
    name: str
    fs: str = "xfs"  # "xfs" or "btrfs"

    def __post_init__(self):
        if self.fs not in ("xfs", "btrfs"):
            raise ValueError(f"volume {self.name}: fs must be xfs or btrfs")


@dataclasses.dataclass
class Config:
    disk: str
    salt: str = ""
    vg: str = "sysvg"
    hostname: str | None = None  # set during setup, since LVM records it
    keymap: str | None = None  # console keymap for the password prompt (kbd name, e.g. "de")
    # FIDO2 slots file (pd/fido2.py), relative to the config file.  Written by
    # `pd fido2 enroll`, put on the USB by `pd usb`.
    fido2: str | None = None
    swap_size: int = 64 << 20
    hidden_cap: float = 0.4  # fraction of the disk the hidden thin LVs may use
    memorable_offset: bool = True
    min_gap_percent: int = 10  # frontier monitor warns below this
    argon2: Argon2Params = dataclasses.field(default_factory=Argon2Params)
    systems: list[System] = dataclasses.field(default_factory=list)
    volumes: list[Volume] = dataclasses.field(default_factory=list)
    default: str | None = None
    subvolumes: dict[str, str] = dataclasses.field(default_factory=lambda: {
        "@": "",
        "@boot": "boot",
        "@home": "home",
        "@home_snapshots": "home/.snapshots",
        "@nix": "nix",
        "@snapshots": ".snapshots",
        "@var_cache": "var/cache",
        "@var_lib_docker": "var/lib/docker",
        "@var_log": "var/log",
        "@var_tmp": "var/tmp",
    })
    # fido2.txt contents taken from an existing UKI (`--salt-from`), if any.
    fido2_text: str | None = dataclasses.field(default=None, repr=False)

    @staticmethod
    def load(path) -> "Config":
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        a = raw.pop("argon2", {})
        systems = [System(**s) for s in raw.pop("systems", [])]
        volumes = [Volume(**v) for v in raw.pop("volumes", [])]
        if "swap_size" in raw:
            raw["swap_size"] = parse_size(raw["swap_size"])
        subvols = raw.pop("subvolumes", None)
        if raw.get("fido2"):
            raw["fido2"] = str(pathlib.Path(path).parent / raw["fido2"])
        cfg = Config(argon2=Argon2Params(**a), systems=systems, volumes=volumes, **raw)
        if subvols is not None:
            cfg.subvolumes = subvols
        if cfg.default is None and cfg.systems:
            cfg.default = cfg.systems[0].name
        if cfg.salt:
            cfg.check_salt()
        return cfg

    def check_salt(self) -> None:
        # Hex from `pd salt`, or for stock boot (docs/stock-boot.md) something
        # read back or typed at boot, such as the decoy's LUKS UUID.  Shell-safe
        # characters only, since it's typed on a command line.
        if not re.fullmatch(r"[0-9A-Za-z._-]{8,}", self.salt):
            raise ValueError("salt must be at least 8 letters, digits, '.', '_' or '-' "
                             "(use `pd salt` to generate one)")

