"""Per-install configuration (TOML, kept outside the repo)."""

import dataclasses
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
class Config:
    disk: str
    salt: str
    vg: str = "sysvg"
    hostname: str | None = None  # set during setup, since LVM records it
    swap_size: int = 64 << 20
    hidden_cap: float = 0.4  # fraction of the disk the hidden thin LVs may use
    memorable_offset: bool = False
    min_gap_percent: int = 10  # frontier monitor warns below this
    argon2: Argon2Params = dataclasses.field(default_factory=Argon2Params)
    systems: list[System] = dataclasses.field(default_factory=list)
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

    @staticmethod
    def load(path) -> "Config":
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        a = raw.pop("argon2", {})
        systems = [System(**s) for s in raw.pop("systems", [])]
        if "swap_size" in raw:
            raw["swap_size"] = parse_size(raw["swap_size"])
        subvols = raw.pop("subvolumes", None)
        cfg = Config(argon2=Argon2Params(**a), systems=systems, **raw)
        if subvols is not None:
            cfg.subvolumes = subvols
        if cfg.default is None and cfg.systems:
            cfg.default = cfg.systems[0].name
        if not re.fullmatch(r"[0-9a-fA-F]{16,}", cfg.salt):
            raise ValueError("salt must be hex (use `pd salt` to generate one)")
        return cfg

