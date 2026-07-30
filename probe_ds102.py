"""DS102/DS112 連線探測工具。

用途:
  1. 列出所有序列埠，含 VID/PID，標示哪一個是 DS102/DS112
  2. 對候選埠輪詢 *IDN?，找出真正會回應的埠與鮑率

DS102/DS112 透過 USB 連接時是駿河精機自訂 VID/PID 的 FTDI 晶片
(VID 0x0DFD / PID 0x0002)。透過 RS-232C 轉接線時 VID/PID 會是轉接
線本身的，這時只能靠 *IDN? 探測。

用法:
    python probe_ds102.py          # 列出 + 探測
    python probe_ds102.py --list   # 只列出，不送任何指令
"""

import sys
import time

import serial
import serial.tools.list_ports

# 駿河精機 DS102/DS112 的 USB 識別碼
DS_VID = 0x0DFD
DS_PID = 0x0002

# 依 DS102 手冊，RS-232C/USB 可設定的鮑率，由最常見的預設值開始試
BAUDRATES = [38400, 19200, 9600, 4800]

# 這些埠不是儀器，探測時直接跳過。Intel AMT SOL 是主機板上的
# 管理用虛擬埠，開得起來但永遠不會回應。
SKIP_KEYWORDS = ('Active Management Technology', 'AMT', 'Bluetooth')

IDN_PREFIX = 'SURUGA,DS1'


def list_ports():
    """回傳 (ports, ds_ports)：全部埠，以及靠 VID/PID 認出的 DS102/DS112。"""
    ports = list(serial.tools.list_ports.comports())
    ds_ports = [p for p in ports if p.vid == DS_VID and p.pid == DS_PID]

    if not ports:
        print('沒有偵測到任何序列埠。')
        return ports, ds_ports

    print(f'偵測到 {len(ports)} 個序列埠:\n')
    for p in ports:
        vidpid = f'{p.vid:04X}:{p.pid:04X}' if p.vid is not None else '   -     '
        tags = []
        if p in ds_ports:
            tags.append('<<< DS102/DS112')
        if _should_skip(p):
            tags.append('(探測時跳過)')
        print(f'  {p.device:<8} {vidpid:<10} {p.description}')
        print(f'           {p.hwid}')
        if tags:
            print(f'           {" ".join(tags)}')
        print()

    return ports, ds_ports


def _should_skip(port):
    text = f'{port.description} {port.hwid}'
    return any(k.lower() in text.lower() for k in SKIP_KEYWORDS)


def probe(device, baudrate, timeout=1.0):
    """對單一埠送 *IDN?，回傳收到的原始 bytes（失敗回 None）。"""
    try:
        with serial.Serial(device, baudrate, timeout=timeout) as ser:
            ser.reset_input_buffer()
            ser.write(b'*IDN?\r')
            time.sleep(0.1)
            return ser.read_until(b'\r')
    except (serial.SerialException, OSError) as exc:
        print(f'    {device} @ {baudrate}: 開啟失敗 - {exc}')
        return None


def find_controller(ports, ds_ports):
    """輪詢候選埠與鮑率，回傳第一個回應 SURUGA,DS1 的 (device, baudrate)。"""
    # 靠 VID/PID 認出來的優先，其餘的（例如 RS-232C 轉接線）再試
    candidates = ds_ports + [p for p in ports
                             if p not in ds_ports and not _should_skip(p)]

    if not candidates:
        print('沒有可探測的候選埠。')
        return None

    print('開始探測 *IDN? ...\n')
    for port in candidates:
        for baudrate in BAUDRATES:
            reply = probe(port.device, baudrate)
            if reply is None:
                break  # 這個埠開不起來，換下一個埠
            shown = repr(reply) if reply else '(無回應)'
            print(f'    {port.device} @ {baudrate:<6} -> {shown}')
            if IDN_PREFIX in reply.decode('ascii', errors='replace'):
                print(f'\n找到了: {port.device} @ {baudrate}')
                return port.device, baudrate

    print('\n所有候選埠都沒有回應。')
    return None


def main():
    only_list = '--list' in sys.argv

    ports, ds_ports = list_ports()

    if ds_ports:
        print(f'靠 VID/PID 認出 DS102/DS112: '
              f'{", ".join(p.device for p in ds_ports)}\n')
    else:
        print(f'沒有任何埠的 VID/PID 是 {DS_VID:04X}:{DS_PID:04X}，'
              f'表示 USB 驅動還沒正常運作，\n'
              f'或是你走 RS-232C（那就靠下面的 *IDN? 探測）。\n')

    if only_list:
        return 0

    result = find_controller(ports, ds_ports)
    if result is None:
        return 1

    device, baudrate = result
    print(f'\n把 test.py 的連線設定改成: port={device}, baudrate={baudrate}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
