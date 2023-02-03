#!/usr/bin/env python3

# ---------------------------------------------------------------------------
# Original code from the PunchThrough Repo espresso-ble
# https://github.com/PunchThrough/espresso-ble
# ---------------------------------------------------------------------------
#
import logging
from operator import truediv
import signal
import threading
import random
import time
import datetime
import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service
import struct
from collections import deque
from enum import Enum

from .ble import (
    Advertisement,
    Characteristic,
    Service,
    Application,
    find_adapter,
    Descriptor,
    Agent,
)

MainLoop = None

try:
    from gi.repository import GLib

    MainLoop = GLib.MainLoop

except ImportError:
    import gobject as GObject

    MainLoop = GObject.MainLoop

DBUS_OM_IFACE = "org.freedesktop.DBus.ObjectManager"
DBUS_PROP_IFACE = "org.freedesktop.DBus.Properties"

GATT_SERVICE_IFACE = "org.bluez.GattService1"
GATT_CHRC_IFACE = "org.bluez.GattCharacteristic1"
GATT_DESC_IFACE = "org.bluez.GattDescriptor1"

LE_ADVERTISING_MANAGER_IFACE = "org.bluez.LEAdvertisingManager1"
LE_ADVERTISEMENT_IFACE = "org.bluez.LEAdvertisement1"

BLUEZ_SERVICE_NAME = "org.bluez"
GATT_MANAGER_IFACE = "org.bluez.GattManager1"

logger = logging.getLogger(__name__)

mainloop = None
AppConnectState = None
AppKeylockReceiveCount = 0
ble_command_q = None
DistanceOffset = 0
CurrentDistance = 0
StrokeOffset = 0
CurrentStrokes = 0
StartTime = None
PendingReset = False

class AppConnectStateEnum(Enum):
    Start=1
    Started=2
    WaitKeylockResponse=3
    ReceivedKeylockResponse=4
    Connected=5

class InvalidArgsException(dbus.exceptions.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.InvalidArgs"


class NotSupportedException(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.NotSupported"


class NotPermittedException(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.NotPermitted"


class InvalidValueLengthException(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.InvalidValueLength"


class FailedException(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.Failed"


def register_app_cb():
    logger.info("GATT application registered")


def register_app_error_cb(error):
    logger.critical("Failed to register application: " + str(error))
    mainloop.quit()

# Function is needed to trigger the reset of the waterrower. It puts the "reset_ble" into the queue (FIFO) in order
# for the WaterrowerInterface thread to get the signal to reset the waterrower.


class SmartRow(Service):
    SMART_ROW_SERVICE_UUID = '1234'

    def __init__(self, bus, index):
        Service.__init__(self, bus, index, self.SMART_ROW_SERVICE_UUID, True)
        self.add_characteristic(WriteToSmartRow(bus,0,self))
        self.add_characteristic(SmartRowData(bus, 1, self))

class WriteToSmartRow(Characteristic):
    WRITE_TO_SMARTROW_UUID = '1235'

    def __init__(self, bus, index, service):
        Characteristic.__init__(
            self, bus, index,
            self.WRITE_TO_SMARTROW_UUID,
            ['write', 'read'],
            service)
        self.value = 0

    def WriteValue(self, value, options):
        self.value = value
        sval = ''.join([str(v) for v in value])
        #print('WriteValue(1235): ' + sval)
        ManageConnection(sval)

    def ReadValue(self, options):
        #print('ReadValue(1235): '+str(options))
        pass

class SmartRowData(Characteristic):
    SMARTROW_DATA_UUID = '1236'

    def __init__(self, bus, index, service):
        Characteristic.__init__(
            self, bus, index,
            self.SMARTROW_DATA_UUID,
            ['notify','read', 'write'],
            service)
        self.notifying = False
        self.iter = 0

    def Waterrower_cb(self):
        global AppConnectState
        global PendingReset
        global ble_command_q
        global ble_in_q_value
        global StartTime

        smartRowFakeData = None
        value = dbus.Byte(0)

        if (AppConnectState == AppConnectStateEnum.Connected):
            if (len(ble_command_q) > 0):
                #print('Command queue length='+str(len(ble_command_q)))
                smartRowFakeData = ble_command_q.popleft()

            elif ble_in_q_value:
                try:
                    smartRowFakeData = ble_in_q_value.popleft()
                    if (not 'V3.00' in smartRowFakeData and not 'V@' in smartRowFakeData):
                        if (StartTime is None and smartRowFakeData[0] == 'f' and smartRowFakeData[11] != '!'):
                            logger.info('Starting rowing timer!')
                            StartTime = time.time()

                        distance = GetDistance(smartRowFakeData)
                        smartRowFakeData = smartRowFakeData[0] + distance + smartRowFakeData[6:14]

                        if (smartRowFakeData[0] == 'a'):
                            smartRowFakeData = AddTime(smartRowFakeData)

                        if (smartRowFakeData[0] == 'd'):
                            smartRowFakeData = AddStrokeCount(smartRowFakeData)

                        cksum  =f'{(sum(ord(ch) for ch in smartRowFakeData)):0>4X}'
                        smartRowFakeData = smartRowFakeData + cksum[-2:] + '\r'

                        if PendingReset:
                            smartRowFakeData = '\rV@\r' + smartRowFakeData
                            PendingReset = False
                except:
                    logger.warn('Exception when processing ble_in_q_value')
                    smartRowFakeData = None

        elif (AppConnectState == AppConnectState.WaitKeylockResponse and len(ble_command_q) > 0):
            smartRowFakeData = ble_command_q.popleft()

        elif (AppConnectState == AppConnectState.ReceivedKeylockResponse):
            smartRowFakeData = '\r'
            AppConnectState = AppConnectStateEnum.Connected
            logger.info("Connect state=4: Connected")

        if (smartRowFakeData is not None): 
            value = [dbus.Byte(ord(b)) for b in smartRowFakeData]
            #logger.info('Sending: '+str(smartRowFakeData).replace('\r', '\\r'))
            self.PropertiesChanged(GATT_CHRC_IFACE, { 'Value': value }, [])
            
        else:
            #logger.warning("no data from SmartRow interface")
            pass

        return self.notifying

    def _update_Waterrower_cb_value(self):
        #print('Update Smartrow Data ' + self.notifying)

        if not self.notifying:
            return

        GLib.timeout_add(50, self.Waterrower_cb)

    def StartNotify(self):
        if self.notifying:
            #print('Already notifying, nothing to do')
            return

        self.notifying = True

        GLib.timeout_add(50, self.Waterrower_cb)
        #print("STARTING NOTIFICATION!")
        self.PropertiesChanged(GATT_CHRC_IFACE, { 'Value': [dbus.Byte(13)] }, [])
        #print("DONE 2 STARTING NOTIFICATION!")
        logger.info('Starting notification')

    def StopNotify(self):
        if not self.notifying:
            #print('Not notifying, nothing to do')
            return

        logger.info('Ending notification')
        self.notifying = False
        ResetConnection()

    def ReadValue(self, options):
        #print('ReadValue(1236): '+str(options))
        pass

    def WriteValue(self, value, options):
        self.value = value
        logger.info(self.value)
        #print('WriteValue(1236): '+str(options))

class SmartRowAdvertisement(Advertisement):
    def __init__(self, bus, index):
        Advertisement.__init__(self, bus, index, "peripheral")
        self.add_manufacturer_data(
             0x1235, [0x34, 0x34]
        )
        self.discoverable = True
        self.add_service_uuid(SmartRow.SMART_ROW_SERVICE_UUID)
        self.add_local_name("SmartRow")
        self.include_tx_power = True

# Start over with connecting to app
def ResetConnection():
    global AppConnectState
    global AppKeylockReceiveCount
    global DistanceOffset
    global StrokeOffset
    global ble_command_q
    global PendingReset
    global CurrentDistance
    global CurrentStrokes
    global StartTime

    logger.info('Resetting app connection')
    AppConnectState = AppConnectStateEnum.Start
    AppKeylockReceiveCount = 0
    DistanceOffset = CurrentDistance
    StrokeOffset = CurrentStrokes
    PendingReset = False
    StartTime = None
    ble_command_q.clear()

def ManageConnection(value):
    global AppConnectState
    global AppKeylockReceiveCount
    global ble_command_q
    global PendingReset
    global DistanceOffset
    global CurrentDistance
    global StrokeOffset
    global CurrentStrokes
    global StartTime

    if(AppConnectState == AppConnectStateEnum.Connected):
        if 'V@' in value:
            # Handle reset
            logger.info('Resetting time, distance and stroke count on reset')
            PendingReset = True
            DistanceOffset = CurrentDistance
            StrokeOffset = CurrentStrokes
            StartTime = None  # Wait for rowing to start before starting timer
            return

    else:
        logger.info('App not connected and received ' + str(value))
        
    if (AppConnectState == AppConnectStateEnum.Start and value[0] == '$'):
        logger.info('Connect state=1')
        AppConnectState = AppConnectStateEnum.Started
        AppKeylockReceiveCount = 0

    elif (AppConnectState == AppConnectStateEnum.Started and value[0] == '$'):
        logger.info('Connect state=2: Sending challenge')
        AppConnectState = AppConnectStateEnum.WaitKeylockResponse
        AppKeylockReceiveCount = 0
        ble_command_q.clear()
        ble_command_q.append(MakeKeylockChallenge())

    elif (AppConnectState == AppConnectStateEnum.WaitKeylockResponse):
        AppKeylockReceiveCount = AppKeylockReceiveCount + 1
        logger.info('Connect state=3: Receive count='+str(AppKeylockReceiveCount))
        if (AppKeylockReceiveCount == 1):
            pass

        elif (AppKeylockReceiveCount < 6):
            ble_command_q.append(str(value))

        elif (AppKeylockReceiveCount == 6):
            ble_command_q.append('\r')
            AppConnectState = AppConnectStateEnum.ReceivedKeylockResponse

def DecryptDistance(data):
    try:
        s = ''
        for c in data[1:6]:
            s += chr(int(ord(c) & 15 | 0x30))
        return s

    except Exception as e:
        print(e)
        print(data)
        return data

def GetDistance(data):
    global DistanceOffset
    global CurrentDistance

    d = int(DecryptDistance(data))

    # Store incoming distance
    CurrentDistance = d

    d = d - DistanceOffset
    if (d < 0):
        d = 0

    strDist = f'{d:05}'
    s = ''
    for c in strDist[0:5]:
            s += chr(int(ord(c) + 0x10))

    return s

def AddTime(data):
    global StartTime
    elapsed = 0

    if (StartTime is not None):
        elapsed = int(time.time() - StartTime)

    # Time format is hmmss. Null out the milliseconds. 
    elapsedStr = str(datetime.timedelta(seconds=elapsed)).replace(':', '')
    return data[:6] + elapsedStr + '   ' + data[14:]

def AddStrokeCount(data):
    global CurrentStrokes
    global StrokeOffset

   # Get and store current stroke count
    CurrentStrokes = int(data[9:13].replace(' ', '0'))
    c = CurrentStrokes - StrokeOffset
    if (c < 0):
        c = 0

    # Stroke count is space padded
    strCount = f'{c: 4}'
    return data[:9] + strCount + data[13:]

def MakeKeylockChallenge():
    rnd = random.randint(8388608, 16777215)
    result='KEYLOCK=' + f'{rnd:0>6X}'
    cksum = f'{(sum(ord(ch) for ch in result)):0>4X}'
    result = result + cksum[-2:]
    logger.info('Generated ' + str(result))
    result = '\r' + result +'\r'
    return result

def register_ad_cb():
    logger.info("Advertisement registered")


def register_ad_error_cb(error):
    logger.critical("Failed to register advertisement: " + str(error))
    mainloop.quit()

def sigint_handler(sig, frame):
    if sig == signal.SIGINT:
        mainloop.quit()
    else:
        raise ValueError("Undefined handler for '{}' ".format(sig))

AGENT_PATH = "/com/inonoob/agent"


def main(out_q, ble_in_q, fake_sr_event):
    global mainloop
    global out_q_reset
    global ble_in_q_value
    global ble_command_q
    global AppConnectState
    
    ble_command_q = deque(maxlen=5)
    out_q_reset = out_q
    ble_in_q_value = ble_in_q
    AppConnectState = AppConnectStateEnum.Start

    logger.info('Waiting for real SmartRow to connect')
    fake_sr_event.wait()
    logger.info('Real SmartRow connected')

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    # get the system bus
    bus = dbus.SystemBus()
    # get the ble controller
    adapter = find_adapter(bus)
    logger.info('Fake SmartRow using ' + str(adapter))

    if not adapter:
        logger.critical("GattManager1 interface not found")
        return

    adapter_obj = bus.get_object(BLUEZ_SERVICE_NAME, adapter)

    adapter_props = dbus.Interface(adapter_obj, "org.freedesktop.DBus.Properties")

    # powered property on the controller to on
    adapter_props.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(1))

    # Get manager objs
    service_manager = dbus.Interface(adapter_obj, GATT_MANAGER_IFACE)
    ad_manager = dbus.Interface(adapter_obj, LE_ADVERTISING_MANAGER_IFACE)

    advertisement = SmartRowAdvertisement(bus, 0)
    obj = bus.get_object(BLUEZ_SERVICE_NAME, "/org/bluez")

    agent = Agent(bus, AGENT_PATH)

    app = Application(bus)
    app.add_service(SmartRow(bus, 0))

    mainloop = MainLoop()

    agent_manager = dbus.Interface(obj, "org.bluez.AgentManager1")
    agent_manager.RegisterAgent(AGENT_PATH, "NoInputNoOutput")

    ad_manager.RegisterAdvertisement(
        advertisement.get_path(),
        {},
        reply_handler=register_ad_cb,
        error_handler=register_ad_error_cb,
    )

    logger.info("Registering GATT application...")

    service_manager.RegisterApplication(
        app.get_path(),
        {},
        reply_handler=register_app_cb,
        error_handler=[register_app_error_cb],
    )

    agent_manager.RequestDefaultAgent(AGENT_PATH)

    mainloop.run()
    # ad_manager.UnregisterAdvertisement(advertisement)
    # dbus.service.Object.remove_from_connection(advertisement)


if __name__ == "__main__":
#     signal.signal(signal.SIGINT, sigint_handler)
    main()

