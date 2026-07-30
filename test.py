import serial
import serial.tools.list_ports
import logging
from datetime import datetime
import time

axisNo = '1'            # Axis number
direction = 'CCW'       # Drive direction setting(-(CCW)、+(CW))
mode = 0                # Drive mode (0: Continue, 1: Step, 2: Origin)
ser = serial.Serial()
baudrate = [38400, 19200, 9600, 4800]

# DS102/DS112 走 USB 時是駿河精機自訂 VID/PID 的 FTDI 晶片
DS_VID = 0x0DFD
DS_PID = 0x0002

# 主機板上的 Intel AMT 虛擬埠(SOL)開得起來但永遠不會回應，
# 不能讓它被當成控制器選中
SKIP_PORT_KEYWORDS = ('Active Management Technology', 'AMT', 'Bluetooth')


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(f"ds102_log_{datetime.now().strftime('%Y%m%d')}.log"),
        logging.StreamHandler() # 同時輸出到終端機
    ]
)


def update_status():
    # ---------------------------------------------------------
    # Request status binary 3
    # ---------------------------------------------------------
    r_data = serial_write_read(('AXI' + axisNo + ':SB3?' + '\r').encode('utf-8'))

    # Terminates if it cannot be converted to a numerical value
    try:
        if r_data is not None:
            int(r_data)
        else:
            logging.info("status Stop")
            status = 'Stop'
            return status
    except ValueError:
        logging.info("status Stop")
        status = 'Stop'
        return status

    if not int(r_data) & 0x01 == 0x01:
        logging.info('Axes cannot be selected')
        status = 'Stop'
        return status
    else:
        # ---------------------------------------------------------
        # Request status binary 1
        # ---------------------------------------------------------
        r_data = serial_write_read(('AXI' + axisNo + ':SB1?' + '\r').encode('utf-8'))

        if int(r_data) & 0x40 == 0x40:
            logging.info('Driving')
            status = 'run'

        elif int(r_data) & 0x10 == 0x10:
            logging.info('Detect origin')
            status = 'Stop'

        elif int(r_data) & 0x02 == 0x02 or int(r_data) & 0x04 == 0x04:
            # Detect limit
            # ---------------------------------------------------------
            # Request status binary 2
            # ---------------------------------------------------------
            r_data = serial_write_read(('AXI' + axisNo + ':SB2?' + '\r').encode('utf-8'))
            if int(r_data) & 0x03 == 0x03:
                logging.info('Stage not connected')

            elif int(r_data) & 0x01 == 0x01:
                logging.info('Detect CW limit')

            elif int(r_data) & 0x02 == 0x02:
                logging.info('Detect CCW limit')

            elif int(r_data) & 0x04 == 0x04:
                logging.info('Detect CW software limit')

            elif int(r_data) & 0x08 == 0x08:
                logging.info('Detect CCW software limit')

            status = 'Stop'
            logging.info(status)

        else:
            status = 'Stop'
            logging.info(status)

        # ---------------------------------------------------------
        # Request the current position
        # ---------------------------------------------------------
        r_data = serial_write_read(('AXI' + axisNo + ':POS?' + '\r').encode('utf-8'))

        return status


def is_skipped_port(port):
    text = f'{port.description} {port.hwid}'
    return any(k.lower() in text.lower() for k in SKIP_PORT_KEYWORDS)


def find_ds_port():
    # ---------------------------------------------------------
    # Locate the controller: returns (device, baudrate) or None
    # ---------------------------------------------------------
    ports = list(serial.tools.list_ports.comports())
    logging.info(f"Detact port: {[p.device for p in ports]}")

    # 走 USB 時可以靠 VID/PID 直接認出來。走 RS-232C 時 VID/PID 屬於
    # 轉接線而不是控制器，所以其餘埠也保留為候選，靠 *IDN? 探測。
    by_id = [p for p in ports if p.vid == DS_VID and p.pid == DS_PID]
    others = [p for p in ports if p not in by_id and not is_skipped_port(p)]

    for port in by_id + others:
        for rate in baudrate:
            try:
                with serial.Serial(port.device, rate, timeout=2) as probe:
                    probe.reset_input_buffer()
                    probe.write(('*IDN?' + '\r').encode('utf-8'))
                    time.sleep(0.1)
                    r_data = probe.read_until(b'\r')
            except (serial.SerialException, OSError) as e:
                logging.warning(f"Probe failed on {port.device}: {e}")
                break

            logging.info(f"Probe {port.device} @ {rate}: {r_data}")
            if 'SURUGA,DS1' in r_data.decode('ascii', errors='replace'):
                return port.device, rate

    logging.error("DS102/DS112 not found on any port")
    return None


def connecet_port():
    global ser

    found = find_ds_port()
    if found is None:
        return

    device, rate = found
    try:
        ser = serial.Serial(device, rate, timeout=2)
        logging.info(f"Connect port: {device} @ {rate}")

    except serial.SerialException as e:
        logging.error(f"Connect error: {e}")
        return
    
    r_data = serial_write_read(('*IDN?' + '\r').encode('utf-8'))

    if 'SURUGA,DS1' in str(r_data):

        r_data = serial_write_read(('DS102VER?' + '\r').encode('utf-8'))
        # ---------------------------------------------------------
        # Get number of control axes
        # ---------------------------------------------------------
        r_data = serial_write_read(('CONTA?' + '\r').encode('utf-8'))

        if int(r_data) == 2:
            for axNo in range(2):
                # ---------------------------------------------------------
                # Setting unit and speed table
                # ---------------------------------------------------------
                serial_write(('AXI' + str(axNo + 1) + ':UNIT 0:SELSP 0' + '\r').encode('utf-8'))
                time.sleep(0.1)

        elif int(r_data) == 3:
            for axNo in range(3):
                # ---------------------------------------------------------
                # Setting unit and speed table
                # ---------------------------------------------------------
                serial_write(('AXI' + str(axNo + 1) + ':UNIT 0:SELSP 0' + '\r').encode('utf-8'))
                time.sleep(0.1)

        elif int(r_data) == 4:
            for axNo in range(4):
                # ---------------------------------------------------------
                # Setting unit and speed table
                # ---------------------------------------------------------
                serial_write(('AXI' + str(axNo + 1) + ':UNIT 0:SELSP 0' + '\r').encode('utf-8'))
                time.sleep(0.1)

        elif int(r_data) == 5:
            for axNo in range(5):
                # ---------------------------------------------------------
                # Setting unit and speed table
                # ---------------------------------------------------------
                serial_write(('AXI' + str(axNo + 1) + ':UNIT 0:SELSP 0' + '\r').encode('utf-8'))
                time.sleep(0.1)

        elif int(r_data) == 6:
            for axNo in range(6):
                # ---------------------------------------------------------
                # Setting unit and speed table
                # ---------------------------------------------------------
                serial_write(('AXI' + str(axNo + 1) + ':UNIT 0:SELSP 0' + '\r').encode('utf-8'))
                time.sleep(0.1)
        update_status()
    else:
        ser.close()

# Send comment
def serial_write(write_data):
    if ser.isOpen():
        try: 
            ser.write(write_data)
            logging.info(f"Send Command: {write_data}")
        except Exception as e:
            logging.error(f"Write Error: {e}")


# Send and receive
def serial_write_read(write_data):
    if ser.isOpen():
        try:
            ser.write(write_data)
            logging.info(f"TX: {write_data}")
            time.sleep(0.1)
            read_data = ser.read_until(b'\r')
            logging.info(f"RX: {read_data}")
            return read_data
        except Exception as e:
            logging.error(f"Read/Write Error: {e}")
            return ""
        

def main():
    connecet_port()

if __name__ == '__main__':
    main()
