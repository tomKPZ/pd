#!/bin/sh
# Boot a pd disk from a live system, without pd's USB (docs/booting.md, "From
# a live system").  Run as root from the extracted pd-live bundle:
#
#   ./boot.sh [--offset SECTOR] [--disk DEVICE] [--salt SALT] [--argon2 T,M_LOG2,P] [--fido2 FILE]
#
# It runs the boot USB's own early.sh with the bundle's static tools: asks
# for the password, finds the disk, and hands over to late.sh in that disk's
# boot volume, which kexecs the chosen system.  --offset is the hidden side's
# start sector (default: the decoy's).  --salt, --argon2 and --fido2 stand in
# for the files that would be next to the boot image (salt.cred, argon2.cred,
# fido2.cred).

set -eu

here=$(cd "$(dirname "$0")" && pwd)
offset='' disk='' salt='' argon2='' fido2=''
while [ $# -gt 0 ]; do
    case $1 in
    --offset) offset=$2; shift 2 ;;
    --disk) disk=$2; shift 2 ;;
    --salt) salt=$2; shift 2 ;;
    --argon2) argon2=$2; shift 2 ;;
    --fido2) fido2=$2; shift 2 ;;
    *) echo "usage: $0 [--offset SECTOR] [--disk DEVICE] [--salt SALT] [--argon2 T,M_LOG2,P] [--fido2 FILE]" >&2
       exit 2 ;;
    esac
done
if [ "$(id -u)" != 0 ]; then
    echo "Run as root (sudo -i)." >&2
    exit 1
fi
case $offset in *[!0-9]*) echo "--offset takes a sector number" >&2; exit 2 ;; esac

# The bundle's tools come first: the same busybox, cryptsetup and argon2 as
# on pd's USB, whatever the live system has.
export PATH="$here/bin:$PATH"
d=$(mktemp -d /run/pd-live.XXXXXX)
trap 'rm -rf "$d"' EXIT
mkdir -p "$d/.extra/credentials"
sed "s/^PLAIN_OFFSET=2048\$/PLAIN_OFFSET=${offset:-2048}/" "$here/early_env.sh" >"$d/early_env.sh"
cp "$here/disk_class.sh" "$d/"
c=$d/.extra/credentials
[ -z "$salt" ] || printf %s "$salt" >"$c/salt.cred"
if [ -n "$argon2" ]; then
    IFS=, read -r t m p <<EOF
$argon2
EOF
    printf -- '-id -t %s -m %s -p %s -l 64 -v 13 -r' "$t" "$m" "$p" >"$c/argon2.cred"
fi
[ -z "$fido2" ] || cp "$fido2" "$c/fido2.cred"
if [ -n "$disk" ]; then
    export PD_DISK="$disk"
fi
PD_BOOT_DIR=$d "$here/bin/busybox" sh "$here/early.sh"
