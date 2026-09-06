# KY-EDL

Kyocera has implemented vendor commands in EDL, let's use them!

> [!WARNING]
> The vendor SAHARA commands are implemented inside SBL1, as such erasing it **will render this tool useless.**

> [!WARNING]
> DO NOT ERASE RANDOM PARTITIONS. DO NOT DISCONNECT THE DEVICE WHEN A FLASHING PROCEDURE STARTED.

## What this tool can do:

- Device Info: Read SecureBoot status and print the GPT.
- Dump: Pull specific partitions or create full eMMC dump.
- Flash: Flash specific partitions or flash a full eMMC backup.
- Partition integrity verification: Compare local file checksums against hardware-calculated partition checksums.
- Sector peek: Read an arbitrary byte count from the start of a raw eMMC sector, without a full-sector aligned read.
- Hardware operations: Clear write protection, run region erasures, and issue hardware resets.

---

## Prerequisites

Ensure you have Python 3.8+ installed along with `libusb` dependencies required by `pyusb` / `usblib`.

Generally, in Linux:

```shell
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install pyusb usblib
```

## Entering RAMDUMP mode:

To enter RAMDUMP mode, you have 2 methods:

1. Create a FAT/FAT32 formatted SD Card with a file named `NOTPUSH`, with the contents `DEVKEYDL` inside it. This SD card trigger is _persistent RAMDUMP mode_ (i.e across reboots)

> [!NOTE]
> On Linux/MacOS, you can run `echo "DEVKEYDL" > "NOTPUSH"` in the root directory of the SD card.

2. use a deepflash cable.

It will connect with a device `KYOCERA_Android Android` and the notification LED will be green (if LED exists).

### To exit from RAMDUMP, remove the SD card and reboot the phone.

# Usage:

1. View GPT mapping and secureboot status:

   `python ky-edl.py info`

2. Dump entire eMMC:

   `python ky-edl.py dump --full -o full_emmc.img`

3. Dump a single partition:

   `python ky-edl.py dump -p system -o system.img`

4. Flash a single partition:

   `python ky-edl.py flash -p system -i system.img`

5. Flash an entire raw eMMC image:

   `python ky-edl.py flash --full -i full_emmc.img`

6. Peek at the first N bytes of a raw sector:

   `python ky-edl.py peek --sector-lba 1 --count 32`

> [!NOTE]
> Your device may have a different VID/PID, and may not be detected. For such cases use `lsusb -v` to check your specific IDs.
> Default: VID is `0482` and PID is `0a7f`

---

# Devices

Devices known to work with this tool:

- [KYF31](https://garahowiki.com/phones:kyocera_gratina_4g)
- [KYF42](https://garahowiki.com/phones:kyocera_gratina_kyf42)
- [902KC/903KC](https://garahowiki.com/phones:kyocera_digno_keitai_3:start)
- [E4810](https://garahowiki.com/phones:kyocera_duraxv_extreme)

This tool is possible on a majority of _Qualcomm-based_ Kyocera devices.

---

# Credits

Original reverse engineering efforts, research and code by [@leobuskin](https://github.com/leobuskin) & [@ars18/@alltechdev](https://github.com/alltechdev) [link](https://forums.jtechforums.org/t/how-to-unlock-the-bootloader-an-root-kyocera-e4810-only/6167/121).

This project uses code derived from or inspired by [bkerler/edl](https://github.com/bkerler/edl) by Bjoern Kerler, and as such is also licensed under GPLv3.

This repository is based on a fork-clone of [sonic011gamer/kedl](https://github.com/sonic011gamer/kedl)
