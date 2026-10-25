#!/bin/sh
# NixOS: offer a generation menu while Shift is held, and set KERNEL,
# INITRD and CMDLINE from the chosen generation.  Sourced by late.sh.

# Generation files are symlinks into /nix/store; mount the store where those
# absolute links expect it.
mkdir -p /nix
mount -t btrfs -o ro,nodiscard,rescue=nologreplay,subvol=@nix "$ROOT_DEV" /nix

entry=default
if detect_shift; then
    # One line per generation: sort key, entry, label, boot.json path.
    # "default" sorts first, then generations newest first.
    list=$(for bootjson in "$R"/boot/*/system/boot.json; do
        e=$(basename "$(dirname "$(dirname "$bootjson")")")
        label=$(jq -r '.["org.nixos.bootspec.v1"].label // "no label"' "$bootjson")
        case "$e" in default) key=1000000 ;; *) key=${e#system-} ;; esac
        printf '%s\t%s\t%s\t%s\n' "$key" "$e" "$label" "$bootjson"
    done | sort -t "$(printf '\t')" -k1,1nr | cut -f2-)
    selected=$(printf '%s\n' "$list" |
        fzf --delimiter="$(printf '\t')" --with-nth=1,2 --layout=reverse --border \
            --border-label="Boot Entry" --color=16 --color='border:-1') || selected=
    [ -n "$selected" ] && entry=$(printf '%s' "$selected" | cut -f1)
fi

INITRD="/boot/$entry/initrd"
KERNEL="/boot/$entry/kernel"
CMDLINE="init=$(readlink "$R/boot/$entry/init") $(cat "$R/boot/$entry/system/kernel-params")"

