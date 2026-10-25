# Roadmap

Status values: ☐ not started · ◐ in progress · ☑ done.

## v2 milestones

| | Milestone | Done when |
|---|---|---|
| ☑ | **M0 Docs**: README, design, threat model, booting, v1 reference, migration, history, AGENTS.md | the author has reviewed them |
| ☑ | **M1 Nix flake**: static busybox, cryptsetup, argon2, efibootmgr, lvm2 + thin-provisioning-tools, kexec-tools, fzf, jq, detect_shift; kernel + module set; UKI template; test VM | `nix build` produces all artifacts |
| ☑ | **M2 Test harness**: QEMU + KVM + OVMF as an ordinary user, pexpect on the serial console, sparse disk images | a smoke VM boots and runs commands |
| ◐ | **M3 Churn tests**: btrfs vs ext4 address reuse under nodiscard; `--discards=ignore` refuses `fstrim`; revive the 2024 disk visualizer | written result; filesystem for `data` decided (D18) |
| ☑ | **M4 Setup and boot chain v2**, with a tiny fake target OS | end-to-end decoy boot and hidden boot (via the recovery-shell `sed`) pass in QEMU |
| ☑ | **M5 Layout and safety tests**: decoy stock-layout read-back, hidden placement, no overlaps, hidden caps, read-only boot environment, `nologreplay` after a simulated crash, NVRAM entry deletion, LUKS→plain equivalence | tests pass |
| ◐ | **M6 Target integrations**: shared `hook.sh`, NixOS module (from `patches/`), Arch mkinitcpio hook; optional real NixOS/Arch installs in tests | NixOS end-to-end in QEMU, at least |
| ☑ | **M7 Frontier monitor** (D15) | warns in a test where the decoy grows toward the hidden region |
| ◐ | **M8 Docs pass**: bring all docs in line with what was built | — |
| ☑ | **M9 Migration tool** and guide for v1 installs ([migration.md](migration.md)) | migration test on a v1 image passes |

Notes on status:
* M3: ext4 measured unsafe; btrfs long-run (200 cycles) pending. The disk visualizer isn't revived yet.
* M6: NixOS module tested with a real NixOS initrd; Arch hook written but untested (needs an Arch
  install in a VM); no full OS installs in tests yet.
* M7: the decoy-segment overlap warning isn't implemented yet.

Not done autonomously: anything involving real disks or USB sticks, pushing to the remote, external NixOS
configuration repos, and running a migration.

## Later / ideas

* Safe decoy re-initialization: require the hidden offset or password, check for overlap before writing,
  and never re-randomize.
* `pd rekey`: change a password, and/or the salt, by re-creating a plain volume.
* `pd open` / `pd close` for maintenance from a live ISO.
* Memorable hidden offset ([design.md §3](design.md#3-disk-layout)).
* Ctrl-C at the password prompt drops straight to the recovery shell, skipping Argon2.
* Keymap support in the boot environment.
* FIDO2 as an extra input to Argon2.
* A "stock boot" variant whose USB doesn't identify pd ([threat-model.md](threat-model.md)).
* A project name.

