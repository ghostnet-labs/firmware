# CN9130 ClearFog Pro USB mapping candidate (GHO-65)

[GHO-65](https://linear.app/ghostnet-labs/issue/GHO-65) asks whether the shipped board DTS attaches the COMPHY lane 1 USB 3.0 SuperSpeed lane to the wrong xHCI controller. The evidence record is the [Maer hardware qualification record](https://github.com/ghostnet-labs/docs/blob/main/project/poc/maer-hardware-qualification.md).

**`cn9130-clearfog-pro-mainline-usb.dts` is a candidate only. It is not built.** No image recipe references it, and `tests/test_dts_usb_topology.py` fails if a file under `target/`, `package/` or `include/` names it. The shipped `target/linux/mvebu/files-6.6/arch/arm64/boot/dts/marvell/cn9130-clearfog-pro.dts` is unchanged. Nothing here shows that the board has a hardware fault or that the shipped image is broken. It shows that the source descriptions disagree.

## What differs

The candidate `#include`s the shipped file and overrides only the two USB controller nodes:

| Controller (cp11x) | Shipped fork | Candidate (= Linux mainline wiring) |
|---|---|---|
| `cp0_usb3_0` @ `0x500000` | `phys = <&cp0_utmi0>` (`utmi`); mini PCIe CON3 USB 2.0 only | `phys = <&cp0_comphy1 0>, <&cp0_utmi0>` (`comphy`, `utmi`); Type-A SuperSpeed and CON3 USB 2.0 |
| `cp0_usb3_1` @ `0x510000` | `phys = <&cp0_utmi1>, <&cp0_comphy1 0>` (`utmi`, `usb`); Type-A USB 3.0 | `phys = <&cp0_utmi1>` (`utmi`); Type-A USB 2.0 only |

Both files keep `dr_mode = "host"`, and both keep the Type-A VBUS switch (`v_5v0_usb3_hst_vbus`, expander0 GPIO 6) on `cp0_usb3_1` through `usb-phy = <&cp0_usb3_0_phy1>`. Mainline puts `vbus-supply` on `cp0_usb3_0` instead. The candidate does not copy that, because v6.6 `drivers/usb/host/xhci-plat.c` reads `usb-phy` (line 256) and does not handle `vbus-supply`.

## Source evidence (checked against raw files from torvalds/linux)

- **v6.6 `arch/arm64/boot/dts/marvell/armada-cp11x.dtsi`:**
  - `utmi@580000` contains `usb-phy@0` (`utmi0`) and `usb-phy@1` (`utmi1`) (lines 293-310).
  - `usb3_0: usb@500000` has `reg = <0x500000 0x4000>` (line 315).
  - `usb3_1: usb@510000` has `reg = <0x510000 0x4000>` (line 327).
  - From `usb3_0` through `utmi1`, the block is the same text in v6.6 and in master. Master has `reg` at lines 316 and 328.
- **v6.6 `drivers/phy/marvell/phy-mvebu-cp110-comphy.c`, `mvebu_comphy_cp110_modes` (lines 214-216):**
  - `/* lane 1 */ GEN_CONF(1, 0, PHY_MODE_USB_HOST_SS, COMPHY_FW_MODE_USB3H)` lists port 0 only. Master has the same table.
  - The other USB_HOST_SS entries are lane 2 → port 0, lane 3 → port 1, and lane 4 → port 1.
  - `mvebu_comphy_xlate` (line 933) takes the port from the specifier argument: `lane->port = args->args[0]`. The driver never learns which controller node holds the reference. So the fork's `<&cp0_comphy1 0>` under `cp0_usb3_1` passes validation, but it programs the lane mux toward USB3 host 0.
- **Mainline [1c510c7d82e5](https://github.com/torvalds/linux/commit/1c510c7d82e52142953255896288eaac716efd72):**
  - `cn9130-cf.dtsi` lines 176-186 contain `SRDS #1 - USB-3.0 Host on Type-A connector / USB-2.0 Host on mPCI-e connector (CON3)` and `&cp0_usb3_0 { phys = <&cp0_comphy1 0>, <&cp0_utmi0>; phy-names = "comphy", "utmi"; ... }`.
  - `cn9130-cf-pro.dts` lines 351-357 contain `USB-2.0 Host on Type-A connector` and `&cp0_usb3_1 { phys = <&cp0_utmi1>; phy-names = "utmi"; ... }`.
  - Current master is identical for these nodes. Its only difference in `cn9130-cf.dtsi` is SATA.
- **v6.6 `drivers/usb/core/phy.c`:** `usb_phy_roothub_alloc` (lines 50-75) powers every `phys` entry by index, so `phy-names` does not change the v6.6 behaviour.

## Compare the two descriptions

```sh
python3 scripts/hardware-qualification/dts_usb_topology.py \
  target/linux/mvebu/files-6.6/arch/arm64/boot/dts/marvell/cn9130-clearfog-pro.dts \
  scripts/hardware-qualification/cn9130-usb/cn9130-clearfog-pro-mainline-usb.dts \
  -I target/linux/mvebu/files-6.6/arch/arm64/boot/dts/marvell \
  -I <linux-6.6>/arch/arm64/boot/dts/marvell -I <linux-6.6>/include
```

When `dtc` and `cpp` are installed, each file is preprocessed with `cpp -nostdinc -undef -x assembler-with-cpp` and then compiled with `dtc`. Use a prepared kernel tree, for example `build_dir/target-*/linux-mvebu_cortexa72/linux-6.6.*`, so the `dt-bindings` symlinks resolve. Without the tools, or with `--static`, the source nodes are parsed directly. In that mode, `cn9130.dtsi` is reported as an unresolved include and node addresses show as unavailable.

Each controller row shows status, phys (COMPHY lane/port or UTMI index), phy-names, dr_mode and usb-phy. A `note:` line means a COMPHY port argument does not equal the index of the controller that holds it. Other notes cover a lane/port pair that has no USB_HOST_SS entry in the v6.6 table. A note is a source inconsistency, not a hardware verdict.

## Bench steps to decide

Do these on a ClearFog Pro running the shipped image, and record the board revision and the bootloader version and build.

1. **Bootloader lane configuration.**
   - Save the U-Boot boot log, which prints the COMPHY lane table at boot. In U-Boot, also run `fdt addr $fdtcontroladdr; fdt print /cp0/config-space*/phy*` (or the platform COMPHY node) so lane 1's configured mode and port are on record.
   - Record any board-specific COMPHY settings in the ATF/U-Boot source used for the SPI/eMMC image.
2. **Type-A with a USB 3 device.**
   - Plug a known USB 3.x stick into the Type-A port with nothing on CON3.
   - Capture `lsusb -t` and `cat /sys/bus/usb/devices/*/speed`.
   - Note the bus number and speed: 5000M, or 480M for a USB 2 fallback.
   - Map the bus to a controller with `ls -l /sys/bus/usb/devices/usb*` (the path contains `f2500000.usb` or `f2510000.usb`).
3. **CON3 USB 2.0.** Fit a USB device or mini PCIe card that uses the USB pins in the mini PCIe slot far from the SOM (CON3). Repeat `lsusb -t` and record which `f25x0000.usb` bus it appears on.
4. **Kernel log.**
   - Capture `dmesg | grep -iE 'xhci|f25[01]0000|comphy|usb[0-9]'`.
   - Record which xHCI registers a SuperSpeed root hub ("USB 3.x" bus).
   - Record any `comphy`/`phy` power-on errors or "Cannot enable" messages.
5. Repeat steps 2-4 on an image built with the candidate (see below). Use a lab-only build that is never released.

The candidate is supported when all of these hold on the same board:

- A USB 3 stick in Type-A enumerates at 5000M under `f2500000.usb` (controller 0).
- A USB 2 device in Type-A enumerates under `f2510000.usb` (controller 1).
- The CON3 device enumerates under `f2500000.usb`.

The shipped description is supported when a Type-A USB 3 stick reaches 5000M under `f2510000.usb` instead.

## Activating the candidate (only after bench proof)

1. Attach the bench logs above to [GHO-65](https://linear.app/ghostnet-labs/issue/GHO-65) and update the qualification record.
2. Copy the two override nodes from the candidate into `cn9130-clearfog-pro.dts` in place of the existing `&cp0_usb3_0` and `&cp0_usb3_1` nodes. Keep the comments, and delete the candidate file in the same change.
3. Build `solidrun_clearfog-pro` and confirm the change with this tool: the compiled table shows no `note:` lines.
4. Repeat the bench steps on the built image before release.
