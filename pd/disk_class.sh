# How a disk looks at its edges (docs/design.md §3, §7.2).  Sourced by the
# boot environment's early.sh (busybox) and run by setup (pd/header.py, with
# util-linux), so a disk whose edges setup accepts is one the boot tries
# first.  POSIX sh; no bashisms.

PD_SALT_OFFSET=1048544      # 1 MiB - 32 (pd/layout.py SALT_OFFSET)
PD_MIN_DISK=2147483648      # 2 GiB, setup's minimum disk size

# The salt: the 32 bytes just below 1 MiB, as 64 hex characters.
disk_salt() {
    od -An -v -tx1 -j "$PD_SALT_OFFSET" -N 32 "$1" 2>/dev/null | tr -d ' \n'
}

# True if the salt is missing or its bytes are all the same value (zeros on
# a new or trimmed SSD, 0xff on erased flash): 32 random bytes never are.
salt_blank() {
    [ ${#1} -eq 64 ] || return 0
    echo "$1" | awk '{ for (i = 3; i < 64; i += 2) if (substr($0, i, 2) != substr($0, 1, 2)) exit 1 }'
}

# True if the start of $1 looks like a partition table or a filesystem: the
# kernel found partitions, there's an MBR boot signature or a GPT header, or
# blkid recognizes something.  Works on regular files too (for tests).
looks_used() {
    _n=${1##*/}
    for _p in /sys/class/block/"$_n"/"$_n"*; do
        [ -e "$_p/partition" ] && return 0
    done
    [ "$(od -An -tx1 -j 510 -N 2 "$1" 2>/dev/null | tr -d ' \n')" = 55aa ] && return 0
    for _off in 512 4096; do
        [ "$(dd if="$1" bs=1 skip=$_off count=8 2>/dev/null)" = "EFI PART" ] && return 0
    done
    # util-linux blkid probes partition tables too with -p; busybox's has no
    # -p and probes filesystems.
    { blkid -p "$1" || blkid "$1"; } 2>/dev/null | grep -q 'TYPE=' && return 0
    return 1
}

# Prints how the boot treats $1:
#   small   smaller than any pd disk: never tried
#   blank   its salt bytes are all the same: never tried
#   used    looks partitioned or formatted: tried last
#   plain   random-looking: tried first
disk_class() {
    if [ -b "$1" ]; then
        _size=$(blockdev --getsize64 "$1" 2>/dev/null)
    else
        _size=$(stat -c %s "$1" 2>/dev/null)
    fi
    if [ "${_size:-0}" -lt "$PD_MIN_DISK" ]; then
        echo small
    elif salt_blank "$(disk_salt "$1")"; then
        echo blank
    elif looks_used "$1"; then
        echo used
    else
        echo plain
    fi
}

