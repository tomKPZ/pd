#!/usr/bin/env bash
set -euo pipefail

mkdir /nix
mount -t btrfs "/dev/$VG/$LV_ROOT" /nix -o,defaults,nodiscard,ro,subvol=@nix

if /mnt/plain/detect_shift; then
    temp_file=$(mktemp)

    # Build the menu list.
    # For each boot.json in /boot/*/system, extract:
    #   - boot_entry: the directory name directly under /boot (e.g. "default" or "system-163")
    #   - label: from .["org.nixos.bootspec.v1"].label in boot.json
    # Assign a sort key: for "default", use 1000000; for others, use the numeric part of "system-XXX".
    for bootjson in /mnt/luks/boot/*/system/boot.json; do
        boot_entry=$(basename "$(dirname "$(dirname "$bootjson")")")
        label=$(jq -r '.["org.nixos.bootspec.v1"].label // "no label"' "$bootjson")
        if [ "$boot_entry" = "default" ]; then
            sort_key=1000000
        else
            sort_key=${boot_entry#system-}
        fi
        # Output: sort_key, boot_entry, label, bootjson path (tab-separated)
        echo -e "${sort_key}\t${boot_entry}\t${label}\t${bootjson}"
    done | sort -t $'\t' -k1,1nr | cut -f2- >"$temp_file"

    # Launch fzf:
    #   --delimiter splits on tabs.
    #   --with-nth=1,2 shows the boot entry and label.
    #   --preview-window=down:50% displays the preview as a horizontal split.
    selected=$(fzf --delimiter=$'\t' --with-nth=1,2 --layout=reverse --border \
        --border-label="Boot Entry" \
        --preview-window=down:50% \
        --color=16 --color='border:-1' \
        --preview 'bat --style=numbers --color=always --line-range :100 {3}' <"$temp_file")

    rm "$temp_file"

    [ -z "$selected" ] && exit 1

    # Extract and output the boot entry name (first field).
    boot_entry_selected=$(echo "$selected" | cut -f1)
else
    boot_entry_selected=default
fi

export INITRD="/boot/$boot_entry_selected/initrd"
export KERNEL="/boot/$boot_entry_selected/kernel"
INIT="$(readlink "/mnt/luks/boot/$boot_entry_selected/init")"
CMD="$(cat "/mnt/luks/boot/$boot_entry_selected/system/kernel-params")"
export CMDLINE="init=$INIT $CMD"
