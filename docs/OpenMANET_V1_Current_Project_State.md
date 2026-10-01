# OpenMANET Compact Rugged Node --- V1 Hardware Project State

**Document type:** Engineering handoff / project-resume document\
**Project:** OpenMANET Compact Rugged Node\
**Revision:** Current state as of 2026-09-30\
**Status:** Architecture defined; first real hardware-design pass ready
to continue\
**Target PCB:** 117 mm × 67 mm working target\
**Primary compute:** Raspberry Pi Compute Module 5\
**Design intent:** Rugged, compact, fanless, headless OpenMANET node;
EUD-controlled in normal operation

------------------------------------------------------------------------

## 1. Purpose

This is the current authoritative handoff state for the OpenMANET V1
hardware project. It is written so another engineer or agent can resume
the project without the previous conversation.

It captures the product requirements, architecture, selected and
rejected hardware, electrical interfaces, power/RF/USB/GNSS/Ethernet
architecture, schematic organization, mechanical floorplan, routing
priorities, thermal strategy, unresolved decisions, validation
requirements, and the exact recommended resume point.

------------------------------------------------------------------------

# 2. Product Definition

The goal is a compact rugged OpenMANET node approximately in the
physical class of an MPU5-style field node.

The node is intended to provide:

-   Raspberry Pi Compute Module 5 compute
-   900 MHz-class Wi-Fi HaLow mesh
-   2.4/5/6 GHz Wi-Fi
-   GNSS positioning and timing
-   Gigabit Ethernet
-   removable Fly-Wheel/Twist-Lock battery
-   battery voltage/current/power telemetry
-   board/radio/CPU thermal telemetry
-   hardware-aware mesh telemetry exposed to software
-   fanless operation
-   no normal-use physical controls
-   no user-facing status LEDs
-   EUD-controlled normal operation

This is being designed as a real product architecture, not a disposable
development-board prototype.

------------------------------------------------------------------------

# 3. Design Philosophy

Priority order:

1.  Electrically sane
2.  RF sane
3.  Thermally sane
4.  Mechanically sane
5.  Debuggable
6.  EUD-controlled
7.  Manufacturable
8.  Size/cost optimization

The 117 × 67 mm PCB target is important but not sacred. Do not
compromise RF, thermal, mechanical, electrical, or serviceability
requirements solely to preserve the target.

However, current analysis indicates that 117 × 67 mm is plausible, so
the current rule is:

> Do not enlarge the PCB until an actual CAD assembly proves the target
> cannot be achieved without compromising the design.

------------------------------------------------------------------------

# 4. Product Requirements

## Compute

-   Raspberry Pi CM5
-   8 GB RAM
-   32 GB eMMC
-   no dependency on CM5 onboard wireless
-   CM5 is the primary compute module

## Wireless

### HaLow

-   900 MHz-class Wi-Fi HaLow
-   North American 902--928 MHz target
-   external antenna
-   mesh capable
-   hardware power isolation
-   USB interface

### Wi-Fi

-   2.4 GHz
-   5 GHz
-   6 GHz
-   2T2R
-   external antennas
-   PCIe WLAN interface
-   USB Bluetooth interface

## GNSS

-   multi-constellation
-   external active antenna
-   UART
-   PPS/time pulse
-   reset/control
-   software-accessible timing

## Ethernet

-   Gigabit Ethernet
-   integrated magnetics
-   edge-mounted RJ45
-   no PoE
-   no user-facing Ethernet LEDs

## Power

-   removable external battery
-   Fly-Wheel/Twist-Lock battery interface
-   current design input target approximately 8--28 V
-   battery telemetry
-   reverse-polarity protection
-   transient protection
-   eFuse/current limiting
-   regulated 5 V
-   regulated 3.3 V
-   independently switchable radios
-   hardware supervision/watchdog

## Mechanical

-   target PCB: 117 × 67 mm
-   rugged enclosure
-   aluminum enclosure baseline
-   fanless
-   external RF
-   external battery
-   external Ethernet
-   no buttons
-   no user LEDs
-   no required external debug connector

------------------------------------------------------------------------

# 5. System Architecture

``` text
                         +---------------------+
                         |     CM5 COMPUTE     |
                         |  8 GB / 32 GB eMMC  |
                         +----------+----------+
                                    |
              +---------------------+---------------------+
              |                     |                     |
             PCIe                  USB2               GPIO/I2C/UART
              |                     |                     |
              v                     v                     v
       +-------------+       +-------------+       +-------------+
       | AIW-170BQ   |       | TUSB4020BI  |       | Supervisors |
       | Wi-Fi 6E    |       | USB 2 Hub   |       | GNSS / PWR  |
       +------+------+       +------+------+       +-------------+
              |                     |
          2x MHF4               +----+----+
              |                 |         |
              v                 v         v
        Wi-Fi antennas       GW16170   AIW BT
                             HaLow      USB BT
                               |
                              MMCX
                               |
                         HaLow antenna

CM5 ---- Integrated Gigabit PHY ---- Bel 1840888-4 ---- RJ45

CM5 ---- UART / I2C / PPS ---- MAX-M10S ---- GNSS antenna

BATTERY
   |
MC327-5
   |
10 mΩ Kelvin shunt
   |
   +------ INA228
   |
Input protection:
   +-- SMBJ33A TVS
   +-- CSD19533Q5A reverse protection
   +-- TPS26633 eFuse
   |
VBAT_PROTECTED
   |
   +------ LM76005 ---- +5V_SYS ---- CM5 / USB
   |
   +------ LM76005 ---- +3V3_RADIO
                         |
                         +-- TPS22975 -- WIFI_3V3
                         |
                         +-- TPS22975 -- HALOW_3V3
                         |
                         +-- filter ---- +3V3_GNSS
```

------------------------------------------------------------------------

# 6. Compute

## Selected: Raspberry Pi Compute Module 5

Current part:

**Raspberry Pi CM5008032**

-   8 GB RAM
-   32 GB eMMC
-   55 × 40 mm
-   4 × M2.5 mounting holes
-   integrated Gigabit Ethernet PHY
-   PCIe Gen2 x1
-   USB2
-   Linux ecosystem

### Why selected

CM5 provides substantial Linux compute, eMMC, PCIe, USB, Ethernet, and a
mature ecosystem in a compact module. It lets the carrier board
implement the product-specific power, RF, battery, telemetry, and rugged
I/O rather than recreating the processor subsystem.

### Mechanical implementation

Selected carrier connector:

**Amphenol 10164227-1004A1RLF**

-   4.0 mm connector
-   approximately 7.44 mm connected CM5 stack height
-   approximately 2.5 mm clearance beneath CM5

The underside clearance is reserved primarily for low-profile passives
and routing.

Do not place:

-   inductors
-   switching regulators
-   tall capacitors
-   connectors

under the CM5.

### CM5 electrical decisions

-   `GPIO_VREF` tied to CM5 3.3 V; it must not float.
-   All CM5 5 V pins tied to `+5V_CM5`.
-   Integrated BCM54210PE PHY is used.
-   PCIe Gen2 x1 is used for Wi-Fi.
-   USB2 is used for the internal USB hub.
-   `PWR_Button` goes to internal `TP_PWR_BUTTON`, not a physical
    button.
-   `nRPI_BOOT` goes to internal `TP_NBOOT`, not a physical button.
-   `PMIC_Enable` is used for hardware recovery.
-   No user LEDs.
-   Ethernet LED outputs are unused.
-   CM5 power/activity LEDs are unused.

### Debug

No external debug USB connector.

Debug is through internal test pads and internal debug UART/test points.

------------------------------------------------------------------------

# 7. Wi-Fi 6E

## Selected: Advantech AIW-170BQ-001

Characteristics:

-   M.2 2230 E-Key
-   approximately 30 × 22 × 2.2 mm
-   Qualcomm WCN6856/QCA2066 family
-   Wi-Fi 6E
-   2.4/5/6 GHz
-   2T2R
-   PCIe WLAN
-   USB Bluetooth
-   2 × MHF4
-   approximately -40 to +85 °C

### Why selected

It fits the size, temperature, external-antenna, Wi-Fi 6E, PCIe, and
USB-Bluetooth requirements.

### Important documentation correction

Older/alternate information suggested Bluetooth UART. Current official
Advantech documentation used for V1 says:

-   WLAN = PCIe
-   Bluetooth = USB

Therefore V1 routes Bluetooth through the USB hub.

### Power

``` text
+3V3_RADIO
     |
 TPS22975
     |
 WIFI_3V3
```

Control:

`WIFI_PWR_EN`

### Antennas

Two MHF4 external antenna connectors.

No PCB antenna.

### Remaining validation

Before freeze:

-   verify exact M.2 pin assignment
-   verify PCIe reset/CLKREQ behavior
-   verify power sequencing
-   validate Linux/ath11k path
-   validate Bluetooth USB on CM5

------------------------------------------------------------------------

# 8. Wi-Fi PCIe

Architecture:

``` text
CM5 PCIe Gen2 x1
   |
   +-- 100 MHz REFCLK
   +-- PCIe TX/RX
   +-- PERST#
   +-- CLKREQ#
   |
   v
AIW-170BQ
```

CM5 PCIe TX already has the relevant AC coupling.

Therefore:

-   do not add capacitors to the CM5 TX path
-   add 220 nF capacitors on AIW TX -\> CM5 RX as required

`PEWAKE#/nWAKE` is not currently used.

M.2 socket reference:

**TE Connectivity 2199119-6**

Approximate envelope:

-   21.9 × 8.7 × 3.2 mm

The exact footprint/pin implementation must be checked against the
manufacturer's documentation before freeze.

------------------------------------------------------------------------

# 9. HaLow

## Selected: Gateworks GW16170 / Morse Micro MM8108-M20

Characteristics:

-   M.2 2230 E-Key
-   30 × 22 × 3.5 mm
-   3.3 V
-   USB2
-   external MMCX
-   902--928 MHz North American target
-   up to approximately +28.5 dBm TX
-   up to approximately 43.3 Mbps
-   -40 to +85 °C

### Why selected

It provides the desired 900 MHz HaLow capability in a compact M.2 module
with external antenna and Linux networking.

### Important software caveat

Gateworks documents the module as tested/supported on Gateworks
platforms. CM5 support is not yet proven by this project.

Therefore:

-   Linux driver validation
-   firmware validation
-   USB enumeration
-   power sequencing
-   mesh software integration

are explicit V1 tasks.

### Power

``` text
+3V3_RADIO
     |
 TPS22975
     |
 HALOW_3V3
```

Control:

`HALOW_PWR_EN`

### Antenna

MMCX, external.

### Critical unresolved item

Verify GW16170-specific definitions before wiring:

-   `W_DISABLE1#`
-   `W_DISABLE2#`
-   `PERST#`
-   `CLKREQ#`
-   `PEWAKE#`

Do not infer these from generic Morse Micro documentation or another
Gateworks module.

------------------------------------------------------------------------

# 10. USB

## Selected architecture: two-port USB 2.0 hub

## Selected hub: TI TUSB4020BI

Characteristics:

-   USB2
-   480 Mbps
-   2 downstream ports
-   approximately -40 to +85 °C
-   approximately 9 × 9 mm HTQFP
-   24 MHz crystal
-   strap configuration
-   no special host driver

### Why selected

The CM5 needs two internal USB devices:

1.  GW16170 HaLow
2.  AIW-170BQ Bluetooth

A two-port hub gives a simple, deterministic internal topology without
unnecessary extra ports.

### Topology

``` text
CM5 USB2
   |
   v
TUSB4020BI
   |
   +-- Port 1 -> GW16170
   |
   +-- Port 2 -> AIW-170BQ Bluetooth
```

### Configuration

-   strap configuration
-   no EEPROM
-   24 MHz crystal
-   `USB_HUB_RESET_N`

### Power

USB VBUS switching is separate from radio 3.3 V power switching.

The TPS22975 devices remain dedicated to radio 3.3 V.

Dedicated USB VBUS switches remain to be selected.

### Fault signals

-   `HALOW_USB_FAULT_N`
-   `BT_USB_FAULT_N`

### Recovery hierarchy

1.  device reset
2.  hub reset
3.  individual radio power-cycle
4.  full system recovery

### Open items

-   exact USB VBUS load switches
-   internal USB ESD
-   exact hub strap configuration

------------------------------------------------------------------------

# 11. GNSS

## Selected: u-blox MAX-M10S-00B

Characteristics:

-   approximately 9.7 × 10.1 × 2.5 mm
-   multi-constellation
-   UART
-   I2C
-   PPS/time pulse
-   reset
-   integrated LNA/SAW
-   -40 to +85 °C

### Why selected

Compact, industrial-temperature, multi-constellation, and provides the
UART/PPS/timing functionality required.

### Interfaces

Primary:

-   UART
-   PPS
-   reset

Secondary:

-   I2C

### Power

``` text
+3V3_RADIO
     |
 filtering
     |
+3V3_GNSS
```

No dedicated GNSS power switch currently planned.

### Antenna

External active antenna.

`VCC_RF` provides antenna bias.

### Placement

GNSS goes in a quiet RF corner, away from:

-   buck converters
-   Ethernet magnetics
-   CM5 high-speed routing
-   Wi-Fi
-   HaLow

### Backup

`V_BCKP` is reserved.

Actual backup storage is not selected.

### Test pads

-   SAFEBOOT_N
-   EXTINT

### RF protection

Do not automatically populate generic TVS on the GNSS RF line.
Protection must be selected specifically for the 1.575 GHz path and its
capacitance/insertion-loss requirements.

------------------------------------------------------------------------

# 12. Ethernet

## Selected: Bel/TRP 1840888-4

Characteristics:

-   1×1 1000BASE-T
-   integrated magnetics
-   through-hole
-   shielded
-   approximately -40 to +85 °C
-   no LEDs
-   non-PoE

### Why selected

The CM5 already includes the BCM54210PE Gigabit PHY.

Therefore an external PHY is unnecessary.

The selected connector provides the physical Ethernet interface and
integrated magnetics without adding PoE or LED complexity.

### Architecture

``` text
CM5 integrated PHY
      |
      +-- 4 x 100 Ω differential MDI pairs
      |
      v
Bel 1840888-4
      |
      v
RJ45
```

### Deliberately not selected

#### External Ethernet PHY

Not selected because it duplicates CM5 functionality and adds power,
area, cost, and complexity.

#### PoE

Not selected because it is not a requirement and would add substantial
power-path and thermal complexity.

#### Ethernet LEDs

Not populated because the node is intentionally headless and status
belongs in software/EUD.

### ESD

A low-capacitance Gigabit Ethernet ESD device is required near the RJ45.

Exact part remains open.

### Chassis

`CHASSIS_GND` is reserved.

Do not arbitrarily connect RJ45 shield directly to digital ground.

### Timing

CM5 Ethernet PHY supports IEEE 1588-2008 and exposes 3.3 V `SYNC_OUT`.

`ETH_SYNC_OUT` is reserved to CM5 GPIO/test point.

It is not hard-wired to GNSS PPS.

------------------------------------------------------------------------

# 13. Battery

## Selected connector: Mil-Con MC327-5

Current known characteristics:

-   Fly-Wheel/Twist-Lock family
-   3 positions plus ground
-   spring-loaded contacts
-   printed-circuit-tail/radio-side connector
-   approximately 6 A family capability
-   intended for AN/PRC-148 / AN/PRC-152-style batteries

### Why selected

It matches the desired field battery interface and rugged radio-style
mechanical concept.

### Critical open issue

Do not freeze the third/auxiliary contact function without verifying the
exact battery SKU and connector documentation.

Also do not freeze connector coordinates until the authoritative MC327-5
mechanical drawing/STEP is imported.

### Battery electrical assumption

Current system design target:

**approximately 8--28 V input**

A published reference battery in the family has been seen at
approximately:

-   10.8 V nominal
-   12.6 V max
-   6 A continuous
-   40 A pulse ≤1 ms

These values are not the final battery specification until the actual
field battery SKU is selected.

------------------------------------------------------------------------

# 14. Current Shunt

## Selected: Vishay WFK0612R0100FE66

-   10 mΩ
-   1%
-   1 W
-   4-terminal Kelvin

At:

-   5 A: 0.25 W
-   6 A: 0.36 W

### Layout

Kelvin sense traces:

-   short
-   symmetric
-   direct to INA228
-   isolated from high-current switching paths

Final thermal derating still needs verification.

------------------------------------------------------------------------

# 15. Battery Monitor

## Selected: TI INA228AIDGSR

Capabilities:

-   high-voltage bus measurement
-   20-bit measurement
-   current
-   voltage
-   power
-   energy
-   charge
-   I2C
-   alert
-   die temperature

### Why selected

It provides much richer battery telemetry than a simple
ADC/current-sense implementation.

Desired software telemetry:

-   battery voltage
-   battery current
-   battery power
-   charge/energy
-   monitor temperature
-   alert/fault state

Place close to the shunt.

------------------------------------------------------------------------

# 16. Input Protection

## Selected: TI TPS26633RGER

Characteristics:

-   approximately 4.5--60 V
-   approximately 6 A class
-   adjustable current limit
-   UVLO/OVLO
-   soft start
-   reverse-current blocking
-   current monitoring
-   PGOOD
-   fault
-   thermal protection

### Why selected

It creates a central controlled battery-input protection point and
supports the desired current/voltage protection architecture.

### Initial targets

-   current limit: \~5.5 A
-   power limit target: \~40 W
-   UVLO: \~7.5--8 V
-   OVLO: \~30--31 V

Exact resistor values remain to be calculated.

------------------------------------------------------------------------

# 17. Reverse Polarity

## Selected candidate: TI CSD19533Q5A

-   100 V N-MOSFET
-   approximately 5 × 6 mm class

### Why selected

It provides a robust high-voltage external MOSFET candidate for the
battery input protection path.

### Still open

Verify:

-   topology
-   gate drive
-   TPS26633 compatibility
-   startup behavior
-   reverse-current behavior
-   SOA/transient performance

before freezing.

------------------------------------------------------------------------

# 18. TVS

## Initial selected candidate: Diodes Inc. SMBJ33A

-   33 V standoff
-   approximately 36.7--42.2 V breakdown
-   approximately 53.3 V max clamp
-   600 W
-   unidirectional

### Why selected

Reasonable initial candidate for high-energy battery input transient
suppression.

### Not yet final

Must be validated against:

-   actual battery
-   cable/transient environment
-   TPS26633 maximum voltage
-   reverse-protection topology
-   pulse duration

------------------------------------------------------------------------

# 19. 5 V Buck

## Selected: TI LM76005

Configuration:

-   5.0 V
-   approximately 2 A initial allocation
-   approximately 10 W target
-   synchronous buck
-   3.5--60 V input
-   5 A class capability

### Why selected

Wide input range and substantial current capability make it suitable for
the battery input and CM5 supply.

Using the same regulator family for 5 V and 3.3 V simplifies design and
validation.

### Initial values

-   frequency: \~400 kHz
-   inductor starting point: \~6.8 µH
-   inductor: ≥6 A saturation, ≥5 A thermal, low DCR, shielded
-   input caps: 4.7 µF + 4.7 µF + 47 nF
-   output: multiple 22 µF ceramics

Final selection must follow TI reference design and calculated
load/transient requirements.

------------------------------------------------------------------------

# 20. 3.3 V Buck

## Selected: TI LM76005

Configuration:

-   3.3 V
-   approximately 4 A initial allocation
-   approximately 13.2 W target
-   approximately 400 kHz initial switching frequency

### Why selected

Same regulator family, wide input range, high current capability, and
simplified design/validation.

### Critical layout rule

Do not place the switching regulator or its inductor directly under or
adjacent to RF modules.

------------------------------------------------------------------------

# 21. Radio Power Switches

## Selected: TI TPS22975DSGT ×2

One each:

-   U302 = Wi-Fi
-   U303 = HaLow

Characteristics:

-   0.6--5.7 V
-   up to 6 A
-   approximately 16 mΩ typical
-   adjustable rise time
-   QOD
-   thermal shutdown
-   approximately -40 to +105 °C

### Why selected

Independent radio power control is important for software recovery and
hardware-aware mesh behavior.

``` text
+3V3_RADIO
    |
    +-- U302 -- WIFI_3V3
    |
    +-- U303 -- HALOW_3V3
```

------------------------------------------------------------------------

# 22. Supervisor / Watchdog

## Selected: TI TPS386000RGPR

### Functions

-   multi-rail supervision
-   watchdog
-   fault/reset behavior

### Rails

-   SVS1 = CM5_3V3
-   SVS2 = +5V_SYS
-   SVS3 = +3V3_RADIO
-   SVS4 = VBAT_PROTECTED

### Watchdog

`SUPERVISOR_WDI` from CM5.

`SUPERVISOR_WDO` back to CM5.

Initial timeout:

**\~1--2 seconds**

Software heartbeat should be faster.

### Hardware recovery

Supervisor fault/watchdog output is intended to participate in a path
that can pull:

`SYS_PMIC_EN`

low.

This uses CM5 `PMIC_Enable`.

### Important correction

CM5 `nEXTRST` is not being used as the external reset mechanism.

### Validation

Startup, release, watchdog timeout, and recovery behavior must be
bench-tested before being treated as field-reliable.

------------------------------------------------------------------------

# 23. Power Budget

Initial design allocation:

  Rail                                Allocation   Approx. output
  --------------------------------- ------------ ----------------
  +5V_SYS                                    2 A             10 W
  +3V3_RADIO                                 4 A           13.2 W
  Combined                                   ---         \~23.2 W
  Approx. input at 90% efficiency            ---         \~25.8 W
  System capability target                   ---           \~35 W
  35 W / 8 V                                 ---          \~4.4 A

The input path should support at least approximately 5 A continuous at
low battery voltage.

Initial eFuse current limit is approximately 5.5 A.

The 35 W value is a design capability target, not expected normal
consumption.

Actual power consumption must be measured on hardware.

------------------------------------------------------------------------

# 24. Fanless Thermal Strategy

-   aluminum enclosure as heat spreader
-   thermal interface from CM5 to enclosure
-   copper thermal areas
-   thermal vias
-   concentrated power island
-   no fan
-   no switching regulators beneath RF modules
-   power components kept away from GNSS/RF
-   major heat sources given deliberate thermal paths

The enclosure is part of the thermal design.

------------------------------------------------------------------------

# 25. GPIO / Control Assignment

Current logical map:

      GPIO Signal                Function
  -------- --------------------- ---------------------
         0 `GNSS_UART_TX`        GNSS UART
         1 `GNSS_UART_RX`        GNSS UART
         2 `SYS_I2C_SDA`         system I2C
         3 `SYS_I2C_SCL`         system I2C
         4 `HALOW_PWR_EN`        HaLow power
         5 `WIFI_PWR_EN`         Wi-Fi power
         6 `HALOW_FAULT_N`       HaLow fault
         7 `WIFI_FAULT_N`        Wi-Fi fault
         8 `GNSS_RESET_N`        GNSS reset
         9 `GNSS_PPS`            GNSS timing
        10 `SUPERVISOR_WDI`      watchdog heartbeat
        11 `SUPERVISOR_WDO`      watchdog status
        12 `POWER_GOOD`          power-good
        13 `EFUSE_FAULT`         eFuse fault
        14 `USB_HUB_RESET_N`     USB hub reset
        15 `HALOW_USB_FAULT_N`   HaLow USB fault
        16 `BT_USB_FAULT_N`      Bluetooth USB fault
        17 `ETH_SYNC_OUT`        Ethernet timing
    18--27 Reserved              future expansion

Dedicated:

-   `SYS_PMIC_EN` = CM5 PMIC enable
-   `TP_PWR_BUTTON` = internal test pad
-   `TP_NBOOT` = internal test pad

### Important

This is a **logical assignment**, not yet a frozen physical CM5
pin-number assignment.

Before schematic freeze, verify every GPIO against the current CM5
datasheet/IO documentation, muxing, boot behavior, and Linux device-tree
requirements.

------------------------------------------------------------------------

# 26. Schematic Sheets

## `01_CM5.sch`

-   CM5
-   2 × Amphenol CM5 connectors
-   +5V_CM5
-   GPIO_VREF
-   PCIe
-   USB2
-   Ethernet
-   UART
-   I2C
-   PPS
-   PMIC_Enable
-   internal test pads

## `02_PCIE_WIFI.sch`

-   AIW-170BQ
-   M.2 E-Key socket
-   PCIe Gen2 x1
-   100 MHz REFCLK
-   PERST#
-   CLKREQ#
-   220 nF AIW TX AC caps
-   Wi-Fi TPS22975
-   2 × MHF4

## `03_USB_HALOW.sch`

-   TUSB4020BI
-   24 MHz crystal
-   CM5 USB2 upstream
-   GW16170
-   AIW Bluetooth
-   hub reset
-   USB fault signals
-   USB VBUS switches
-   USB ESD

## `04_ETHERNET.sch`

-   CM5 integrated PHY interface
-   Bel 1840888-4
-   four MDI differential pairs
-   Ethernet ESD
-   chassis/shield
-   ETH_SYNC_OUT

## `05_GNSS.sch`

-   MAX-M10S-00B
-   UART
-   I2C
-   PPS
-   reset
-   VCC_RF
-   active antenna
-   optional RF protection/filter footprints
-   backup provision
-   test pads

## `06_POWER.sch`

-   MC327-5
-   10 mΩ shunt
-   INA228
-   SMBJ33A
-   CSD19533Q5A
-   TPS26633
-   LM76005 5 V
-   LM76005 3.3 V
-   TPS22975 ×2
-   GNSS filtering
-   protection/fault/telemetry

## `07_SYSTEM.sch`

-   GPIO assignment
-   supervisor
-   watchdog
-   PMIC_Enable recovery
-   radio power/fault
-   GNSS reset/PPS
-   USB hub reset/fault
-   Ethernet timing
-   reserved GPIO

------------------------------------------------------------------------

# 27. Mechanical Architecture

## PCB

**117 × 67 mm working target**

## Enclosure

Current concept:

-   rectangular aluminum
-   approximately 2.0 mm wall assumption
-   approximately 1.5 mm internal component-to-wall clearance assumption
-   fanless
-   four M3 mounting points
-   external RF connectors
-   external RJ45
-   external battery connector

Exact enclosure is not frozen.

------------------------------------------------------------------------

# 28. Current Floorplan

Conceptual:

``` text
                         117 mm
+-------------------------------------------------------------+
|                                                             |
| +----------------------+                                    |
| |      GW16170         |                                    |
| |      30 x 22         |        RF / antenna edge           |
| +----------------------+                                    |
|                                                             |
|              +-------------------------------+              |
|              |                               |              |
|              |             CM5               |              |
|              |            55 x 40            |              |
|              |                               |              |
|              +-------------------------------+              |
|                                      +--------------------+ |
|                                      |     AIW-170BQ      | |
|                                      |       30 x 22      | |
|                                      +--------------------+ |
|                                                             |
| GNSS             POWER / DC       USB             RJ45      |
| +---+       +-----------------+                 +--------+ |
| |   |       | protection      |                 |        | |
| +---+       | 5V / 3V3       |                 |        | |
|             +-----------------+                 +--------+ |
|                                                             |
| MC327-5                                                     |
+-------------------------------------------------------------+
                          67 mm
```

This is a concept only.

Actual component placement must use manufacturer STEP models.

------------------------------------------------------------------------

# 29. Mechanical Placement Rules

## CM5

Central/upper anchor.

Working envelope:

-   X ≈ 31--86 mm
-   Y ≈ 24--64 mm

Approximate hole references for that assumed placement:

-   (34.5, 27.5)
-   (82.5, 27.5)
-   (34.5, 60.5)
-   (82.5, 60.5)

These are not frozen coordinates.

## HaLow

Upper/left RF region.

MMCX toward RF enclosure wall.

## Wi-Fi

Upper/right RF region.

MHF4 connectors toward RF enclosure wall.

## GNSS

Quiet lower/left region.

## Power

Lower/middle.

Approximate working region: 45 × 25 mm.

## USB hub

Lower/central area or bottom side.

Do not put power inductors under RF modules.

## Ethernet

Lower/right board edge.

## Battery

Lower/left board edge.

Exact position is blocked by MC327-5 CAD.

------------------------------------------------------------------------

# 30. Mechanical CAD Blockers

Before freezing mechanical coordinates, obtain/use:

1.  CM5 STEP
2.  Amphenol 10164227-1004A1RLF model
3.  GW16170 STEP
4.  AIW-170BQ STEP
5.  TE 2199119-6 model
6.  MC327-5 model
7.  Bel 1840888-4 model
8.  actual MHF4 connectors
9.  actual MMCX connector
10. GNSS RF connector
11. enclosure
12. enclosure boss geometry

Then perform:

-   collision check
-   connector access check
-   battery insertion/removal check
-   antenna cable bend-radius check
-   enclosure-wall clearance check
-   thermal interface check
-   mounting check

Do not freeze the MC327-5 position from guessed geometry.

------------------------------------------------------------------------

# 31. RF Architecture

## HaLow

GW16170 -\> MMCX -\> external antenna.

## Wi-Fi

AIW-170BQ -\> 2 × MHF4 -\> external antennas.

## GNSS

MAX-M10S -\> active antenna connector -\> external antenna.

### RF rules

-   short paths
-   50 Ω
-   continuous reference
-   appropriate RF ground fencing
-   no unnecessary vias
-   no long wandering traces
-   no buck inductors near RF
-   GNSS separated from Ethernet magnetics and power switching
-   RF connectors near enclosure wall

------------------------------------------------------------------------

# 32. Routing Priority

1.  PCIe
2.  Gigabit Ethernet
3.  RF
4.  GNSS RF
5.  USB 2.0
6.  CM5 clocks/high-speed control
7.  GNSS UART/PPS
8.  I2C
9.  GPIO
10. power enables/supervisor
11. telemetry
12. everything else

------------------------------------------------------------------------

# 33. PCB Stackup

Eight layers remain the starting assumption.

Conceptually:

``` text
L1  Components / critical signals / RF
L2  Solid GND
L3  High-speed signals
L4  GND
L5  Power
L6  High-speed/general signals
L7  GND
L8  General/power/low-speed
```

This is not a manufacturing stackup.

The actual stackup must come from the PCB fabricator and be used to
calculate:

-   PCIe impedance
-   Ethernet impedance
-   USB impedance
-   RF 50 Ω geometry
-   return paths
-   thermal copper

------------------------------------------------------------------------

# 34. Power Layout Rules

Battery path:

``` text
MC327-5
   |
10 mΩ shunt
   |
TVS / reverse protection / eFuse
   |
VBAT_PROTECTED
```

Keep high-current paths:

-   short
-   wide
-   low resistance
-   thermally capable

Keep Kelvin sense traces isolated from switching current.

Buck converter loops must follow TI reference layouts.

Keep switching nodes small.

Do not put inductors under RF modules.

------------------------------------------------------------------------

# 35. User Interface

The node is deliberately headless.

## No physical buttons

There will be no:

-   power button
-   reset button
-   mode button

Normal operation is EUD/software controlled.

## No user LEDs

There will be no:

-   power LED
-   activity LED
-   Ethernet LEDs
-   radio LEDs

Status belongs in the EUD/software interface.

------------------------------------------------------------------------

# 36. Hardware-Aware Mesh

The hardware should expose enough state for OpenMANET software to
understand node health and radio capability.

Useful telemetry:

-   battery voltage
-   battery current
-   battery power
-   energy/charge
-   power temperature
-   CM5/system temperature
-   radio temperature where available
-   radio power state
-   radio fault state
-   GNSS lock/state
-   GNSS PPS
-   Ethernet state
-   Ethernet timing
-   USB fault state
-   power-good
-   eFuse fault

The exact hardware-aware mesh metric/state model is primarily a software
task, but the carrier must expose the necessary hardware
telemetry/control.

------------------------------------------------------------------------

# 37. Hardware Recovery

Desired hierarchy:

1.  Reset individual USB device
2.  Reset USB hub
3.  Power-cycle individual radio
4.  Hardware supervisor/watchdog
5.  CM5 PMIC_Enable recovery

The goal is to recover from common failures without requiring physical
user interaction.

------------------------------------------------------------------------

# 38. Components Deliberately Not Selected

## External Ethernet PHY

Not selected because CM5 already contains the required Gigabit PHY.

## PoE

Not selected because there is no current requirement and it would add
significant complexity/area/thermal load.

## Physical buttons

Not selected because the product is EUD controlled and headless.

## User LEDs

Not selected because software/EUD is the status interface.

## External debug connector

Not selected because the product should remain sealed/headless; internal
test pads are sufficient.

## PCB antennas

Not selected because external antennas better support rugged enclosure
integration, field antenna selection, and RF performance.

## Separate GNSS power switch

Not selected because independent GNSS power cycling is not currently
required. A filtered 3.3 V rail is considered sufficient for V1.

## GNSS PPS hard-wired to Ethernet timing

Not selected because GNSS PPS and Ethernet PHY timing are kept as
independent timing interfaces.

## Generic GNSS RF TVS

Not selected by default because RF protection must be selected
specifically for the GNSS path.

## USB hub EEPROM

Not selected because V1 can use strap configuration.

## Hub with more than two downstream ports

Not selected because the current architecture needs exactly two internal
USB devices.

## Extra HaLow SDIO/SPI routing

Not selected because USB is the current primary interface and those
buses are not required.

------------------------------------------------------------------------

# 39. Current Status Table

  Area                         Status
  ---------------------------- ----------------------------
  Product architecture         Defined
  CM5                          Selected
  Wi-Fi                        Selected
  HaLow                        Selected
  GNSS                         Selected
  Ethernet                     Selected
  Battery connector family     Selected; geometry pending
  Shunt                        Selected
  INA228                       Selected
  Input eFuse                  Selected
  Reverse MOSFET               Candidate selected
  TVS                          Initial candidate selected
  5 V regulator                Selected
  3.3 V regulator              Selected
  Radio load switches          Selected
  Supervisor/watchdog          Selected
  USB hub                      Selected
  USB VBUS switches            Open
  USB ESD                      Open
  Ethernet ESD                 Open
  GNSS backup                  Open
  GNSS RF protection           Open
  Exact battery SKU            Open
  Enclosure                    Open
  PCB size                     117 × 67 working target
  Mechanical floorplan         Concept defined
  Exact coordinates            Open
  CAD collision check          Not completed
  PCB stackup                  Open
  Schematic architecture       Defined
  Exact schematic values       Incomplete
  PCB routing                  Not started
  Thermal validation           Not started
  RF validation                Not started
  CM5 software validation      Not started
  HaLow CM5/Linux validation   Not started
  Wi-Fi/BT validation          Not started
  Production validation        Not started

------------------------------------------------------------------------

# 40. Critical Open Items

## Mechanical

1.  Import MC327-5 CAD.
2.  Import exact CM5 CAD.
3.  Import AIW-170BQ CAD.
4.  Import GW16170 STEP.
5.  Import TE M.2 socket model.
6.  Import Bel RJ45 model.
7.  Select actual RF connectors.
8.  Define enclosure wall thickness.
9.  Define bosses.
10. Freeze mounting holes.
11. Run 3D collision analysis.
12. Verify antenna cable bend radii.
13. Verify RJ45 enclosure intrusion.
14. Verify battery latch and insertion/removal.

## Electrical

1.  Verify every CM5 GPIO mux/pin.
2.  Verify exact GW16170 M.2 control pins.
3.  Verify AIW-170BQ pinout.
4.  Calculate TPS26633 resistors.
5.  Verify reverse-FET topology.
6.  Validate TVS against actual battery/transients.
7.  Finalize LM76005 components.
8.  Select USB VBUS switches.
9.  Select USB ESD.
10. Select Ethernet ESD.
11. Finalize GNSS backup.
12. Finalize GNSS RF protection.
13. Finalize chassis/shield strategy.
14. Finalize supervisor recovery topology.
15. Verify PMIC_Enable behavior.
16. Verify startup sequencing.
17. Verify radio power sequencing.

## Software

1.  Validate CM5 PCIe Wi-Fi.
2.  Validate AIW-170BQ ath11k path.
3.  Validate Bluetooth USB.
4.  Validate GW16170 on CM5.
5.  Validate HaLow firmware.
6.  Define radio power-management software.
7.  Define hardware telemetry API.
8.  Define hardware-aware mesh metrics.
9.  Define watchdog service.
10. Define GNSS PPS handling.
11. Define Ethernet timing handling.
12. Define fault/recovery state machine.

------------------------------------------------------------------------

# 41. Validation Plan

## Power

Test:

-   minimum input
-   maximum input
-   cold startup
-   hot startup
-   max radio load
-   Ethernet load
-   USB load
-   transient load
-   radio power cycling
-   short circuit/current limit
-   reverse battery
-   overvoltage
-   undervoltage
-   thermal shutdown
-   recovery behavior

## RF

Test:

-   Wi-Fi 2.4 GHz
-   Wi-Fi 5 GHz
-   Wi-Fi 6 GHz
-   HaLow 902--928 MHz
-   GNSS acquisition/sensitivity
-   coexistence
-   antenna isolation
-   enclosure effects
-   thermal effects
-   conducted TX
-   receiver sensitivity

## Ethernet

Test:

-   10/100/1000
-   negotiation
-   throughput
-   sustained traffic
-   ESD
-   shield/chassis behavior
-   PHY timing

## USB

Test:

-   HaLow enumeration
-   Bluetooth enumeration
-   hub reset
-   device reset
-   fault/recovery
-   VBUS fault
-   simultaneous operation

## GNSS

Test:

-   cold start
-   warm start
-   multi-constellation
-   PPS
-   UART
-   I2C
-   active antenna bias
-   loss/recovery
-   backup behavior

## Battery telemetry

Compare INA228 against calibrated equipment for:

-   voltage
-   current
-   power
-   accumulated charge/energy
-   temperature

------------------------------------------------------------------------

# 42. Design Freeze Criteria

Do not call the design frozen until:

## Mechanical

-   all major STEP models loaded
-   no collisions
-   connector access verified
-   battery insertion verified
-   antenna clearance verified
-   mounting verified

## Electrical

-   every external pin verified
-   power calculations complete
-   protection calculations complete
-   regulator components selected
-   watchdog recovery bench-tested

## RF

-   actual stackup calculated
-   connectors placed
-   keep-outs defined
-   RF rules defined

## Thermal

-   major dissipation estimated
-   enclosure thermal path modeled
-   CM5 cooling verified
-   buck thermal performance estimated

## Software

-   Wi-Fi demonstrated
-   HaLow demonstrated
-   Bluetooth demonstrated
-   GNSS demonstrated
-   telemetry demonstrated

------------------------------------------------------------------------

# 43. Immediate Next Steps

## Step 1 --- Mechanical CAD assembly

Build a 117 × 67 mm board assembly containing:

-   CM5
-   CM5 connectors
-   GW16170
-   AIW-170BQ
-   M.2 sockets
-   MC327-5
-   Bel 1840888-4
-   RF connectors
-   major power components
-   TUSB4020BI
-   enclosure walls
-   four M3 bosses

Run collision/clearance analysis.

### Success condition

Everything fits with:

-   enclosure clearance
-   connector access
-   antenna access
-   battery insertion/removal
-   thermal paths
-   mounting access

without enlarging the board.

## Step 2 --- Freeze mechanical reference coordinates

Freeze:

-   PCB outline
-   mounting holes
-   CM5
-   M.2 sockets
-   RJ45
-   MC327-5
-   RF connectors
-   enclosure interface

## Step 3 --- Finish electrical values

Complete:

-   TPS26633 resistors
-   LM76005 components
-   load-switch settings
-   watchdog timing
-   USB VBUS switches
-   USB ESD
-   Ethernet ESD
-   GNSS backup
-   RF protection
-   chassis strategy

## Step 4 --- Verify authoritative pinouts

Verify:

-   CM5 GPIO
-   CM5 PCIe
-   CM5 USB
-   AIW-170BQ M.2
-   GW16170 M.2
-   TE socket
-   MC327-5

No inferred pinout should reach PCB layout.

## Step 5 --- Select actual PCB stackup

Obtain fabricator stackup and calculate:

-   PCIe
-   Ethernet
-   USB
-   50 Ω RF
-   return paths
-   thermal copper

## Step 6 --- Route

Use priority:

1.  PCIe
2.  Ethernet
3.  RF
4.  GNSS RF
5.  USB
6.  high-speed/control
7.  UART/PPS
8.  I2C
9.  GPIO
10. power/supervisor
11. telemetry

------------------------------------------------------------------------

# 44. Current Engineering Conclusion

The project has moved beyond basic architecture selection.

The current V1 architecture is:

-   Raspberry Pi CM5
-   Advantech AIW-170BQ Wi-Fi 6E
-   Gateworks GW16170 HaLow
-   TUSB4020BI USB hub
-   u-blox MAX-M10S-00B GNSS
-   CM5 integrated Gigabit Ethernet PHY
-   Bel/TRP 1840888-4
-   Mil-Con MC327-5
-   INA228
-   10 mΩ Kelvin shunt
-   TPS26633
-   CSD19533Q5A candidate
-   SMBJ33A initial TVS
-   LM76005 5 V
-   LM76005 3.3 V
-   TPS22975 ×2
-   TPS386000

The remaining work is primarily:

-   exact implementation
-   verification
-   mechanical CAD
-   power calculations
-   RF/layout
-   enclosure integration
-   software validation

The next meaningful step is **not another architecture brainstorm**.

It is to build the actual CAD assembly and resolve the mechanical design
using manufacturer models.

------------------------------------------------------------------------

# 45. Rules for Future Agents

1.  Do not restart the architecture from scratch.
2.  Do not substitute parts casually.
3.  Do not guess connector pinouts.
4.  Do not guess MC327-5 geometry.
5.  Do not assume generic M.2 pin behavior applies to GW16170.
6.  Current AIW-170BQ design uses Bluetooth USB, not UART.
7.  Do not add an external Ethernet PHY.
8.  Do not add PoE unless requirements change.
9.  Do not add physical buttons.
10. Do not add user LEDs.
11. Do not enlarge the PCB until CAD proves it necessary.
12. Do not route switching inductors beneath RF modules.
13. Do not blindly populate GNSS RF TVS.
14. Do not use CM5 `nEXTRST` as the external reset strategy.
15. Use `PMIC_Enable` for the intended hardware recovery path.
16. Keep USB VBUS switching separate from radio 3.3 V power switching.
17. Use manufacturer STEP models wherever possible.
18. Treat the enclosure as part of the thermal and RF design.
19. Prefer authoritative manufacturer documentation.
20. Verify critical assumptions against the actual selected part
    revision before freeze.

------------------------------------------------------------------------

# 46. Exact Resume Point

**Start here:**

> Import actual manufacturer STEP models for CM5, GW16170, AIW-170BQ, TE
> 2199119-6, MC327-5, Bel 1840888-4, RF connectors, and the intended
> enclosure/boss geometry. Build the 117 × 67 mm assembly, resolve
> collisions and enclosure clearances, and produce a final mechanical
> coordinate table.

Then:

> Complete exact power-component calculations and the remaining
> USB/ESD/GNSS/chassis decisions.

Then:

> Verify every CM5/GW16170/AIW-170BQ/M.2/battery pin against
> authoritative documentation.

Then:

> Freeze schematic → freeze stackup → route PCB → DRC/3D clearance →
> prototype → bench validation.

------------------------------------------------------------------------

# 47. Current BOM Summary

  -----------------------------------------------------------------------------------
  Function          Current part         Status            Why
  ----------------- -------------------- ----------------- --------------------------
  Compute           Raspberry Pi         Selected          Compute, eMMC, PCIe, USB,
                    CM5008032                              integrated Ethernet

  CM5 connector     Amphenol             Selected          4 mm stack / 2.5 mm
                    10164227-1004A1RLF                     underside clearance

  Wi-Fi             Advantech            Selected          Wi-Fi 6E, PCIe, USB BT,
                    AIW-170BQ-001                          MHF4

  HaLow             Gateworks GW16170 /  Selected          900 MHz HaLow, USB, M.2,
                    MM8108-M20                             MMCX

  GNSS              u-blox MAX-M10S-00B  Selected          compact,
                                                           multi-constellation,
                                                           UART/I2C/PPS

  Ethernet          Bel/TRP 1840888-4    Selected          1G integrated magnetics,
                                                           no LED, no PoE

  M.2 socket        TE 2199119-6         Selected          E-Key socket
                                         reference         

  Battery connector Mil-Con MC327-5      Selected          field battery interface

  Shunt             Vishay               Selected          10 mΩ Kelvin
                    WFK0612R0100FE66                       

  Battery monitor   TI INA228AIDGSR      Selected          high-resolution power
                                                           telemetry

  Input eFuse       TI TPS26633RGER      Selected          wide
                                                           input/protection/current
                                                           limiting

  Reverse FET       TI CSD19533Q5A       Candidate         high-voltage external FET

  TVS               Diodes Inc. SMBJ33A  Initial candidate battery transient
                                                           suppression

  5 V buck          TI LM76005           Selected          wide input/high current

  3.3 V buck        TI LM76005           Selected          common regulator family

  Radio switches    TI TPS22975DSGT ×2   Selected          independent radio power
                                                           control

  Supervisor        TI TPS386000RGPR     Selected          rail supervision/watchdog

  USB hub           TI TUSB4020BI        Selected          exactly two USB2 devices

  USB VBUS switch   TBD                  Open              dedicated USB power
                                                           control

  USB ESD           TBD                  Open              low-capacitance USB2
                                                           protection

  Ethernet ESD      TBD                  Open              Gigabit-capable protection

  GNSS backup       TBD                  Open              V_BCKP strategy

  GNSS RF           TBD                  Open              frequency-specific
  protection                                               selection

  Enclosure         TBD                  Open              CAD/thermal/mechanical
                                                           work required
  -----------------------------------------------------------------------------------

------------------------------------------------------------------------

# 48. Final One-Paragraph Handoff

