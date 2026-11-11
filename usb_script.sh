#!/bin/sh
# usb_script.sh — executed by update.sh at rcS boot time via sh (no execute bit needed on FAT32)
# $1 = mount path of USB stick (e.g. /mnt/mass_storage/U-disk0)
#
# This runs AFTER update.sh has:
#   - killed rcS (so no reboot -f)
#   - replicated normal-boot steps (eth0, devmem, GPIO, mdev, FPGA, drivers, app, VNC, lighttpd)
#
# Put your custom boot logic here. This runs on every boot with the USB stick inserted.
# Keep it idempotent — it may run multiple times.

USB_MP="$1"
# --- check the mount path actually exists ---
if [ -z "$USB_MP" ] || [ ! -d "$USB_MP" ]; then
    exit 1
fi

# --- restart telnetd after it's killed ---
(
while pidof telnetd > /dev/null 2>&1; do
    sleep 1
done
/bin/busybox telnetd -l /bin/sh > /dev/null 2>&1 &
) &

# --- restart web interface ---
/usr/sbin/lighttpd -f /etc/lighttpd.conf > /dev/null 2>&1 &

# --- add your code here ---



# --- forces file save ---
sync 2>&1

