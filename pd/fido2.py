"""FIDO2 security keys as a second factor (docs/design.md §4.1), in one of
two modes (the config's `fido2_mode`):

* "data" (the default): the boot volume stays keyed by A = Argon2(password,
  salt), and the security key protects the side's data-region key instead.
  The boot volume holds fido2.txt (INNER) in place of data.key: one line per
  credential, "CRED SLOT0 SLOT1", SLOT0 the data key and SLOT1 (on the hidden
  side) the decoy's data key, each XOR SHA-512(A || h || n) with
  h = hmac-secret(CRED, SHA-256(A)) and n the slot's number as one byte.
  Nothing goes on the USB, but a password can be checked offline.
* "boot": the boot volume's key K is random, and each enrolled security key
  holds a wrapped copy of it on the USB (fido2.cred, from the config's
  `fido2` file): one line per credential,

    CRED SLOT0 SLOT1        (hex)

where SLOTn = K_n XOR SHA-512(A_n || hmac-secret(CRED, SHA-256(A_n))), slot 0
for the side whose boot volume is at the decoy's offset and slot 1 for the
other.  A slot nobody uses holds random bytes, so the file looks the same
whether or not a second side exists.  Credentials aren't per side: both
sides use the same security keys.

The token operations go through `fido2-hmac` (tools/fido2_hmac.c), the same
program the boot USB uses.
"""

import dataclasses
import getpass
import hashlib
import os
import pathlib
import subprocess

from pd import layout as L
from pd.util import cmd

TOOL = "fido2-hmac"
SLOTS = 2
KEY_LEN = 64


@dataclasses.dataclass
class Credential:
    id: bytes
    slots: list[bytes]


def parse(text: str) -> list[Credential]:
    creds = []
    for line in text.splitlines():
        f = line.split()
        if not f or f[0].startswith("#"):
            continue
        if len(f) != 1 + SLOTS:
            raise RuntimeError("bad fido2.txt line")
        slots = [bytes.fromhex(x) for x in f[1:]]
        if any(len(s) != KEY_LEN for s in slots):
            raise RuntimeError("bad fido2.txt slot")
        creds.append(Credential(bytes.fromhex(f[0]), slots))
    return creds


def dump(creds: list[Credential]) -> str:
    return "".join(f"{c.id.hex()} " + " ".join(s.hex() for s in c.slots) + "\n" for c in creds)


def hmac_salt(a: bytes) -> bytes:
    return hashlib.sha256(a).digest()


def wrapping_key(a: bytes, h: bytes) -> bytes:
    return hashlib.sha512(a + h).digest()


def xor(x: bytes, y: bytes) -> bytes:
    return bytes(i ^ j for i, j in zip(x, y, strict=True))


def side_slot(offset: int) -> int:
    return 0 if offset == L.DECOY_PLAIN_OFFSET else 1


# --- Tokens --------------------------------------------------------------------------

def _prompt_pin(dev: str) -> str:
    return getpass.getpass(f"Security key PIN ({dev}): ")


pin_source = _prompt_pin  # set by the CLI (e.g. to read stdin)
_pins: dict[str, str] = {}


def forget_pins() -> None:
    """After swapping security keys: the next one has its own PIN."""
    _pins.clear()


def _pin(dev: str) -> bytes:
    if dev not in _pins:
        _pins[dev] = pin_source(dev)
    return _pins[dev].encode() + b"\n"


def _run(*args, pin_dev: str | None = None) -> str:
    try:
        return cmd(TOOL, *args, input=_pin(pin_dev) if pin_dev else None,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()
    except FileNotFoundError:
        raise RuntimeError(f"{TOOL} not found (it's built with pd; see docs/install.md)")
    except subprocess.CalledProcessError as e:
        if pin_dev and e.returncode == 3:
            _pins.pop(pin_dev, None)
        msg = e.stderr.decode().strip().splitlines()
        raise RuntimeError(msg[-1] if msg else f"{TOOL} {args[0]} failed")


def devices() -> list[str]:
    return _run("list").split()


def make(dev: str) -> bytes:
    return bytes.fromhex(_run("make", dev, pin_dev=dev))


def has(dev: str, cred: bytes) -> bool:
    try:
        cmd(TOOL, "has", dev, cred.hex(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except subprocess.CalledProcessError:
        return False


def get(dev: str, cred: bytes, salt: bytes) -> bytes:
    return bytes.fromhex(_run("get", dev, cred.hex(), salt.hex(), pin_dev=dev))


def holders(creds: list[Credential]) -> dict[str, int]:
    """Present security keys -> index of the credential each holds (keys
    holding none are left out)."""
    out = {}
    for dev in devices():
        for i, c in enumerate(creds):
            if has(dev, c.id):
                out[dev] = i
                break
    return out


# --- Data mode: fido2.txt inside the boot volume ---------------------------------------

INNER = "fido2.txt"


def inner_pad(a: bytes, h: bytes, slot: int) -> bytes:
    return hashlib.sha512(a + h + bytes([slot])).digest()


def inner_slots(a: bytes, dev: str, cred: bytes, secrets: list[bytes]) -> list[bytes]:
    """The slots wrapping `secrets` (data key, decoy key) for one security
    key: one touch."""
    h = get(dev, cred, hmac_salt(a))
    return [xor(x, inner_pad(a, h, n)) for n, x in enumerate(secrets)]


def inner_unwrap(text: str, a: bytes) -> list[bytes]:
    """The secrets in a boot volume's fido2.txt, with whichever enrolled
    security key is plugged in."""
    creds = parse(text)
    held = holders(creds)
    if not held:
        raise RuntimeError("this side uses FIDO2: plug in one of its security keys")
    dev, i = next(iter(held.items()))
    h = get(dev, creds[i].id, hmac_salt(a))
    return [xor(s, inner_pad(a, h, n)) for n, s in enumerate(creds[i].slots)]


def volume_secrets(files: dict[str, bytes], a: bytes) -> tuple[bytes, bytes | None]:
    """(data key, decoy key or None) of a boot volume opened with key `a`,
    from its files (name -> content): plain, or unwrapped in data mode."""
    if "data.key" in files:
        return files["data.key"], files.get("decoy.key")
    if INNER not in files:
        raise RuntimeError("that boot volume has no data.key; not a pd boot volume?")
    data, decoy = inner_unwrap(files[INNER].decode(), a)
    return data, decoy


# --- Boot mode: fido2.txt next to the config, fido2.cred on the USB -------------------

def load(cfg) -> list[Credential]:
    if cfg.fido2_text is not None:
        return parse(cfg.fido2_text)
    if cfg.fido2 and pathlib.Path(cfg.fido2).exists():
        return parse(pathlib.Path(cfg.fido2).read_text())
    return []


def save(cfg, creds: list[Credential]) -> None:
    if not cfg.fido2:
        raise RuntimeError("set `fido2 = \"fido2.txt\"` in the config")
    path = pathlib.Path(cfg.fido2)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dump(creds))
    os.replace(tmp, path)
    cfg.fido2_text = None


# --- Keys ----------------------------------------------------------------------------

def candidate_keys(cfg, a: bytes) -> list[tuple[bytes, int | None]]:
    """The keys a password may open a boot volume with, as (key, slot):
    A itself (slot None: a side without FIDO2), and, if a present security
    key holds an enrolled credential, both of its unwrapped slots."""
    keys: list[tuple[bytes, int | None]] = [(a, None)]
    creds = load(cfg)
    if not creds:
        return keys
    held = holders(creds)
    if not held:
        return keys
    dev, i = next(iter(held.items()))
    w = wrapping_key(a, get(dev, creds[i].id, hmac_salt(a)))
    keys += [(xor(s, w), n) for n, s in enumerate(creds[i].slots)]
    return keys


def wrap_for(a: bytes, key: bytes, dev: str, cred: bytes) -> bytes:
    """This side's slot for one security key."""
    return xor(key, wrapping_key(a, get(dev, cred, hmac_salt(a))))

