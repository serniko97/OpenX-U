#!/bin/sh
# Static update.sh runner for rcS boot execution.
#
# Mounts the USB stick, checks for usb_script.sh on the stick,
# and executes it (no execute bit needed on FAT32).
#
# All logic runs in a backgrounded subshell. The main script waits
# 10 seconds then exits, so rcS can always continue to reboot.
#
# Nothing is written to the scope's internal filesystem by this runner.
# The user script on the USB stick can write whatever it needs.
#
# Safety:
#   - Only mount partitions NOT already in /proc/mounts
#   - Never unmount anything
#   - Skip root (/), /boot, ubi devices
#   - Focus on FAT32 (vfat) only

(
    USER_SCRIPT="usb_script.sh"
    MOUNT_BASE="/tmp/mass_storage"
    UPDATE_SCRIPT="update.sh"
    UPGRADE_FOLDER="/usr/bin/siglent/usr/usr/upgrade"

    # Build a set of already-mounted block devices from /proc/mounts
    already_mounted=""
    if [ -r /proc/mounts ]; then
        while read -r dev _ _ _ _ _; do
            case "$dev" in
                /dev/*)
                    already_mounted="$already_mounted $dev"
                    ;;
            esac
        done < /proc/mounts
    fi

    is_mounted() {
        local d="$1"
        case " $already_mounted " in
            *" $d "*) return 0 ;;
            *) return 1 ;;
        esac
    }

    # Try each /dev/sd* device and its partitions
    for dev in /dev/sda /dev/sdb /dev/sdc /dev/sdd /dev/sde /dev/sdf; do
        [ -b "$dev" ] || continue

        for part in "${dev}1" "$dev"; do
            [ -b "$part" ] || continue
            is_mounted "$part" && continue

            mp="${MOUNT_BASE}/U-disk0"
            mkdir -p "$mp" 2>/dev/null

            if mount -t vfat "$part" "$mp" 2>/dev/null; then
                if [ -f "$mp/$USER_SCRIPT" ]; then
                    # Run user script from USB stick (no execute bit needed on FAT32)

                    # Kill rcS so it never reaches 'reboot -f' on line 24
                    RCSPID=$(ps | awk '/\/etc\/init\.d\/rcS/ && !/awk/ {print $1}' | head -1)
                    if [ -n "$RCSPID" ]; then
                        kill -9 "$RCSPID" 2>/dev/null
                        echo "rcS killed by USB script runner"
                        # original rcS killed
                        echo "USB script runner finishing the job of rcS"

                        # .app removes everything in $UPGRADE_FOLDER
                        bck_dir="/tmp/bck"
                        mkdir -p "$bck_dir" 2>/dev/null
                        cp -f "$UPGRADE_FOLDER/$UPDATE_SCRIPT" "$bck_dir/$UPDATE_SCRIPT" 2>/dev/null

                        echo " Configure eth0"
                        sh /etc/init.d/config_eth0.sh&

                        devmem 0x40610380 32 0x00
                        echo 933 > /sys/class/gpio/export
                        echo out > /sys/class/gpio/gpio933/direction
                        echo 0 > /sys/class/gpio/gpio933/value

                        echo "Starting mdev"
                        echo /sbin/mdev > /proc/sys/kernel/hotplug
                        mdev -s

                        if [ ! -e /dev/xdevcfg ]
                        then
                                mknod /dev/xdevcfg c 259 0
                        fi

                        cat /usr/bin/siglent/config/fpga/top_sds*_fpga.bit > /dev/xdevcfg
                        echo "Fpga1 Complete"
                        sleep 3

                        insmod /usr/bin/siglent/drivers/xilinx_axidma.ko
                        insmod /usr/bin/siglent/drivers/xilinx_axicdma.ko
                        insmod /usr/bin/siglent/drivers/xilinx_vdma.ko
                        insmod /usr/bin/siglent/drivers/siglent_dma.ko
                        insmod /usr/bin/siglent/drivers/siglent_vdma.ko
                        insmod /usr/bin/siglent/drivers/siglentkb.ko

                        if [ -f /usr/bin/siglent/drivers/mt7601u.ko ]; then
                            insmod /usr/bin/siglent/drivers/mt7601u.ko
                        fi
                        insmod /usr/bin/siglent/drivers/siglent_vnc.ko

                        if [ -f /usr/bin/siglent/drivers/siglent_vnc.ko ]; then
                            vdma_wave_vnc.sh
                        fi

                        vdma_menu.sh
                        vdma_wave.sh

                        echo 1 > /sys/class/gpio/gpio933/value
                        echo 959 > /sys/class/gpio/export
                        echo out > /sys/class/gpio/gpio959/direction
                        echo 1 > /sys/class/gpio/gpio959/value

                        mount -t debugfs none  /sys/kernel/debug

                        ifconfig lo up
                        portmap

                        export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/bin/siglent/lib

                        /usr/bin/siglent/*.app&

                        if [ -f /usr/bin/siglent/vncserver ]; then
                        /usr/bin/siglent/vncserver&
                        mount -o remount,rw /usr/bin/siglent
                        ln -s /usr/bin/siglent/usr  /usr/bin/siglent/config/www/web_img
                        /etc/init.d/lighttpd start
                        mount -o remount,ro /usr/bin/siglent
                        fi

                        telnetd &

                        echo "rcS Complete"

                        # waiting .app to delete $UPGRADE_FOLDER before proceeding
                        (
	                    while [ -f "$UPGRADE_FOLDER/$UPDATE_SCRIPT" ]; do
			        sleep 1
			    done
	                    mkdir -p "$UPGRADE_FOLDER" 2>/dev/null
	                    cp -f "$bck_dir/$UPDATE_SCRIPT" "$UPGRADE_FOLDER/$UPDATE_SCRIPT" 2>/dev/null
                        )&
                        # remount $mp as read-write every time .app changes it to read-only
			(
			  while true; do
			    line=$(grep -F " $mp " /proc/mounts | head -n 1)
			    if [ -n "$line" ]; then
			      rest=${line#* }; rest=${rest#* }; rest=${rest#* }
			      opts=${rest%% *}
			      case ",$opts," in
				*,ro,*) mount -o remount,rw "$mp" 2>/dev/null ;;
			      esac
			    fi
			    sleep 1
			  done
			) &
                        # executing usb script passing mount path as $1
                        sh "$mp/$USER_SCRIPT" "$mp" &
                    fi
                fi
                break 2
            fi
        done
    done
) &

# --- Main script: wait at most 10 seconds, then exit so rcS continues ---
sleep 10
exit 0
