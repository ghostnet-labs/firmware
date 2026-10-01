#!/bin/sh
# Download the manufacturer CAD that is publicly available without a login
# into step/. Vendor files are not committed; see README for the rest.
set -eu
cd "$(dirname "$0")/step"

# Raspberry Pi CM5 official 3D STEP (Product Information Portal, design files).
curl -fsSL -o cm5-step.zip \
  "https://pip-assets.raspberrypi.com/categories/1096-design-files/documents/RP-007222-DD-2-rpi-cm5%203D_STEP.zip"
unzip -o -q cm5-step.zip CM5R5-3dsteps.step
mv CM5R5-3dsteps.step cm5.step
rm cm5-step.zip

# Bel/TRP 1840888-4 customer drawing (2D PDF; the STEP needs a Bel account).
curl -fsSL -A "Mozilla/5.0" -o bel_1840888-4_drawing.pdf \
  "https://www.belfuse.com/media/drawings/products/magjack%20ICMs/dr-MAG-1840888-4.pdf"

ls -l
