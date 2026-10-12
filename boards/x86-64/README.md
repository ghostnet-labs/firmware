# x86_64 (Protectli VP2430 bench node)

A generic OpenWrt x86_64 image with the OpenMANET stack, built for the
Protectli Vault Pro VP2430 bench node. It boots any 64-bit UEFI or legacy
BIOS PC; the Protectli-specific setup (GNSS on the COM port, battery
monitor, port roles) lives in the `bsp-x86` package and only acts when the
DMI board name is a Protectli VP2430.

The bench design, wiring and test plan are in the docs repository under
`project/bench-tracks/protectli/`.

## Building

From the root of the repository:

```console
./scripts/openmanet_setup.sh -i -b x86-64 && make -j$(nproc) download && make -j$(nproc)
```

The images land in `bin/targets/x86/64/`. Use
`openmanet-*-x86-64-generic-squashfs-combined-efi.img.gz` on UEFI machines
(the VP2430 is UEFI) and `...-squashfs-combined.img.gz` on legacy BIOS.

CI builds the same image on every pull request that touches this board, the
common configs, the x86 target or the feeds (`build-pr-x86-64.yml`).

## What the image adds over upstream x86_64

| Function | Hardware on the bench node | Packages |
|---|---|---|
| HaLow | Gateworks GW16167 (MM8108) on a Pier42 USB carrier | `kmod-mm8108`, `mm8108-firmware` |
| Wi-Fi mesh | AsiaRF AW7916-AED (MT7916) in the internal M.2 E-key slot | `kmod-mt7915e`, `kmod-mt7916-firmware` |
| Ethernet | 4x Intel i226-V 2.5 GbE | `kmod-igc` |
| GNSS + PPS | u-blox MAX-M10S on the RS-232 COM port through a MAX3232, PPS on DCD | `gpsd`, `ntpd`, `kmod-pps-ldisc`, `pps-tools` |
| Battery telemetry | Adafruit INA260 on an Adafruit MCP2221A USB-I2C bridge | `kmod-usb-hid-mcp2221`, `kmod-hwmon-ina2xx`, `i2c-tools` |
| Push-to-talk audio | OpenVLM (USB audio + HID) | `kmod-usb-audio`, `kmod-usb-hid`, `alsa-utils` |
| Platform | Intel N150 | `intel-microcode`, `kmod-hwmon-coretemp`, `kmod-nvme`, `lm-sensors` |

## Installing on the VP2430

1. Write the `combined-efi` image to a USB stick (`gunzip -c image.img.gz |
   sudo dd of=/dev/sdX bs=4M conv=fsync`, or balenaEtcher).
2. Connect the USB-C console port to a laptop (115200 8N1), plug in the
   stick and power on. Pick the stick in the BIOS boot menu.
3. Once OpenMANET is up from the stick, copy the same image onto the
   internal SSD:

   ```console
   gunzip -c /tmp/image.img.gz | dd of=/dev/nvme0n1 bs=4M conv=fsync
   ```

   (`/dev/sda` if the box has a SATA SSD.) Remove the stick and reboot.
4. Later upgrades go through the normal sysupgrade path with the same
   `combined-efi` image.

The first-boot checks for the node (radios, GNSS lock, PPS, battery
monitor, port order) are in the docs repository bring-up runbook for this
track.
