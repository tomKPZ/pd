# Frontier monitor.  Installed only in the hidden side's boot volume (as
# hooks/frontier.sh) and sourced by late.sh on every hidden boot.  It opens
# the decoy read-only with the decoy's data key, and reports how much space
# is left between the decoy's allocations (growing up from the start of the
# disk) and the hidden side's (growing down from the end).  Nothing is
# written to either side.

MIN_GAP_PERCENT=10
# A stock decoy (docs/stock-boot.md) has its own VG name and LUKS cipher
# settings; a pd decoy shares the hidden side's.
DECOY_VG=
DECOY_CRYPT_ARGS=
# The lowest sector the hidden side can reach at its cap: the decoy's data
# must stay below it (design.md §10.5).  Set by init-hidden.
FLOOR_SECTOR=
[ -r "$B/frontier.env" ] && . "$B/frontier.env"
: "${DECOY_VG:=$VG}" "${DECOY_CRYPT_ARGS:=$CRYPT_ARGS}"

# map_ranges TABLE: read "start end" sector ranges on stdin, map them through
# a linear dm TABLE (file), print the mapped "start end" ranges.
map_ranges() {
    awk -v table="$1" '
        BEGIN {
            while ((getline line < table) > 0) {
                split(line, f, " ")
                if (f[3] != "linear") continue
                n++; ls[n] = f[1]; len[n] = f[2]; ps[n] = f[5]
            }
        }
        {
            a = $1; b = $2
            for (i = 1; i <= n; i++) {
                lo = (a > ls[i]) ? a : ls[i]
                hi = (b < ls[i] + len[i]) ? b : ls[i] + len[i]
                if (lo < hi) printf "%d %d\n", ps[i] + lo - ls[i], ps[i] + hi - ls[i]
            }
        }'
}

# pool_ranges VG PV REGION_TABLE OUT [STALE_OUT]: physical disk ranges holding
# VG's thin-pool data that has been allocated so far (blocks 0..max).  With
# STALE_OUT, also the ranges up to the highest block any btree node in the
# pool's metadata maps, stale copies included (tmeta_scan; design.md §10.5).
pool_ranges() {
    vg=$1 pv=$2 region=$3 out=$4 stale_out=${5:-}
    l="lvm lvs --readonly --devices $pv --noheadings"
    pe_start=$(lvm pvs --readonly --devices "$pv" --noheadings --nosuffix --units s -o pe_start "$pv" 2>/dev/null | tr -d ' ')
    pool=$($l -o lv_name,segtype "$vg" 2>/dev/null | awk '$2 == "thin-pool" { print $1 }')
    lv_table "$vg" "${pool}_tmeta" "$pv" >/run/fr-tmeta.table
    lv_table "$vg" "${pool}_tdata" "$pv" >/run/fr-tdata.table
    dmsetup create --readonly fr-tmeta </run/fr-tmeta.table
    dmsetup mknodes fr-tmeta
    # Highest allocated data block over all thin devices, and the block size,
    # in sectors.
    set -- $(thin_dump /dev/mapper/fr-tmeta | awk '
        function attr(name,   m) {
            if (match($0, name "=\"[0-9]+\"")) {
                m = substr($0, RSTART, RLENGTH); sub(/^[^"]*"/, "", m); sub(/"$/, "", m)
                return m + 0
            }
            return -1
        }
        /<superblock/ { bs = attr("data_block_size") }
        /<range_mapping/ { e = attr("data_begin") + attr("length"); if (e > max) max = e }
        /<single_mapping/ { e = attr("data_block") + 1; if (e > max) max = e }
        END { print max * bs + 0, bs + 0 }')
    end=${1:-0} bs=${2:-0}
    if [ -n "$stale_out" ]; then
        stale=$(($(tmeta_scan /dev/mapper/fr-tmeta 2>/dev/null || echo 0) * bs))
        echo "0 $stale" | map_ranges /run/fr-tdata.table | map_ranges "$region" >"$stale_out"
    fi
    dmsetup remove fr-tmeta
    # Allocation is sequential, so blocks [0, max) are the used range.
    echo "0 $end" | map_ranges /run/fr-tdata.table | map_ranges "$region" >"$out"
}

frontier_check() {
    disk=$(cat /run/pd/disk)
    total=$(blockdev --getsz "$disk")

    sed "s|DISK|$disk|g" "$B/decoy.table" >/run/fr-decoy-region.table
    sed "s|DISK|$disk|g" "$B/table.txt" >/run/fr-hidden-region.table
    dmsetup create --readonly fr-decoy-raw </run/fr-decoy-region.table
    dmsetup mknodes fr-decoy-raw
    cryptsetup open --type plain --readonly --batch-mode $DECOY_CRYPT_ARGS \
        --key-file "${DECOY_KEY:-$B/decoy.key}" /dev/mapper/fr-decoy-raw fr-decoy

    pool_ranges "$DECOY_VG" /dev/mapper/fr-decoy /run/fr-decoy-region.table /run/fr-decoy.ranges \
        /run/fr-decoy-stale.ranges
    pool_ranges "$VG" /dev/mapper/cryptlvm /run/fr-hidden-region.table /run/fr-hidden.ranges

    # Decoy LVs other than the pool's data must stay out of the hidden
    # region: between the start of the pool's main data segment and the top
    # of the hidden boot volume (just above the hidden data region).
    decoy_pe_start=$(lvm pvs --readonly --devices /dev/mapper/fr-decoy --noheadings --nosuffix \
        --units s -o pe_start /dev/mapper/fr-decoy 2>/dev/null | tr -d ' ')
    overlap=$(lvm lvs --readonly --devices /dev/mapper/fr-decoy --noheadings --nosuffix --units s -a \
        -o lv_name,seg_pe_ranges,vg_extent_size "$DECOY_VG" 2>/dev/null | awk \
        -v pe_start="$decoy_pe_start" -v region=/run/fr-decoy-region.table -v hidden=/run/fr-hidden-region.table '
        BEGIN {
            # Decoy region: linear, one line.  Hidden top: highest mapped
            # sector of its data region plus the boot volume (522240).
            getline line < region; split(line, f, " "); base = f[5]
            while ((getline line < hidden) > 0) {
                split(line, f, " "); end = f[5] + f[2]; if (end > top) top = end
            }
            top += 522240
        }
        $2 ~ /^\/dev\// {
            split($2, r, ":"); split(r[2], e, "-")
            name = $1; gsub(/[][]/, "", name)
            n++; lv[n] = name; lo[n] = base + pe_start + e[1] * $3; hi[n] = base + pe_start + (e[2] + 1) * $3
            if (name ~ /_tdata$/ && e[2] - e[1] > big) { big = e[2] - e[1]; data_lo = lo[n] }
        }
        END {
            for (i = 1; i <= n; i++)
                if (lv[i] !~ /_tdata$/ && hi[i] > data_lo && lo[i] < top) print lv[i]
        }' | sort -u | tr '\n' ' ')

    cryptsetup close fr-decoy
    dmsetup remove fr-decoy-raw

    decoy_top=$(awk 'BEGIN { m = 0 } { if ($2 > m) m = $2 } END { print m }' /run/fr-decoy.ranges)
    stale_top=$(awk 'BEGIN { m = 0 } { if ($2 > m) m = $2 } END { print m }' /run/fr-decoy-stale.ranges)
    hidden_bottom=$(awk -v t="$total" 'BEGIN { m = t } { if ($1 < m) m = $1 } END { print m }' /run/fr-hidden.ranges)
    gap=$(((hidden_bottom - decoy_top) / 2048))
    min=$((total / 2048 * MIN_GAP_PERCENT / 100))
    rm -f /run/fr-*

    if [ -z "$decoy_pe_start" ]; then
        echo "WARNING: the decoy doesn't open with the key this side keeps for it (was it re-created"
        echo "without --hidden-offset?), so the numbers below are wrong. pd init-decoy --force"
        echo "--hidden-offset N updates it."
        printf "Press Enter to continue booting. "
        read -r _
    fi
    echo "Free space between the two sides: $gap MiB."
    if [ -n "$overlap" ]; then
        echo "WARNING: decoy volume(s) inside the hidden region: $overlap"
        printf "Press Enter to continue booting. "
        read -r _
    fi
    if [ -n "$FLOOR_SECTOR" ] && [ "$decoy_top" -gt "$FLOOR_SECTOR" ]; then
        echo "WARNING: the decoy has data in the space reserved for this side ($(((decoy_top - FLOOR_SECTOR) / 2048)) MiB)."
        echo "When this side grows there, it overwrites space the decoy has used, which can be detected."
        echo "Free space on the decoy, then run pd compact --hidden-offset N on it from the setup"
        echo "environment: it moves the decoy's data back below and clears the traces above."
        printf "Press Enter to continue booting. "
        read -r _
    fi
    # Where the decoy's data used to be: its pool metadata's stale nodes
    # still point there, and its freed blocks still hold the old data.
    if [ "$stale_top" -gt "$hidden_bottom" ]; then
        echo "WARNING: this side has data where the decoy's old data was: the decoy's pool metadata"
        echo "still points there, and an examiner could see that something wrote over it."
        echo "Run pd compact --hidden-offset N on the decoy from the setup environment soon: it"
        echo "erases those pointers (docs/design.md §10.5)."
        printf "Press Enter to continue booting. "
        read -r _
    elif [ -n "$FLOOR_SECTOR" ] && [ "$stale_top" -gt "$FLOOR_SECTOR" ] && [ "$decoy_top" -le "$FLOOR_SECTOR" ]; then
        echo "WARNING: the decoy's pool metadata still points into the space reserved for this side"
        echo "($(((stale_top - FLOOR_SECTOR) / 2048)) MiB of old data the decoy has freed)."
        echo "Before this side grows there, run pd compact --hidden-offset N on the decoy from the"
        echo "setup environment: it erases those pointers and the old data (docs/design.md §10.5)."
        printf "Press Enter to continue booting. "
        read -r _
    fi
    if [ "$gap" -lt "$min" ]; then
        echo "WARNING: less than $MIN_GAP_PERCENT% of the disk separates the two sides."
        echo "Free space on one side, or the other side's data will be overwritten."
        printf "Press Enter to continue booting. "
        read -r _
    fi
}

frontier_check || echo "Frontier check failed."

