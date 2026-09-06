#!/usr/bin/env python3
import argparse
import binascii
import logging
import mmap
import os
import struct
import time
from binascii import hexlify
from io import BytesIO
from pathlib import Path
from typing import Optional

from edl.config.qualcomm_config import msmids
from edl.sahara import (
    CmdHeader,
    ErrorDesc,
    ExecuteResponse,
    HelloRequest,
    ImageEnd,
    SaharaCmd,
    SaharaExecCmd,
    SaharaMode,
    SaharaPacket,
)
from edl.usblib import usb_class
from edl.utils import structhelper_io

logger = logging.getLogger("ky-edl")


class DataError(Exception): ...


class CommandHandler:

    @staticmethod
    def parse_hello_req(data):
        if len(data) < 0xC * 0x4:
            raise DataError
        st = structhelper_io(BytesIO(data))
        return HelloRequest(
            cmd=SaharaCmd(st.dword()),
            len=st.dword(),
            version=st.dword(),
            version_supported=st.dword(),
            cmd_packet_length=st.dword(),
            mode=st.dword(),  # type: ignore ; IntEnum
            reserved1=st.dword(),
            reserved2=st.dword(),
            reserved3=st.dword(),
            reserved4=st.dword(),
            reserved5=st.dword(),
            reserved6=st.dword(),
        )

    @staticmethod
    def parse_execute_rsp(data):
        if len(data) < 0x4 * 0x4:
            raise DataError
        st = structhelper_io(BytesIO(data))
        return ExecuteResponse(
            cmd=SaharaCmd(st.dword()),
            len=st.dword(),
            client_cmd=st.dword(),
            data_len=st.dword(),
        )

    @staticmethod
    def parse_image_end(data):
        if len(data) < 0x4 * 0x4:
            raise DataError
        st = structhelper_io(BytesIO(data))
        return ImageEnd(
            cmd=SaharaCmd(st.dword()),
            len=st.dword(),
            image_id=st.dword(),
            image_tx_status=st.dword(),
        )

    def parse_pkt(self, data):
        if len(data) < 2 * 4:
            raise DataError
        st = structhelper_io(BytesIO(data))
        cmd = SaharaCmd(st.dword())
        if cmd == SaharaCmd.HELLO_REQ:
            return self.parse_hello_req(data)
        if cmd == SaharaCmd.EXECUTE_RSP:
            return self.parse_execute_rsp(data)
        if cmd == SaharaCmd.END_TRANSFER:
            return self.parse_image_end(data)
        return CmdHeader(cmd=cmd, len=st.dword())


class Sahara:
    def __init__(self, cdc: usb_class) -> None:
        self._cdc = cdc
        self._ch = CommandHandler()
        self._version = 2
        self._pkt_size = None
        self._serial = None

    def connect(self) -> bool:
        v = self._cdc.read(length=0xC * 0x4, timeout=1)
        if len(v) > 1:
            pkt = self._ch.parse_pkt(v)
            if isinstance(pkt, HelloRequest) and (pkt.cmd == SaharaCmd.HELLO_REQ):
                self._pkt_size = pkt.cmd_packet_length
                self._version = pkt.version
                logger.debug(f"RX: {pkt}")
                if self.cmd_info(self._version):
                    logger.info(f"Serial Number: {self._serial}")
                    return True
                return False
            else:
                logger.info(f"Unsupported pkt on connect: {pkt}")
                return False
        else:
            logger.info(f"Not enough data on connect: {v}")
            return False

    def disconnect(self) -> None:
        if self.cmd_reset():
            logger.info("Sahara was successfully reset")
        else:
            logger.info("Sahara failed to reset")

    def cmd_hello(
        self,
        mode: SaharaMode,
        version_min: int = 1,
        max_cmd_len: int = 0,
        version: int = 2,
    ):
        """CMD 0x1, RSP 0x2"""
        cmd = SaharaCmd.HELLO_RSP
        length = 0x30
        payload = struct.pack(
            "<IIIIIIIIIIII",
            cmd,
            length,
            version,
            version_min,
            max_cmd_len,
            mode,
            1,
            2,
            3,
            4,
            5,
            6,
        )
        try:
            self._cdc.write(payload)
        except Exception as e:
            logger.error(str(e))
            return False
        return True

    def cmd_reset(self):
        try:
            self._cdc.write(struct.pack("<II", SaharaCmd.RESET_REQ, 0x8))
            logger.info("Reset command sent. Device is rebooting...")

            time.sleep(0.1)
            return True
        except Exception as e:
            logger.debug(f"USB connection severed during reset as expected: {e}")
            return True

    def get_rsp(self):
        try:
            data = self._cdc.read()
            if data == b"":
                return None
            return self._ch.parse_pkt(data)
        except Exception as e:
            logger.error(str(e))
            return None

    def enter_command_mode(self, version=2) -> bool:
        if not self.cmd_hello(SaharaMode.COMMAND, version=version):
            return False
        res = self.get_rsp()
        if res:
            if res.cmd == SaharaCmd.CMD_READY:
                return True
            logger.error(f"Unsupported command mode response: {res}")
        else:
            logger.warning(f"Empty command mode response: {res}")
        return False

    @staticmethod
    def get_error_desc(status):
        if status in ErrorDesc:
            return f"Error: {ErrorDesc[status]}"
        return "Unknown error"

    def cmd_exec(self, mcmd: SaharaExecCmd):  # CMD 0xD, RSP 0xE, CMD2 0xF
        # Send request
        data = struct.pack("<III", SaharaCmd.EXECUTE_REQ, 0xC, mcmd)
        self._cdc.write(data)
        # Get info about request
        res = self.get_rsp()
        if res is None:
            logger.warning(f"Empty command exec response: {res}")
            return res

        if (isinstance(res, ExecuteResponse)) and (res.cmd == SaharaCmd.EXECUTE_RSP):
            data = struct.pack("<III", SaharaCmd.EXECUTE_DATA, 0xC, mcmd)
            self._cdc.write(data)
            payload = self._cdc.usbread(res.data_len)
            return payload
        elif (isinstance(res, ImageEnd)) and (res.cmd == SaharaCmd.END_TRANSFER):
            logger.error(self.get_error_desc(res.image_tx_status))
        else:
            logger.warning(f"Unsupported command exec response: {res}")

        return res

    def cmdexec_get_serial_num(self) -> int:
        res: bytes = self.cmd_exec(SaharaExecCmd.SERIAL_NUM_READ)  # type: ignore

        logger.info(f"Serial Number (bytes): {res} [{hexlify(res)}]")
        return int.from_bytes(res, "little")

    def cmd_modeswitch(self, mode):
        data = struct.pack("<III", SaharaCmd.SWITCH_MODE, 0xC, mode)
        self._cdc.write(data)

    def cmd_info(self, version) -> bool:
        if self.enter_command_mode(version=version):
            self._serial = self.cmdexec_get_serial_num()
            return True
        return False


class KyoceraSahara(Sahara):

    # based on SBL1 analysis
    VendorErrorDesc = {
        0x0A: "Response send failed: length mismatch",
        0x0C: "Response send failed: general transmit error",
        0x17: "Response send failed: timeout/busy",
        0x50: "Storage driver failed to initialize",
        0x51: "Low-level read call failed (bad sector count, or driver rejected a zero-length read)",
        0x52: "Low-level write call failed (bad sector count, or driver rejected a zero-length write)",
        0x54: "Requested byte/sector count too large for this command",
        0x57: "0xAF: requested length too large (>= 0xfec)",
        0x58: "0xAF: info-id lookup failed (unpopulated/invalid id, or id data unavailable)",
        0x59: "0xB1: requested length too large (>= 0xfec)",
        0x5A: "0xB1: info-id write call failed",
    }

    @classmethod
    def get_vendor_error_desc(cls, status: int) -> str:
        if status in cls.VendorErrorDesc:
            return f"Vendor error: {cls.VendorErrorDesc[status]}"
        if status in ErrorDesc:
            return f"Error: {ErrorDesc[status]}"
        return f"Unknown vendor status: 0x{status:02X}"

    def drain_in(
        self, location: str, count: int = 0, attempts: int = 1, timeout: int = 1
    ) -> Optional[bytes]:
        buffer = bytearray()
        length = min(self._cdc.maxsize, count or self._cdc.maxsize)
        for _ in range(attempts):
            try:
                buf = bytes(self._cdc.EP_IN.read(length, timeout))  # type: ignore
            except Exception as e:
                error = str(getattr(e, "strerror", e))
                if "timed out" not in error:
                    if "Overflow" in error:
                        logger.error(f"{location} drain_in USB overflow: {error}")
                    else:
                        logger.debug(error)
                    break
                logger.debug(error)
            else:
                buffer.extend(buf)
        response = bytes(buffer)
        if response:
            logger.warning(f"{location} drainage [{len(response)}]: {response}")
            return response
        return None

    def vendor_cmd_ab_clear_write_protect(self, lba: int, byte_count: int) -> bool:
        logger.debug(
            f"vendor_cmd_ab_clear_write_protect: clearing write-protect for {byte_count} bytes from lba:{lba}"
        )
        self.drain_in("pre")
        data = struct.pack("<IIII", 0xAB, 16, lba, byte_count)
        if not self._cdc.write(data):
            logger.error(f"failed to send data")
            return False
        resp = self._cdc.read(16)
        if len(resp) == 16:
            cmd_id = struct.unpack("<I", resp[0:4])[0]
            if cmd_id == 0xAC:
                logger.info("Write-protect cleared")
                return True
            elif cmd_id == 0x04:
                status = struct.unpack("<I", resp[12:16])[0]
                logger.error(
                    f"clear_write_protect error: {self.get_vendor_error_desc(status)}"
                )
                return False
            else:
                logger.warning(f"Unsupported response on clear_write_protect: {resp}")
                return False
        else:
            logger.error(f"error: {resp}")
        self.drain_in("post")
        return False

    A7_CHUNK_BYTES = 8 * 1024 * 1024
    A7_MS_PER_MB = 1500

    def vendor_cmd_a7_checksum(self, lba: int, byte_count: int) -> Optional[int]:
        logger.debug(
            f"vendor_cmd_a7_checksum: verifying {byte_count} bytes from lba:{lba}"
        )
        self.drain_in("pre")
        result = 0
        remaining = byte_count
        current_lba = lba
        while remaining > 0:
            chunk = min(remaining, self.A7_CHUNK_BYTES, 0xFFFFFFFF)
            chunk -= chunk % 512
            if chunk == 0:
                chunk = 512
            self.drain_in("process")
            data = struct.pack("<IIII", 0xA7, 16, current_lba, chunk)
            if not self._cdc.write(data):
                logger.error(f"failed to send data")
                return None
            rsp = self._cdc.read(
                20,
                timeout=max(
                    3000,
                    min(60000, int(1000 + (chunk / (1024 * 1024)) * self.A7_MS_PER_MB)),
                ),
            )
            if len(rsp) == 20:
                rid, rlen, rsp_lba, rsp_bytes, rsp_sum = struct.unpack("<IIIII", rsp)
                if rid == 0xA8:
                    result = (result + rsp_sum) & 0xFFFFFFFF
                else:
                    logger.error(f"invalid response id: {rid} [{rsp}]")
                    return None
            elif len(rsp) == 16:
                status = struct.unpack("<I", rsp[12:16])[0]
                logger.error(f"checksum error: {self.get_vendor_error_desc(status)}")
                return None
            else:
                logger.error(f"invalid response length: {len(rsp)}")
                return None
            sectors_advanced = (chunk + 511) // 512
            current_lba += sectors_advanced
            remaining -= sectors_advanced * 512
        self.drain_in("post")
        return result

    def vendor_cmd_b5_read_secureboot(self) -> Optional[bool]:
        logger.debug("vendor_cmd_b5_read_secureboot: reading secureboot")
        self.drain_in("pre")
        data = struct.pack("<II", 0xB5, 8)
        if not self._cdc.write(data):
            logger.error(f"failed to send data")
            return False
        resp = self._cdc.read(12)
        if len(resp) == 12:
            rid, rlen, sb_status = struct.unpack("<III", resp)
            if rid == 0xB6:
                return sb_status == 1
            else:
                logger.error(f"error: {resp}")
                return None
        logger.error(f"error: {resp}")
        return None

    def vendor_cmd_b9_read_raw(self, lba: int, sectors: int) -> Optional[bytes]:
        logger.debug(
            f"vendor_cmd_b9_read_raw: reading raw {sectors} sectors from lba:{lba}"
        )
        self.drain_in("pre")
        data = struct.pack("<IIII", 0xB9, 16, lba, sectors)
        if not self._cdc.write(data):
            logger.error(f"failed to send data")
            return None
        total = sectors * 512
        first = True
        buf = bytearray()
        while len(buf) < total:
            tmp = self._cdc.read(min(total - len(buf), 1024 * 1024))
            if not tmp:
                logger.error(
                    "read_raw: no data received, aborting to avoid a stuck loop"
                )
                return None
            if first and len(tmp) == 16:
                pkt = self._ch.parse_pkt(tmp)
                if pkt.cmd == SaharaCmd.END_TRANSFER:
                    logger.error(f"read_raw error: {pkt}")
                    return None
            first = False
            buf.extend(tmp)
        post = self.drain_in("post", 0)
        if post:
            if len(post) == 16:
                pkt = self._ch.parse_pkt(post)
                if pkt.cmd == SaharaCmd.END_TRANSFER:
                    logger.error(f"read_raw error: {pkt}")
                    return None
                else:
                    logger.warning(f"Unsupported response on read_raw: {pkt}")
                    return None
        return bytes(buf)

    def vendor_cmd_a5_erase_region(self, lba: int, sectors: int) -> bool:
        logger.debug(
            f"vendor_cmd_a5_erase_region: erasing {sectors} sectors from lba:{lba}"
        )
        end_lba = int(lba) + int(sectors) - 1
        if end_lba < int(lba):
            logger.error("vendor_cmd_a5_erase_region: computed end_lba < start_lba")
            return False
        self.drain_in("pre")
        data = struct.pack("<IIII", 0xA5, 16, lba, sectors)
        if not self._cdc.write(data):
            logger.error(f"failed to send data")
            return False
        deadline = time.time() + 60.0
        while True:
            if time.time() >= deadline:
                logger.error("Timed out waiting for 0xA6 after 0xA5")
                return False
            hdr = self._cdc.read(16)
            rid, rlen, p2, p3 = struct.unpack("<IIII", hdr)
            code = rid & 0xFF
            logger.debug(f"[debug] 0xA5 rsp: id=0x{rid:x} len={rlen} p2={p2} p3={p3}")
            if code == 0xA6 and rlen == 16:
                break
            if code == 0x04 and rlen == 16:
                status = p3
                if status != 0:
                    logger.error(
                        f"Device reported error on 0xA5: {self.get_vendor_error_desc(status)}"
                    )
                    return False
                continue
        return True

    BA_CHUNK_SIZE = 1048576

    def vendor_cmd_ba_write_raw(self, lba: int, data: bytes) -> bool:
        if len(data) % 512 != 0:
            logger.error("vendor_cmd_ba_write_raw: data length must be multiple of 512")
            return False
        sectors = len(data) // 512
        logger.debug(
            f"vendor_cmd_ba_write_raw: writing raw {len(data)} bytes ({sectors} sectors) to lba:{lba}"
        )

        self.drain_in("pre")
        payload = struct.pack("<IIII", 0xBA, 16, lba, sectors)
        if not self._cdc.write(payload):
            logger.error(f"failed to send data")
            return False

        try:
            resp = self._cdc.read(16)
        except Exception as e:
            logger.error(str(e))
            return False
        else:
            if len(resp) == 16:
                cmd_id, rlen, p2, p3 = struct.unpack("<IIII", resp)
                if cmd_id == 0x04 and p3 != 0x00:
                    logger.error(f"write_ba error: {self.get_vendor_error_desc(p3)}")
                    return False
                logger.info(
                    f"0xBA ready hdr: id=0x{cmd_id:x} len={rlen} p2=0x{p2:x} p3=0x{p3:x}"
                )

        sent = 0
        total = len(data)
        while sent < total:
            chunk_size = min(total - sent, self.BA_CHUNK_SIZE)
            actual = self._cdc.EP_OUT.write(data[sent : sent + chunk_size])  # type: ignore
            if actual == chunk_size and chunk_size % self._cdc.EP_OUT.wMaxPacketSize == 0:  # type: ignore
                self._cdc.EP_OUT.write(b"")  # type: ignore
            sent += actual
            logger.debug(f"Sent {sent}/{total} bytes")

        try:
            resp = self._cdc.read(timeout=6000)
        except Exception as e:
            logger.error(str(e))
            return False
        else:
            if len(resp) == 16:
                cmd_id, rlen, p2, p3 = struct.unpack("<IIII", resp)
                if cmd_id == 0x04:
                    logger.error(f"write_ba error: {self.get_vendor_error_desc(p3)}")
                    return False
                logger.info(
                    f"0xBB ready hdr: id=0x{cmd_id:x} len={rlen} p2=0x{p2:x} p3=0x{p3:x}"
                )
        return True

    def vendor_cmd_a3_peek_sector(self, lba: int, count: int) -> Optional[bytes]:
        """Read the first `count` bytes of eMMC sector
        `lba`, starting at byte offset 0.

        Unlike 0xB9 (full sector reads),
        this returns an arbitrary byte count without full-sector alignment"""
        logger.debug(f"vendor_cmd_a3_peek_sector: peeking {count} bytes from lba:{lba}")
        self.drain_in("pre")
        data = struct.pack("<IIII", 0xA3, 16, lba, count)
        if not self._cdc.write(data):
            logger.error("failed to send data")
            return None
        try:
            resp = self._cdc.read(16 + count, timeout=max(1000, 200 + count))
        except Exception as e:
            logger.error(str(e))
            return None
        extra = self.drain_in("post", 0)
        if extra:
            resp = resp + extra
        if len(resp) < 16:
            logger.error(
                f"peek_sector error: short response {resp.hex() if resp else resp}"
            )
            return None
        rid, _, _, p3 = struct.unpack_from("<IIII", resp, 0)
        if rid == 0x04:
            logger.error(f"peek_sector rejected: {self.get_vendor_error_desc(p3)}")
            return None
        if rid != 0xA4:
            logger.warning(
                f"peek_sector: unexpected response id 0x{rid:X}: {resp.hex()}"
            )
            return None
        body = resp[16 : 16 + count]
        if len(body) < count:
            logger.error(
                f"peek_sector: truncated body, got {len(body)} of {count} bytes"
            )
            return None
        return body


class Qdl:

    # Max sectors as per raw 0xB9 (read) command.
    PARTITION_READ_CHUNK_SECTORS = 2048

    # Max sectors as per raw 0xBA (write) command.
    PARTITION_WRITE_CHUNK_SECTORS = 2048

    def __init__(self, vid: int, pid: int) -> None:
        self._vid = vid
        self._pid = pid
        self._cdc = None
        self._sahara = None
        self._parts = {}
        self._total_sectors = None

    @property
    def partitions(self) -> dict:
        return dict(self._parts)

    def connect(self) -> bool:
        try:
            self._cdc = usb_class([[self._vid, self._pid, -1]])
            if self._cdc.connect(-1, -1, ""):
                logger.info("Connected to the device")
                self._sahara = KyoceraSahara(self._cdc)
                resp = self._sahara.connect()
                if not resp:
                    self.disconnect()
                    return False
                return True
        except Exception as e:
            logger.debug(f"USB connection instantiation exception: {e}")
        return False

    def disconnect(self) -> None:
        if self._sahara:
            self._sahara.disconnect()
        if self._cdc:
            try:
                self._cdc.close(True)
            except Exception as e:
                # Catching expected 'Entity not found' or disconnected bus errors during reset teardown
                logger.debug(
                    f"USB interface closed with expected disconnect state: {e}"
                )
        self._parts.clear()

    def peek_sector(self, lba: int, count: int) -> Optional[bytes]:
        if not self._sahara:
            raise RuntimeError("Not initialized")
        return self._sahara.vendor_cmd_a3_peek_sector(lba, count)

    def partition_checksum(self, partition: str) -> int:
        if not self._sahara:
            raise RuntimeError("Not initialized")
        if partition not in self._parts:
            logger.error(f"No such partition: {partition}")
        resp = self._sahara.vendor_cmd_a7_checksum(
            self._parts[partition]["first_lba"], self._parts[partition]["sectors"] * 512
        )
        if resp is None:
            raise RuntimeError("Failed to call checksum function")
        return resp

    def erase_partition(self, partition: str) -> bool:
        if not self._sahara:
            raise RuntimeError("Not initialized")
        if partition in self._parts:
            return self._sahara.vendor_cmd_a5_erase_region(
                self._parts[partition]["first_lba"], self._parts[partition]["sectors"]
            )
        logger.error(f"No such partition: {partition}")
        return False

    def prepare_partition(self, partition: str) -> bool:
        if not self._sahara:
            raise RuntimeError("Not initialized")
        if partition in self._parts:
            return self._sahara.vendor_cmd_ab_clear_write_protect(
                self._parts[partition]["first_lba"],
                self._parts[partition]["sectors"] * 512,
            )
        logger.error(f"No such partition: {partition}")
        return False

    def _read_lba_range(self, first_lba: int, sectors: int, label: str) -> Optional[bytes]:
        """Chunked 0xB9 read of `sectors` sectors starting at `first_lba`.

        Clips to the device's declared total sector count (self._total_sectors,
        populated by read_gpt()) if known. The static partition table can
        describe a larger disk than the device's own GPT header currently
        declares (e.g. a smaller/replacement eMMC than this firmware's
        partition layout was designed for); reading far past that declared
        boundary has been observed to make the device stall/hang rather than
        fail fast, so anything past it is refused instead of attempted.
        """
        if self._total_sectors is not None and first_lba + sectors > self._total_sectors:
            available = max(0, self._total_sectors - first_lba)
            if available <= 0:
                logger.error(
                    f"{label} (lba {first_lba}) lies entirely beyond the device's "
                    f"declared capacity ({self._total_sectors} sectors); nothing to read."
                )
                return None
            logger.warning(
                f"{label} extends to sector {first_lba + sectors}, but the device only "
                f"declares {self._total_sectors} sectors total. "
                f"Reading only the available {available}/{sectors} sectors."
            )
            sectors = available

        buf = bytearray()
        remaining = sectors
        lba = first_lba
        while remaining > 0:
            n = min(self.PARTITION_READ_CHUNK_SECTORS, remaining)
            chunk = self._sahara.vendor_cmd_b9_read_raw(lba, n)
            if chunk is None:
                logger.error(f"Failed reading {label} at sector (lba) {lba} (n={n})")
                return None
            buf.extend(chunk)
            lba += n
            remaining -= n
        return bytes(buf)

    def read_partition(self, partition: str) -> Optional[bytes]:
        if not self._sahara:
            raise RuntimeError("Not initialized")
        if partition not in self._parts:
            logger.error(f"No such partition: {partition}")
            return None
        first_lba = self._parts[partition]["first_lba"]
        sectors = self._parts[partition]["sectors"]
        return self._read_lba_range(first_lba, sectors, f"Partition '{partition}'")

    def read_raw_range(self, start_lba: int, sectors: int) -> Optional[bytes]:
        """Read an arbitrary raw LBA range, independent of the partition table.

        Useful for regions the partition table doesn't cover at all, e.g. the
        protective MBR + primary GPT header + partition entry table + padding
        that sits before the first real partition entry.
        """
        if not self._sahara:
            raise RuntimeError("Not initialized")
        return self._read_lba_range(start_lba, sectors, "Raw range")

    def write_partition(
        self, partition: str, data: bytes, allow_padding: bool = False
    ) -> bool:
        if not self._sahara:
            raise RuntimeError("Not initialized")
        if partition not in self._parts:
            logger.error(f"No such partition: {partition}")
            return False
        expected_size = self._parts[partition]["sectors"] * 512
        if allow_padding and len(data) < expected_size:
            data = data.ljust(expected_size, b"\x00")
            logger.info(f"Padding partition {partition} to {len(data)} bytes")
        if len(data) % 512 != 0:
            logger.error("write_partition: data size must be a multiple of 512 bytes")
            return False

        first_lba = self._parts[partition]["first_lba"]
        total_sectors = len(data) // 512
        sent_sectors = 0
        lba = first_lba
        while sent_sectors < total_sectors:
            n = min(self.PARTITION_WRITE_CHUNK_SECTORS, total_sectors - sent_sectors)
            chunk = data[sent_sectors * 512 : (sent_sectors + n) * 512]
            if not self._sahara.vendor_cmd_ba_write_raw(lba, chunk):
                logger.error(
                    f"Failed writing partition {partition} at sector (lba) {lba} (n={n})"
                )
                return False
            lba += n
            sent_sectors += n
        return True

    def read_gpt(self) -> int:
        """Reads the GPT and returns the total sector count of the eMMC disk."""
        if not self._sahara:
            raise RuntimeError("Not initialized")
        self._parts.clear()
        boot_region = self._sahara.vendor_cmd_b9_read_raw(0, 34)
        if not boot_region or len(boot_region) < 1024:
            raise RuntimeError("Could not read initial boot sector/GPT data")

        gpt_hdr = boot_region[512:1024]
        if gpt_hdr[:8] != b"EFI PART":
            raise RuntimeError("No GPT signature at LBA1")

        # The Backup GPT LBA is stored at offset 0x20.
        # Since it is written to the final sector of the disk, (backup_lba + 1) gives total sectors.
        backup_gpt_lba = struct.unpack_from("<Q", gpt_hdr, 0x20)[0]
        total_sectors = backup_gpt_lba + 1
        self._total_sectors = total_sectors
        logger.info("Total eMMC sectors detected via GPT: %d", total_sectors)

        last_usable_lba = struct.unpack_from("<Q", gpt_hdr, 0x30)[0]
        entries_lba = struct.unpack_from("<Q", gpt_hdr, 72)[0]
        entries_count = struct.unpack_from("<I", gpt_hdr, 80)[0]
        entry_size = struct.unpack_from("<I", gpt_hdr, 84)[0]
        entries_crc = struct.unpack_from("<I", gpt_hdr, 88)[0]

        entries_bytes = entries_count * entry_size
        if (entries_lba + (entries_bytes + 511) // 512) * 512 <= len(boot_region):
            entries_data = boot_region[
                entries_lba * 512 : entries_lba * 512 + entries_bytes
            ]
        else:
            entries_data = self._sahara.vendor_cmd_b9_read_raw(
                entries_lba, (entries_bytes + 511) // 512
            )

        if not entries_data:
            raise RuntimeError("Failed reading GPT entry array data")

        calc_crc = binascii.crc32(entries_data[:entries_bytes]) & 0xFFFFFFFF
        if entries_crc != calc_crc:
            raise RuntimeError(
                f"GPT entries crc does not match, r:{entries_crc:08X} c:{calc_crc:08X}"
            )

        for i in range(entries_count):
            off = i * entry_size
            ent = entries_data[off : off + entry_size]
            if not ent or ent == b"\x00" * entry_size:
                continue
            first_lba = struct.unpack_from("<Q", ent, 32)[0]
            last_lba = struct.unpack_from("<Q", ent, 40)[0]
            attrs = struct.unpack_from("<Q", ent, 48)[0]
            name_utf16 = ent[56 : 56 + 72]
            try:
                name = name_utf16.decode("utf-16le").rstrip("\x00")
            except Exception:
                name = f"__unknown_partition_{i}__"
            sectors = (last_lba - first_lba + 1) if last_lba >= first_lba else 0
            self._parts[name] = {
                "index": i,
                "first_lba": first_lba,
                "last_lba": last_lba,
                "sectors": sectors,
                "attrs": attrs,
                "name": name,
            }
        return total_sectors

    def is_secureboot_enabled(self) -> bool:
        if not self._sahara:
            raise RuntimeError("Not initialized")
        resp = self._sahara.vendor_cmd_b5_read_secureboot()

        if isinstance(resp, bool):
            return resp

        raise RuntimeError

    CHUNK_MAX_RETRIES = 6

    def raw_dump_emmc(
        self,
        output_path: Path,
        total_sectors: int = 15269888,
        chunk_size: int = 2048,
        resume: bool = False,
    ) -> bool:
        if not self._sahara:
            logger.error("Not initialized")
            return False

        start_lba = 0
        sectors_read = 0
        mode = "wb"

        if resume and output_path.exists():
            existing_size = output_path.stat().st_size
            resumable_sectors = existing_size // 512
            if resumable_sectors > 0:
                if resumable_sectors >= total_sectors:
                    logger.info("Existing output already covers the full dump size.")
                    return True
                sectors_read = resumable_sectors
                mode = "r+b"
                logger.info(
                    f"Resuming dump: {existing_size} bytes ({sectors_read} sectors) "
                    "already present, continuing from there."
                )

        logger.info("Starting full eMMC raw dump...")
        start_time = time.time()
        start_sectors_read = sectors_read

        with open(output_path, mode) as f:
            if mode == "r+b":
                # Drop any trailing partial (non-sector-aligned) bytes from a
                # previous interrupted run before appending more data.
                f.seek(sectors_read * 512)
                f.truncate()
                f.seek(0, os.SEEK_END)

            while sectors_read < total_sectors:
                to_read = min(chunk_size, total_sectors - sectors_read)
                current_lba = start_lba + sectors_read

                pct = (sectors_read / total_sectors) * 100
                elapsed = time.time() - start_time
                done_this_run = sectors_read - start_sectors_read
                sec_per_s = done_this_run / elapsed if elapsed > 0 else 0
                mb_per_s = sec_per_s * 512 / (1024 * 1024)
                eta = (
                    (total_sectors - sectors_read) / sec_per_s
                    if sec_per_s > 0
                    else float("inf")
                )
                print(
                    f"  Read progress: {pct:.2f}% ({sectors_read}/{total_sectors} sectors) | LBA: {current_lba} | "
                    f"{sec_per_s:.0f} sectors/s ({mb_per_s:.2f} MB/s) | ETA: {format_eta(eta)}",
                    end="\r",
                )

                chunk = None
                for attempt in range(1, self.CHUNK_MAX_RETRIES + 1):
                    chunk = self._sahara.vendor_cmd_b9_read_raw(current_lba, to_read)
                    if chunk is not None:
                        break
                    backoff = min(30.0, 2.0 * attempt)
                    print(
                        f"\n  [WARN] Read stalled at LBA {current_lba} "
                        f"(attempt {attempt}/{self.CHUNK_MAX_RETRIES}), "
                        f"retrying in {backoff:.0f}s..."
                    )
                    time.sleep(backoff)

                if chunk is None:
                    print(
                        f"\n  [ERROR] Failed to read chunk at LBA {current_lba} after "
                        f"{self.CHUNK_MAX_RETRIES} attempts. Dump aborted "
                        f"(partial data kept — rerun with --resume to continue)."
                    )
                    break

                f.write(chunk)
                sectors_read += to_read

        if sectors_read == total_sectors:
            print(f"\n  Progress: 100.00% ({total_sectors}/{total_sectors} sectors)")
            logger.info(f"Successfully dumped entire eMMC to {output_path}")
            return True
        else:
            logger.error(
                f"Incomplete dump. Saved up to sector {sectors_read}/{total_sectors}. "
                "Rerun with --resume to continue from this point."
            )
            return False

    def raw_write_emmc(self, input_path: Path, chunk_sectors: int = 2048) -> bool:
        if not self._sahara:
            raise RuntimeError("Not initialized")
        if not input_path.exists():
            logger.error(f"Input image file does not exist: {input_path}")
            return False

        file_size = input_path.stat().st_size
        if file_size % 512 != 0:
            logger.error(
                "Input raw image size must be a strict multiple of 512 bytes (sector aligned)."
            )
            return False

        total_sectors = file_size // 512
        logger.info(
            f"Preparing full eMMC restoration: {file_size} bytes ({total_sectors} sectors)"
        )

        self._sahara.vendor_cmd_ab_clear_write_protect(0, file_size)

        sectors_written = 0
        chunk_bytes = chunk_sectors * 512
        start_time = time.time()

        with open(input_path, "rb") as f:
            while sectors_written < total_sectors:
                current_lba = sectors_written
                data_chunk = f.read(chunk_bytes)
                if not data_chunk:
                    break

                actual_sectors = len(data_chunk) // 512
                pct = (sectors_written / total_sectors) * 100
                elapsed = time.time() - start_time
                sec_per_s = sectors_written / elapsed if elapsed > 0 else 0
                mb_per_s = sec_per_s * 512 / (1024 * 1024)
                eta = (
                    (total_sectors - sectors_written) / sec_per_s
                    if sec_per_s > 0
                    else float("inf")
                )
                print(
                    f"  Flash progress: {pct:.2f}% ({sectors_written}/{total_sectors} sectors) | LBA: {current_lba} | "
                    f"{sec_per_s:.0f} sectors/s ({mb_per_s:.2f} MB/s) | ETA: {format_eta(eta)}",
                    end="\r",
                )

                if not self._sahara.vendor_cmd_ba_write_raw(current_lba, data_chunk):
                    print(
                        f"\n  [ERROR] Failed flashing raw sector array sequence at LBA {current_lba}. Restoration aborted."
                    )
                    return False

                sectors_written += actual_sectors

        if sectors_written == total_sectors:
            print(
                f"\n  Flash Progress: 100.00% ({total_sectors}/{total_sectors} sectors)"
            )
            logger.info("Successfully restored target eMMC raw storage container.")
            return True
        else:
            logger.error("Incomplete structural raw disk validation sync occurred.")
            return False


def format_eta(seconds: float) -> str:
    if seconds <= 0 or seconds == float("inf"):
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def checksum_file(filepath: str, offset: int = 0, length: Optional[int] = None) -> int:
    file_path = Path(filepath)
    file_size = file_path.stat().st_size

    if offset >= file_size:
        return 0

    if length is None:
        length = file_size - offset
    else:
        length = min(length, file_size - offset)

    if length == 0:
        return 0

    with open(filepath, "rb") as f:
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mmapped_file:
            chunk_size = 65536
            checksum = 0
            pos = offset
            end = offset + length

            while pos < end:
                chunk_end = min(pos + chunk_size, end)
                chunk = mmapped_file[pos:chunk_end]

                i = 0
                chunk_len = len(chunk)
                while i + 4 <= chunk_len:
                    dword = int.from_bytes(chunk[i : i + 4], "little")
                    checksum = (checksum + dword) & 0xFFFFFFFF
                    i += 4

                if chunk_end == end and i < chunk_len:
                    remaining = chunk_len - i
                    last_bytes = chunk[i:] + b"\x00" * (4 - remaining)
                    last_dword = int.from_bytes(last_bytes[:4], "little")
                    mask = (1 << (remaining * 8)) - 1
                    checksum = (checksum + (last_dword & mask)) & 0xFFFFFFFF

                pos = chunk_end

    return checksum


def handle_info(qdl: Qdl, args: argparse.Namespace):
    sb_enabled = qdl.is_secureboot_enabled()

    sb_status = "Enforced" if sb_enabled else "Blown"
    print(f"Secure boot status: {sb_status}")

    qdl.read_gpt()
    print(
        f"{'Index':<6} {'Partition Name':<20} {'LBA Range':<25} {'Sectors':<12} {'Attributes'}"
    )
    print("-" * 75)
    for part in qdl.partitions.values():
        lba_range = f"{part['first_lba']}-{part['last_lba']}"
        print(
            f"{part['index']:<6} {part['name']:<20} {lba_range:<25} {part['sectors']:<12} {part['attrs']}"
        )


def handle_dump(qdl: Qdl, args: argparse.Namespace):
    output_img = Path(args.output)

    # We read the GPT in every path: for --full it gives total disk sectors,
    # for -p it's needed to resolve the partition, and for --sectors it's
    # needed to know the device's real declared capacity so the raw read can
    # be clipped instead of stalling the device past it.
    try:
        total_sectors = qdl.read_gpt()
    except Exception as e:
        logger.error(f"Failed parsing GPT configuration data: {e}")
        return

    if args.full:
        qdl.raw_dump_emmc(
            output_img, total_sectors=total_sectors, resume=args.resume
        )
    elif args.sectors is not None:
        # Arbitrary raw LBA range, independent of the partition table. Useful
        # for regions the table doesn't cover at all, e.g. the protective MBR
        # + primary GPT header + partition entry table + padding that sits
        # before the first real partition entry.
        logger.info(
            f"Dumping raw range: lba={args.start_lba}, sectors={args.sectors}..."
        )
        data = qdl.read_raw_range(args.start_lba, args.sectors)
        if data is not None:
            output_img.write_bytes(data)
            logger.info(f"Successfully dumped raw range out to {output_img}")
        else:
            logger.error("Failed to dump raw LBA range.")
    else:
        if not args.partition:
            logger.error(
                "Error: --partition name required unless doing a --full disk dump "
                "or a --sectors raw range dump"
            )
            return
        if args.partition not in qdl.partitions:
            logger.error(f"Partition '{args.partition}' not found in GPT mapping.")
            return
        part = qdl.partitions[args.partition]
        logger.info(
            f"Dumping partition '{args.partition}' ({part['sectors']} sectors)..."
        )
        data = qdl.read_partition(args.partition)
        if data is not None:
            output_img.write_bytes(data)
            logger.info(f"Successfully dumped partition out to {output_img}")
        else:
            logger.error("Failed to dump data from partition.")


def handle_flash(qdl: Qdl, args: argparse.Namespace):
    input_file = Path(args.image)
    if not input_file.exists():
        logger.error(f"Input file does not exist: {input_file}")
        return

    try:
        qdl.read_gpt()
    except Exception as e:
        logger.error(
            f"Failed parsing GPT configuration data ahead of flashing process: {e}"
        )
        return

    if args.full:
        qdl.raw_write_emmc(input_file)
        return

    if not args.partition:
        logger.error(
            "Error: --partition name target configuration required unless choosing --full eMMC flashing strategies."
        )
        return

    if args.partition not in qdl.partitions:
        logger.error(f"Partition '{args.partition}' not found in GPT mapping.")
        return

    data = input_file.read_bytes()

    if args.clear_wp:
        qdl.prepare_partition(args.partition)
    if args.erase:
        qdl.erase_partition(args.partition)

    logger.info(
        f"Flashing {input_file.name} directly into partition '{args.partition}'..."
    )
    if qdl.write_partition(args.partition, data, allow_padding=args.pad):
        logger.info("Flash completed successfully.")
    else:
        logger.error("Flashing routine failed.")


def handle_erase(qdl: Qdl, args: argparse.Namespace):
    qdl.read_gpt()
    if args.partition not in qdl.partitions:
        logger.error(f"Partition '{args.partition}' not found in GPT mapping.")
        return
    logger.info(f"Erasing partition '{args.partition}'...")
    if qdl.erase_partition(args.partition):
        logger.info("Erase completed successfully.")
    else:
        logger.error("Erase routine failed.")


def handle_verify(qdl: Qdl, args: argparse.Namespace):
    local_file = Path(args.image)
    if not local_file.exists():
        logger.error(f"Local target verification file does not exist: {local_file}")
        return
    qdl.read_gpt()
    if args.partition not in qdl.partitions:
        logger.error(f"Partition '{args.partition}' not found in GPT mapping.")
        return

    logger.info(f"Calculating dynamic checksums for verification...")
    local_crc = hex(checksum_file(str(local_file)))
    remote_crc = hex(qdl.partition_checksum(args.partition))

    print(f"Local Image CRC  : {local_crc}")
    print(f"Device Partition CRC: {remote_crc}")
    if local_crc == remote_crc:
        print("RESULT: Checksums match perfectly! Verification passed.")
    else:
        print("RESULT: CRITICAL WARNING! Mismatched checksum profiles.")


def handle_peek(qdl: Qdl, args: argparse.Namespace):
    data = qdl.peek_sector(args.sector_lba, args.count)
    if data is None:
        logger.error("Peek failed.")
        return
    ascii_repr = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    print(f"Sector (LBA) {args.sector_lba}, {len(data)} bytes:")
    print(data.hex())
    print(f"ascii: {ascii_repr}")
    if args.output:
        Path(args.output).write_bytes(data)
        logger.info(f"Saved to {args.output}")


def main():
    parser = argparse.ArgumentParser(
        description="Kyocera EDL flashing utility",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  1. View GPT mapping and secureboot status:
     python3 ky-edl.py info
  2. Dump entire eMMC:
     python3 ky-edl.py dump --full -o full_emmc.img
  2b. Resume an interrupted full dump:
     python3 ky-edl.py dump --full --resume -o full_emmc.img
  3. Dump a single partition:
     python3 ky-edl.py dump -p system -o system.img
  3b. Dump a raw LBA range (e.g. MBR/GPT header/entries before the first partition):
     python3 ky-edl.py dump --start-lba 0 --sectors 16384 -o gpt.img
  4. Flash a single partition:
     python3 ky-edl.py flash -p system -i system.img
  5. Flash an entire raw eMMC image:
     python3 ky-edl.py flash --full -i full_emmc.img
        """,
    )
    parser.add_argument(
        "--vid",
        type=lambda x: int(x, 16),
        default="0x0482",
        help="USB Vendor ID in hex (default: 0x0482)",
    )
    parser.add_argument(
        "--pid",
        type=lambda x: int(x, 16),
        default="0x0a7f",
        help="USB Product ID in hex (default: 0x0a7f)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging for low-level Sahara commands",
    )

    subparsers = parser.add_subparsers(
        dest="action", required=True, help="Command to execute"
    )

    # Action: Info
    subparsers.add_parser(
        "info",
        help="Show device status and the GPT partition table map.",
        description="""
[info] 
Check SecureBoot bit, and reads the GUID Partition Table (GPT) map.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Action: Dump
    dump_parser = subparsers.add_parser(
        "dump",
        help="Dump data from the device and save it to a local file.",
        description="""
[dump]
Reads data from the eMMC chip via Kyocera modifed Sahara commands. Can dump a specific
partition by name, dump the entire raw eMMC, or dump an arbitrary raw LBA range
(e.g. the MBR/GPT header/partition table region before the first named partition,
which --partition and --full don't otherwise cover on their own).
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    dump_parser.add_argument(
        "-o", "--output", required=True, help="Path to save the output image file."
    )
    dump_parser.add_argument("-p", "--partition", help="Name of the partition to dump.")
    dump_parser.add_argument(
        "--full", action="store_true", help="Dump the entire eMMC."
    )
    dump_parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume a --full dump from an existing partial --output file instead of starting over.",
    )
    dump_parser.add_argument(
        "--start-lba",
        type=lambda x: int(x, 0),
        default=0,
        help="First sector to read for a raw LBA-range dump (used with --sectors, default: 0).",
    )
    dump_parser.add_argument(
        "--sectors",
        type=lambda x: int(x, 0),
        default=None,
        help="Dump this many raw sectors starting at --start-lba, independent of the "
        "partition table (e.g. --start-lba 0 --sectors 16384 for the MBR/GPT/entries "
        "region before the first partition). Overrides --partition/--full.",
    )

    # Action: Flash
    flash_parser = subparsers.add_parser(
        "flash",
        help="Write a local image file to a partition or flash the entire eMMC.",
        description="""
        
[flash] 
Writes local binary files onto the device eMMC.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    flash_parser.add_argument(
        "-i", "--image", required=True, help="Path to image/binary file."
    )
    flash_parser.add_argument(
        "-p",
        "--partition",
        help="Name of the target partition to overwrite (omit if using --full).",
    )
    flash_parser.add_argument(
        "--full",
        action="store_true",
        help="Overwrite the ENTIRE eMMC starting at LBA 0.",
    )
    flash_parser.add_argument(
        "--clear-wp",
        action="store_true",
        help="Attempt to strip hardware write protection from target sectors before flashing.",
    )
    flash_parser.add_argument(
        "--erase",
        action="store_true",
        help="Erase target blocks clean before writing data payloads.",
    )
    flash_parser.add_argument(
        "--pad",
        action="store_true",
        help="Pad the input file with trailing zeros to match sector alignment boundaries.",
    )

    # Action: Erase
    erase_parser = subparsers.add_parser(
        "erase",
        help="Erase and wipe a specified partition.",
        description="""
[erase] 
Erase partition by filling it with zeroes.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    erase_parser.add_argument(
        "-p", "--partition", required=True, help="Name of the partition to erase."
    )

    # Action: Verify
    verify_parser = subparsers.add_parser(
        "verify",
        help="Verify a partition's integrity against a local image file.",
        description="""
[verify] 
Calculates the checksum of a local file and compares it directly 
against a hardware-calculated checksum of the partition on the device.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    verify_parser.add_argument(
        "-p", "--partition", required=True, help="Name of the partition to verify."
    )
    verify_parser.add_argument(
        "-i",
        "--image",
        required=True,
        help="Path to the local image file to check against.",
    )

    # Action: Peek
    peek_parser = subparsers.add_parser(
        "peek",
        help="Read the first N bytes of a raw eMMC sector (vendor cmd 0xA3).",
        description="""
[peek]
Reads the first `count` bytes of eMMC sector `lba` using the vendor
0xA3 command - a lightweight alternative to a full 0xB9 sector read.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    peek_parser.add_argument(
        "--sector-lba",
        type=lambda x: int(x, 0),
        required=True,
        help="Sector (LBA) number to read from.",
    )
    peek_parser.add_argument(
        "--count",
        type=lambda x: int(x, 0),
        default=16,
        help="Number of bytes to read from the start of the sector (default: 16).",
    )
    peek_parser.add_argument(
        "-o", "--output", help="Optional path to save the raw bytes to."
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    qdl = Qdl(args.vid, args.pid)

    print(
        f"Waiting for device (VID: 0x{args.vid:04x}, PID: 0x{args.pid:04x})... Connect device now."
    )
    while not qdl.connect():
        print("Device not found. Retrying...", end="\r")
        time.sleep(2)
    print("\nDevice found and handshake complete!")

    try:
        dispatch_map = {
            "info": handle_info,
            "dump": handle_dump,
            "flash": handle_flash,
            "erase": handle_erase,
            "verify": handle_verify,
            "peek": handle_peek,
        }
        dispatch_map[args.action](qdl, args)
    finally:
        print("\n")
        qdl.disconnect()


if __name__ == "__main__":
    main()
