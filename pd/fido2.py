"""FIDO2 security keys as a second factor for a side's data (docs/design.md
§4.1).

The boot volume stays keyed by A = Argon2(password, salt), as without FIDO2.
With FIDO2 it holds fido2.txt (INNER) in place of data.key (and, on the
hidden side, decoy.key): one line per credential,

    CRED SLOT0 SLOT1        (hex)

SLOT0 the side's data key and SLOT1 the decoy's data key on the hidden side
(random bytes on the decoy), each XOR SHA-512(A || h || n), with
h = hmac-secret(CRED, SHA-256(A)) and n the slot's number as one byte.  Both
sides use the same security keys; nothing goes on the USB.

The token operations go through `fido2-hmac` (tools/fido2_hmac.c), the same
program late.sh runs from the boot volume.
"""

import dataclasses
import getpass
import hashlib
import subprocess

from pd.util import cmd

TOOL = "fido2-hmac"
SLOTS = 2
KEY_LEN = 64
INNER = "fido2.txt"


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


def pad(a: bytes, h: bytes, slot: int) -> bytes:
    return hashlib.sha512(a + h + bytes([slot])).digest()


def xor(x: bytes, y: bytes) -> bytes:
    return bytes(i ^ j for i, j in zip(x, y, strict=True))


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


# --- Slots ---------------------------------------------------------------------------

def wrap(a: bytes, dev: str, cred: bytes, secrets: list[bytes]) -> list[bytes]:
    """The slots wrapping `secrets` (data key, decoy key) for one security
    key: one touch."""
    h = get(dev, cred, hmac_salt(a))
    return [xor(x, pad(a, h, n)) for n, x in enumerate(secrets)]


def unwrap(text: str, a: bytes) -> list[bytes]:
    """The secrets in a boot volume's fido2.txt, with whichever enrolled
    security key is plugged in."""
    creds = parse(text)
    held = holders(creds)
    if not held:
        raise RuntimeError("this side uses FIDO2: plug in one of its security keys")
    dev, i = next(iter(held.items()))
    h = get(dev, creds[i].id, hmac_salt(a))
    return [xor(s, pad(a, h, n)) for n, s in enumerate(creds[i].slots)]


def volume_secrets(files: dict[str, bytes], a: bytes) -> tuple[bytes, bytes | None]:
    """(data key, decoy key or None) of a boot volume opened with key `a`,
    from its files (name -> content): plain, or unwrapped with FIDO2."""
    if "data.key" in files:
        return files["data.key"], files.get("decoy.key")
    if INNER not in files:
        raise RuntimeError("that boot volume has no data.key; not a pd boot volume?")
    data, decoy = unwrap(files[INNER].decode(), a)
    return data, decoy
