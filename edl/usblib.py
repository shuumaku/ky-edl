# (c) B.Kerler 2018-2024 under GPLv3 license
# If you use my code, make sure you refer to my name

import time
import array
import logging

from ctypes import c_void_p, c_int

import usb.backend.libusb0
import usb.core  # pyusb
import usb.util

import usb.backend.libusb1

from edl.devicehandler import DeviceClass

logger = logging.getLogger('edl.usblib')

USB_DIR_OUT = 0  # to device
USB_DIR_IN = 0x80  # to host

# USB types, the second of three bRequestType fields
USB_TYPE_MASK = (0x03 << 5)
USB_TYPE_STANDARD = (0x00 << 5)
USB_TYPE_CLASS = (0x01 << 5)
USB_TYPE_VENDOR = (0x02 << 5)
USB_TYPE_RESERVED = (0x03 << 5)

# USB recipients, the third of three bRequestType fields
USB_RECIP_MASK = 0x1f
USB_RECIP_DEVICE = 0x00
USB_RECIP_INTERFACE = 0x01
USB_RECIP_ENDPOINT = 0x02
USB_RECIP_OTHER = 0x03
# From Wireless USB 1.0
USB_RECIP_PORT = 0x04
USB_RECIP_RPIPE = 0x05

MAX_USB_BULK_BUFFER_SIZE = 16384

tag = 0

CDC_CMDS = {
    "SEND_ENCAPSULATED_COMMAND": 0x00,
    "GET_ENCAPSULATED_RESPONSE": 0x01,
    "SET_COMM_FEATURE": 0x02,
    "GET_COMM_FEATURE": 0x03,
    "CLEAR_COMM_FEATURE": 0x04,
    "SET_LINE_CODING": 0x20,
    "GET_LINE_CODING": 0x21,
    "SET_CONTROL_LINE_STATE": 0x22,
    "SEND_BREAK": 0x23,  # wValue is break time
}


class usb_class(DeviceClass):

    def __init__(self, portconfig=None, devclass=-1, serial_number=None):
        super().__init__(portconfig, devclass)
        self.serial_number = serial_number
        self.EP_IN = None
        self.EP_OUT = None
        self.is_serial = False
        self.buffer = array.array('B', [0]) * 1048576
        self.backend = usb.backend.libusb1.get_backend(find_library=lambda x: "libusb-1.0.so")
        if self.backend is not None:
            try:
                self.backend.lib.libusb_set_option.argtypes = [c_void_p, c_int]
                self.backend.lib.libusb_set_option(self.backend.ctx, 1)
            except:
                self.backend = None

    def getInterfaceCount(self):
        if self.vid is not None:
            self.device = usb.core.find(idVendor=self.vid, idProduct=self.pid, backend=self.backend)
            if self.device is None:
                logger.debug("Couldn't detect the device. Is it connected ?")
                return False
            try:
                self.device.set_configuration()
            except Exception as err:
                logger.debug(str(err))
            self.configuration = self.device.get_active_configuration()
            logger.debug(2, self.configuration)
            return self.configuration.bNumInterfaces
        else:
            logger.error("No device detected. Is it connected ?")
        return 0

    def setLineCoding(self, baudrate=None, parity=0, databits=8, stopbits=1):
        sbits = {1: 0, 1.5: 1, 2: 2}
        dbits = {5, 6, 7, 8, 16}
        pmodes = {0, 1, 2, 3, 4}
        brates = {300, 600, 1200, 2400, 4800, 9600, 14400,
                  19200, 28800, 38400, 57600, 115200, 230400}

        if stopbits is not None:
            if stopbits not in sbits.keys():
                valid = ", ".join(str(k) for k in sorted(sbits.keys()))
                raise ValueError("Valid stopbits are " + valid)
            self.stopbits = stopbits
        else:
            self.stopbits = 0

        if databits is not None:
            if databits not in dbits:
                valid = ", ".join(str(d) for d in sorted(dbits))
                raise ValueError("Valid databits are " + valid)
            self.databits = databits
        else:
            self.databits = 0

        if parity is not None:
            if parity not in pmodes:
                valid = ", ".join(str(pm) for pm in sorted(pmodes))
                raise ValueError("Valid parity modes are " + valid)
            self.parity = parity
        else:
            self.parity = 0

        if baudrate is not None:
            if baudrate not in brates:
                brs = sorted(brates)
                dif = [abs(br - baudrate) for br in brs]
                best = brs[dif.index(min(dif))]
                raise ValueError(
                    "Invalid baudrates, nearest valid is {}".format(best))
            self.baudrate = baudrate

        linecode = [
            self.baudrate & 0xff,
            (self.baudrate >> 8) & 0xff,
            (self.baudrate >> 16) & 0xff,
            (self.baudrate >> 24) & 0xff,
            sbits[self.stopbits],
            self.parity,
            self.databits]

        txdir = 0  # 0:OUT, 1:IN
        req_type = 1  # 0:std, 1:class, 2:vendor
        recipient = 1  # 0:device, 1:interface, 2:endpoint, 3:other
        req_type = (txdir << 7) + (req_type << 5) + recipient
        data = bytearray(linecode)
        wlen = self.device.ctrl_transfer(
            req_type, CDC_CMDS["SET_LINE_CODING"],
            data_or_wLength=data, wIndex=1)
        logger.debug("Linecoding set, {}b sent".format(wlen))

    def setbreak(self):
        txdir = 0  # 0:OUT, 1:IN
        req_type = 1  # 0:std, 1:class, 2:vendor
        recipient = 1  # 0:device, 1:interface, 2:endpoint, 3:other
        req_type = (txdir << 7) + (req_type << 5) + recipient
        wlen = self.device.ctrl_transfer(
            bmRequestType=req_type, bRequest=CDC_CMDS["SEND_BREAK"],
            wValue=0, data_or_wLength=0, wIndex=1)
        logger.debug("Break set, {}b sent".format(wlen))

    def setcontrollinestate(self, RTS=None, DTR=None, isFTDI=False):
        ctrlstate = (2 if RTS else 0) + (1 if DTR else 0)
        if isFTDI:
            ctrlstate += (1 << 8) if DTR is not None else 0
            ctrlstate += (2 << 8) if RTS is not None else 0
        txdir = 0  # 0:OUT, 1:IN
        req_type = 2 if isFTDI else 1  # 0:std, 1:class, 2:vendor
        # 0:device, 1:interface, 2:endpoint, 3:other
        recipient = 0 if isFTDI else 1
        req_type = (txdir << 7) + (req_type << 5) + recipient

        wlen = self.device.ctrl_transfer(
            bmRequestType=req_type,
            bRequest=1 if isFTDI else CDC_CMDS["SET_CONTROL_LINE_STATE"],
            wValue=ctrlstate,
            wIndex=1,
            data_or_wLength=0)
        logger.debug("Linecoding set, {}b sent".format(wlen))

    def flush(self):
        return

    def connect(self, EP_IN=-1, EP_OUT=-1, portname: str = ""):
        if self.connected:
            self.close()
            self.connected = False
        self.device = None
        self.EP_OUT = None
        self.EP_IN = None
        devices = usb.core.find(find_all=True, backend=self.backend)
        for dev in devices:
            for usbid in self.portconfig:
                if dev.idProduct == usbid[1] and dev.idVendor == usbid[0]:
                    if self.serial_number is not None:
                        if dev.serial_number != self.serial_number:
                            continue
                    self.device = dev
                    self.vid = dev.idVendor
                    self.pid = dev.idProduct
                    self.serial_number = dev.serial_number
                    self.interface = usbid[2]
                    break
            if self.device is not None:
                break

        if self.device is None:
            logger.debug("Couldn't detect the device. Is it connected ?")
            return False

        try:
            self.configuration = self.device.get_active_configuration()
        except usb.core.USBError as e:
            if e.strerror == "Configuration not set":
                self.device.set_configuration()
                self.configuration = self.device.get_active_configuration()
            if e.errno == 13:
                logger.error("Permission denied accessing {:04x}:{:04x}.".format(self.vid,self.pid))
                logger.info("Potential fix (update udev rules): sudo echo 'SUBSYSTEM==\"usb\",ATTRS{{idVendor}}==\"{:04x}\",ATTRS{{idProduct}}==\"{:04x}\",MODE=\"0666\"' >> /etc/udev/rules.d/99-edl.rules".format(self.vid,self.pid))
                self.backend = usb.backend.libusb0.get_backend()
                self.device = usb.core.find(idVendor=self.vid, idProduct=self.pid, backend=self.backend)
        if self.configuration is None:
            logger.error("Couldn't get device configuration.")
            return False
        if self.interface > self.configuration.bNumInterfaces:
            print("Invalid interface, max number is %d" % self.configuration.bNumInterfaces)
            return False
        for itf in self.configuration:
            if self.devclass == -1 or self.devclass == 0xFF:
                self.devclass = 0x02
            if itf.bInterfaceClass == self.devclass:
                if self.interface == -1 or self.interface == itf.bInterfaceNumber:
                    self.interface = itf
                    self.EP_OUT = EP_OUT
                    self.EP_IN = EP_IN
                    for ep in itf:
                        edir = usb.util.endpoint_direction(ep.bEndpointAddress)
                        if (edir == usb.util.ENDPOINT_OUT and EP_OUT == -1) or ep.bEndpointAddress == (EP_OUT & 0xF):
                            self.EP_OUT = ep
                        elif (edir == usb.util.ENDPOINT_IN and EP_IN == -1) or ep.bEndpointAddress == (EP_OUT & 0xF):
                            self.EP_IN = ep
                    break

        if self.EP_OUT is not None and self.EP_IN is not None:
            self.maxsize = self.EP_IN.wMaxPacketSize
            try:
                if self.device.is_kernel_driver_active(0):
                    logger.debug("Detaching kernel driver")
                    self.device.detach_kernel_driver(0)
            except Exception as err:
                logger.debug("No kernel driver supported: " + str(err))

            try:
                usb.util.claim_interface(self.device, 0)
            except:
                pass
            self.connected = True
            return True
        print("Couldn't find CDC interface. Aborting.")
        self.connected = False
        return False

    def close(self, reset=False):
        if self.connected:
            try:
                if reset:
                    self.device.reset()
                if not self.device.is_kernel_driver_active(self.interface):
                    self.device.attach_kernel_driver(0)
            except Exception as err:
                logger.debug(str(err))
            usb.util.dispose_resources(self.device)
            del self.device
            if reset:
                time.sleep(2)
            self.connected = False

    def write(self, command, pktsize=None):
        if pktsize is None:
            # pktsize = self.EP_OUT.wMaxPacketSize
            pktsize = MAX_USB_BULK_BUFFER_SIZE
        if isinstance(command, str):
            command = bytes(command, 'utf-8')
        pos = 0
        if command == b'':
            try:
                self.EP_OUT.write(b'')
            except usb.core.USBError as err:
                error = str(err.strerror)
                if "timeout" in error:
                    # time.sleep(0.01)
                    try:
                        self.EP_OUT.write(b'')
                    except Exception as err:
                        logger.debug(str(err))
                        return False
                return True
        else:
            i = 0
            while pos < len(command):
                try:
                    ctr = self.EP_OUT.write(command[pos:pos + pktsize])
                    if ctr <= 0:
                        logger.info(ctr)
                    pos += ctr
                except Exception as err:
                    logger.debug(str(err))
                    # print("Error while writing")
                    # time.sleep(0.01)
                    i += 1
                    if i == 3:
                        return False
        self.verify_data(bytearray(command), "TX:")
        return True

    def usbread(self, resplen=None, timeout=0) -> bytes:
        if timeout == 0:
            timeout = 1
        if resplen is None:
            resplen = self.maxsize
        if resplen <= 0:
            logger.info("Warning !")
        res = bytearray()
        buffer = self.buffer[:resplen]
        epr = self.EP_IN.read
        extend = res.extend
        retries = 0
        max_retries = 20
        while len(res) < resplen:
            try:
                resplen = epr(buffer, timeout)
                extend(buffer[:resplen])
                if resplen == self.EP_IN.wMaxPacketSize:
                    break
            except usb.core.USBError as e:
                error = str(e.strerror)
                if "timed out" in error:
                    if timeout is None:
                        return b""
                    # A stalled eMMC read (e.g. controller busy with GC/bad-block
                    # remapping) can take far longer than one bulk-read timeout.
                    # Back off and keep waiting instead of giving up after ~10s.
                    retries += 1
                    if retries >= max_retries:
                        return b""
                    time.sleep(min(2.0, 0.1 * retries))
                    timeout += 1000
                elif "Overflow" in error:
                    logger.error("USB Overflow")
                    return b""
                else:
                    logger.info(repr(e))
                    return b""

        return res[:resplen]

    def ctrl_transfer(self, bmRequestType, bRequest, wValue, wIndex, data_or_wLength):
        ret = self.device.ctrl_transfer(bmRequestType=bmRequestType, bRequest=bRequest, wValue=wValue, wIndex=wIndex,
                                        data_or_wLength=data_or_wLength)
        return ret[0] | (ret[1] << 8)

    class deviceclass:
        vid = 0
        pid = 0

        def __init__(self, vid, pid):
            self.vid = vid
            self.pid = pid

    def detectdevices(self):
        dev = usb.core.find(find_all=True, backend=self.backend)
        ids = [self.deviceclass(cfg.idVendor, cfg.idProduct) for cfg in dev]
        return ids

    def usbwrite(self, data, pktsize=None):
        if pktsize is None:
            pktsize = len(data)
        res = self.write(data, pktsize)
        # port->flush()
        return res

    def usbreadwrite(self, data, resplen):
        self.usbwrite(data)  # size
        # port->flush()
        res = self.usbread(resplen)
        return res
