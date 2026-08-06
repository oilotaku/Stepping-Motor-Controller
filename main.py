from typing import Literal

import serial
import serial.tools.list_ports
import time
import threading
import tkinter as tk
import tkinter.ttk as ttk
from tkinter import messagebox
import logging
from datetime import datetime

axisNo = "1"  # Axis number
direction = "CCW"  # Drive direction setting(-(CCW)、+(CW))
mode = 0  # Drive mode (0: Continue, 1: Step, 2: Origin)
ser = serial.Serial()


# 設定 Log 格式與存檔路徑
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(f"ds102_log_{datetime.now().strftime('%Y%m%d')}.log"),
        logging.StreamHandler(),  # 同時輸出到終端機
    ],
)


# processing : when press the connect button
def connect_button_click(event) -> None:
    root.after(10, comm_port_open)


# processing : set COM port
def comm_port_open() -> None:
    global ser

    if ser.is_open:
        ser.close()

    try:
        ser = serial.Serial(cmbCommPort.get(), cmbBaudrate.get(), timeout=2)
    except serial.SerialException:
        ser.close()
        lblState["text"] = "Connection error"
        root.after(1, showerror("Connection error"))
        logging.error(f"Write Error: {serial.SerialException}")
        return

    # ---------------------------------------------------------
    # Request ID
    # Check DS102 / DS112 controller connection
    # ---------------------------------------------------------
    r_data = serial_write_read(("*IDN?" + "\r").encode("utf-8"))

    if "SURUGA,DS1" in str(r_data):
        # ---------------------------------------------------------
        # Request version
        # ---------------------------------------------------------
        r_data = serial_write_read(("DS102VER?" + "\r").encode("utf-8"))
        lblFirmware["text"] = r_data

        # ---------------------------------------------------------
        # Number of control axes
        # ---------------------------------------------------------
        r_data = serial_write_read(("CONTA?" + "\r").encode("utf-8"))

        btnAxisX["state"] = "disabled"
        btnAxisY["state"] = "disabled"
        btnAxisZ["state"] = "disabled"
        btnAxisU["state"] = "disabled"
        btnAxisV["state"] = "disabled"
        btnAxisW["state"] = "disabled"

        # Number of axes is 2(X,Y)
        if int(r_data) == 2:
            for axNo in range(2):
                # ---------------------------------------------------------
                # Setting unit and speed table
                # ---------------------------------------------------------
                serial_write(
                    ("AXI" + str(axNo + 1) + ":UNIT 0:SELSP 0" + "\r").encode("utf-8")
                )
                time.sleep(0.1)

            btnAxisX["state"] = "normal"
            btnAxisY["state"] = "normal"
        # Number of axes is 4(X-U)
        elif int(r_data) == 4:
            for axNo in range(4):
                # ---------------------------------------------------------
                # Setting unit and speed table
                # ---------------------------------------------------------
                serial_write(
                    ("AXI" + str(axNo + 1) + ":UNIT 0:SELSP 0" + "\r").encode("utf-8")
                )
                time.sleep(0.1)

            btnAxisX["state"] = "normal"
            btnAxisY["state"] = "normal"
            btnAxisZ["state"] = "normal"
            btnAxisU["state"] = "normal"
        # Number of axes is 6(X-W)
        else:
            for axNo in range(6):
                # ---------------------------------------------------------
                # Setting unit and speed table
                # ---------------------------------------------------------
                serial_write(
                    ("AXI" + str(axNo + 1) + ":UNIT 0:SELSP 0" + "\r").encode("utf-8")
                )
                time.sleep(0.1)

            btnAxisX["state"] = "normal"
            btnAxisY["state"] = "normal"
            btnAxisZ["state"] = "normal"
            btnAxisU["state"] = "normal"
            btnAxisV["state"] = "normal"
            btnAxisW["state"] = "normal"

        update_status()
    else:
        ser.close()
        lblState["text"] = "Receive error"
        root.after(
            1,
            showerror(
                "DS102/DS112 controller is not connected to " + cmbCommPort.get()
            ),
        )


# processing : when press the disconnect button
def disconnect_button_click(event) -> None:
    # Init Serial Port Setting
    global ser
    if ser.is_open:
        ser.close()

    btnAxisX["state"] = "normal"
    btnAxisY["state"] = "normal"
    btnAxisZ["state"] = "normal"
    btnAxisU["state"] = "normal"
    btnAxisV["state"] = "normal"
    btnAxisW["state"] = "normal"

    lblState["text"] = "Disconnected"


# processing : when select the X button
def axis_x_button_click() -> None:
    global axisNo
    axisNo = "1"
    btnAxisX.config(bg="LightBlue")
    btnAxisY.config(bg="SystemButtonFace")
    btnAxisZ.config(bg="SystemButtonFace")
    btnAxisU.config(bg="SystemButtonFace")
    btnAxisV.config(bg="SystemButtonFace")
    btnAxisW.config(bg="SystemButtonFace")

    update_status()


# processing : when select the Y button
def axis_y_button_click() -> None:
    global axisNo
    axisNo = "2"
    btnAxisX.config(bg="SystemButtonFace")
    btnAxisY.config(bg="LightBlue")
    btnAxisZ.config(bg="SystemButtonFace")
    btnAxisU.config(bg="SystemButtonFace")
    btnAxisV.config(bg="SystemButtonFace")
    btnAxisW.config(bg="SystemButtonFace")

    update_status()


# processing : when select the Z button
def axis_z_button_click() -> None:
    global axisNo
    axisNo = "3"
    btnAxisX.config(bg="SystemButtonFace")
    btnAxisY.config(bg="SystemButtonFace")
    btnAxisZ.config(bg="LightBlue")
    btnAxisU.config(bg="SystemButtonFace")
    btnAxisV.config(bg="SystemButtonFace")
    btnAxisW.config(bg="SystemButtonFace")

    update_status()


# processing : when select the U button
def axis_u_button_click() -> None:
    global axisNo
    axisNo = "4"
    btnAxisX.config(bg="SystemButtonFace")
    btnAxisY.config(bg="SystemButtonFace")
    btnAxisZ.config(bg="SystemButtonFace")
    btnAxisU.config(bg="LightBlue")
    btnAxisV.config(bg="SystemButtonFace")
    btnAxisW.config(bg="SystemButtonFace")

    update_status()


# processing : when select the V button
def axis_v_button_click() -> None:
    global axisNo
    axisNo = "5"
    btnAxisX.config(bg="SystemButtonFace")
    btnAxisY.config(bg="SystemButtonFace")
    btnAxisZ.config(bg="SystemButtonFace")
    btnAxisU.config(bg="SystemButtonFace")
    btnAxisV.config(bg="LightBlue")
    btnAxisW.config(bg="SystemButtonFace")

    update_status()


# processing : when select the W button
def axis_w_button_click() -> None:
    global axisNo
    axisNo = "6"
    btnAxisX.config(bg="SystemButtonFace")
    btnAxisY.config(bg="SystemButtonFace")
    btnAxisZ.config(bg="SystemButtonFace")
    btnAxisU.config(bg="SystemButtonFace")
    btnAxisV.config(bg="SystemButtonFace")
    btnAxisW.config(bg="LightBlue")

    update_status()


# processing : when select the continue button
def continue_mode() -> None:
    global mode
    btnCCW["text"] = "- (CCW)"
    btnCW["text"] = "+ (CW)"
    mode = var.get()


# processing : when select the step button
def step_mode() -> None:
    global mode
    btnCCW["text"] = "- (CCW)"
    btnCW["text"] = "+ (CW)"
    mode = var.get()


# processing : when select the origin button
def org_mode() -> None:
    global mode
    btnCCW["text"] = "Origin"
    btnCW["text"] = "Origin"
    mode = var.get()


# processing : when press the stop button
def stop_button_click(event) -> None:
    # ---------------------------------------------------------
    # Stop
    # ---------------------------------------------------------
    serial_write(("STOP 0" + "\r").encode("utf-8"))


# processing : when press the CCW button
def ccw_button_press(event) -> None:
    global direction
    direction = "CCW"
    move_stage()


# processing : when release the CCW button
def ccw_button_release(event) -> None:
    global mode
    if mode == 0:
        # ---------------------------------------------------------
        # Stop
        # ---------------------------------------------------------
        serial_write(("STOP 0" + "\r").encode("utf-8"))


# processing : when press the CW button
def cw_button_press(event) -> None:
    global direction
    direction = "CW"
    move_stage()


# processing : when release the CW button
def cw_button_release(event) -> None:
    global mode
    if mode == 0:
        # ---------------------------------------------------------
        # Stop
        # ---------------------------------------------------------
        serial_write(("STOP 0" + "\r").encode("utf-8"))


# Drive the stage
def move_stage() -> None:
    global axisNo
    global direction
    global mode

    # Continue
    if mode == 0:
        if direction == "CW":
            # ---------------------------------------------------------
            # Continue drive / CW direction
            # ---------------------------------------------------------
            serial_write(
                (
                    "AXI"
                    + axisNo
                    + ":L0 "
                    + txtLSpeed.get()
                    + ":R0 "
                    + txtRate.get()
                    + ":S0 "
                    + txtSRate.get()
                    + ":F0 "
                    + txtSpeed.get()
                    + ":GO CWJ"
                    + "\r"
                ).encode("utf-8")
            )
        else:
            # ---------------------------------------------------------
            # Continue drive / CCW direction
            # ---------------------------------------------------------
            serial_write(
                (
                    "AXI"
                    + axisNo
                    + ":L0 "
                    + txtLSpeed.get()
                    + ":R0 "
                    + txtRate.get()
                    + ":S0 "
                    + txtSRate.get()
                    + ":F0 "
                    + txtSpeed.get()
                    + ":GO CCWJ"
                    + "\r"
                ).encode("utf-8")
            )
    # Step
    elif mode == 1:
        # ---------------------------------------------------------
        # Step drive
        # ---------------------------------------------------------
        serial_write(
            (
                "AXI"
                + axisNo
                + ":L0 "
                + txtLSpeed.get()
                + ":R0 "
                + txtRate.get()
                + ":S0 "
                + txtSRate.get()
                + ":F0 "
                + txtSpeed.get()
                + ":PULS "
                + txtStep.get()
                + ":GO "
                + direction
                + "\r"
            ).encode("utf-8")
        )
    # Origin
    elif mode == 2:
        # Change the origin type
        # ---------------------------------------------------------
        # Memory switch 0 setting
        # ---------------------------------------------------------
        serial_write(
            ("AXI" + axisNo + ":MEMSW0 " + str(cmbOrgMode.current()) + "\r").encode(
                "utf-8"
            )
        )
        time.sleep(0.1)
        # ---------------------------------------------------------
        # Origin drive
        # ---------------------------------------------------------
        serial_write(
            (
                "AXI"
                + axisNo
                + ":L0 "
                + txtLSpeed.get()
                + ":R0 "
                + txtRate.get()
                + ":S0 "
                + txtSRate.get()
                + ":F0 "
                + txtSpeed.get()
                + ":GO ORG"
                + "\r"
            ).encode("utf-8")
        )

    timer = threading.Timer(0.1, get_status)
    timer.start()


# processing : Screen update
def get_status() -> None:
    if update_status() == "run":
        timer = threading.Timer(0.1, get_status)
        timer.start()


# processing : Status update
def update_status() -> Literal['Stop'] | Literal['run']:
    # ---------------------------------------------------------
    # Request status binary 3
    # ---------------------------------------------------------
    r_data = serial_write_read(("AXI" + axisNo + ":SB3?" + "\r").encode("utf-8"))

    # Terminates if it cannot be converted to a numerical value
    try:
        if r_data is not None:
            int(r_data)
        else:
            status = "Stop"
            return status
    except ValueError:
        status = "Stop"
        return status

    if not int(r_data) & 0x01 == 0x01:
        lblState["text"] = "Axes cannot be selected"
        status = "Stop"
        return status
    else:
        # ---------------------------------------------------------
        # Request status binary 1
        # ---------------------------------------------------------
        r_data = serial_write_read(("AXI" + axisNo + ":SB1?" + "\r").encode("utf-8"))

        if int(r_data) & 0x40 == 0x40:
            lblState["text"] = "Driving"
            status = "run"
        elif int(r_data) & 0x10 == 0x10:
            lblState["text"] = "Detect origin"
            status = "Stop"
        elif int(r_data) & 0x02 == 0x02 or int(r_data) & 0x04 == 0x04:
            # Detect limit
            # ---------------------------------------------------------
            # Request status binary 2
            # ---------------------------------------------------------
            r_data = serial_write_read(
                ("AXI" + axisNo + ":SB2?" + "\r").encode("utf-8")
            )
            if int(r_data) & 0x03 == 0x03:
                lblState["text"] = "Stage not connected"
            elif int(r_data) & 0x01 == 0x01:
                lblState["text"] = "Detect CW limit"
            elif int(r_data) & 0x02 == 0x02:
                lblState["text"] = "Detect CCW limit"
            elif int(r_data) & 0x04 == 0x04:
                lblState["text"] = "Detect CW software limit"
            elif int(r_data) & 0x08 == 0x08:
                lblState["text"] = "Detect CCW software limit"
            status = "Stop"
        else:
            lblState["text"] = "Stop"
            status = "Stop"

        # ---------------------------------------------------------
        # Request the current position
        # ---------------------------------------------------------
        r_data = serial_write_read(("AXI" + axisNo + ":POS?" + "\r").encode("utf-8"))
        txtPosition.delete(0, tk.END)
        txtPosition.insert(tk.END, r_data)

        return status


# Send
def serial_write(write_data) -> None:
    if ser.isOpen():
        try:
            ser.write(write_data)
            logging.info(f"Send Command: {write_data}")
        except Exception as e:
            logging.error(f"Write Error: {e}")


# Send and receive
def serial_write_read(write_data) -> bytes | Literal['']:
    if ser.isOpen():
        try:
            ser.write(write_data)
            logging.info(f"TX: {write_data}")
            time.sleep(0.1)
            read_data = ser.read_until(b"\r")
            logging.info(f"RX: {read_data}")
            return read_data
        except Exception as e:
            logging.error(f"Read/Write Error: {e}")
            return ""
    return ""


# processing : when press the position set button
def position_button_click(event) -> None:
    # ---------------------------------------------------------
    # Set the current position
    # ---------------------------------------------------------
    serial_write(("AXI" + axisNo + ":POS " + txtPosition.get() + "\r").encode("utf-8"))


# processing : when press the close button
def close_button_click(event) -> None:
    global ser
    if ser.isOpen():
        ser.close()

    root.destroy()


def showerror(msg) -> None:
    messagebox.showerror("Error", msg)


# main
if __name__ == "__main__":
    # Window
    root = tk.Tk()
    root.title("Sample program for DS102/DS112 controller Ver.1.0.0")
    root.geometry("580x340")

    # Target axes
    btnAxisX = tk.Button(text="X", width=3, height=1, command=axis_x_button_click)
    btnAxisX.place(x=10, y=10)

    btnAxisY = tk.Button(text="Y", width=3, height=1, command=axis_y_button_click)
    btnAxisY.place(x=45, y=10)

    btnAxisZ = tk.Button(text="Z", width=3, height=1, command=axis_z_button_click)
    btnAxisZ.place(x=80, y=10)

    btnAxisU = tk.Button(text="U", width=3, height=1, command=axis_u_button_click)
    btnAxisU.place(x=115, y=10)

    btnAxisV = tk.Button(text="V", width=3, height=1, command=axis_v_button_click)
    btnAxisV.place(x=150, y=10)

    btnAxisW = tk.Button(text="W", width=3, height=1, command=axis_w_button_click)
    btnAxisW.place(x=185, y=10)

    lblFirmware = tk.Label(text="DS102")
    lblFirmware.place(x=310, y=15)

    btnClose = tk.Button(text="Close", width=10, height=1)
    btnClose.bind("<Button-1>", close_button_click)
    btnClose.place(x=476, y=10)

    # Speed setting
    frameDriveSetting = tk.LabelFrame(root, text="Speed setting", width=275, height=165)
    frameDriveSetting.pack(fill=tk.BOTH, expand=True)
    frameDriveSetting.place(x=10, y=50)
    frameDriveSetting.propagate(False)

    frameLSpeed = tk.Frame(frameDriveSetting)
    frameLSpeed.pack(fill=tk.BOTH, expand=True)

    lblLSpeed = tk.Label(frameLSpeed, text="Start-up Speed(L) :", width=16, anchor="w")
    lblLSpeed.pack(side=tk.LEFT, padx=3, pady=6)

    txtLSpeed = tk.Entry(frameLSpeed, width=16)
    txtLSpeed.insert(tk.END, "100")
    txtLSpeed.pack(side=tk.LEFT, padx=4, pady=6)

    lblLSpeedUnit = tk.Label(frameLSpeed, text="pps")
    lblLSpeedUnit.pack(side=tk.LEFT, padx=1, pady=6)

    frameLRate = tk.Frame(frameDriveSetting)
    frameLRate.pack(fill=tk.BOTH, expand=True)

    lblRate = tk.Label(
        frameLRate,
        text="Acceleration/" "\nDeceleration rate(R) :",
        width=16,
        anchor="w",
        justify="left",
        wraplength=180,
    )
    lblRate.pack(side=tk.LEFT, padx=3, pady=0, anchor="w")

    txtRate = tk.Entry(frameLRate, width=16)
    txtRate.insert(tk.END, "100")
    txtRate.pack(side=tk.LEFT, padx=4, pady=0)

    lblRateUnit = tk.Label(frameLRate, text="ms")
    lblRateUnit.pack(side=tk.LEFT, padx=1, pady=0)

    frameSRate = tk.Frame(frameDriveSetting)
    frameSRate.pack(fill=tk.BOTH, expand=True)

    lblSRate = tk.Label(
        frameSRate,
        text="S-curve Acceleration/" "\nDeceleration rate(S) :",
        width=16,
        anchor="w",
        justify="left",
        wraplength=180,
    )
    lblSRate.pack(side=tk.LEFT, padx=3, pady=0)

    txtSRate = tk.Entry(frameSRate, width=16)
    txtSRate.insert(tk.END, "100")
    txtSRate.pack(side=tk.LEFT, padx=4, pady=0)

    lblSRateUnit = tk.Label(frameSRate, text="%")
    lblSRateUnit.pack(side=tk.LEFT, padx=1, pady=0)

    frameSpeed = tk.Frame(frameDriveSetting)
    frameSpeed.pack(fill=tk.BOTH, expand=True)

    lblSpeed = tk.Label(frameSpeed, text="Driving Speed(F) :", width=16, anchor="w")
    lblSpeed.pack(side=tk.LEFT, padx=3, pady=8)

    txtSpeed = tk.Entry(frameSpeed, width=16)
    txtSpeed.insert(tk.END, "1000")
    txtSpeed.pack(side=tk.LEFT, padx=4, pady=8)

    lblSpeedUnit = tk.Label(frameSpeed, text="pps")
    lblSpeedUnit.pack(side=tk.LEFT, padx=1, pady=8)

    # Drive mode select
    frameDriveSelect = tk.LabelFrame(root, text="Drive mode", width=275, height=105)
    frameDriveSelect.pack(fill=tk.BOTH, expand=True)
    frameDriveSelect.place(x=10, y=225)
    frameDriveSelect.propagate(False)

    frameContinueMode = tk.Frame(frameDriveSelect)
    frameContinueMode.pack(fill=tk.BOTH, expand=False)

    var = tk.IntVar()
    var.set(0)

    rbnContinueMode = tk.Radiobutton(
        frameContinueMode,
        text="Continue",
        value="0",
        variable=var,
        command=continue_mode,
        width=10,
        anchor="w",
    )
    rbnContinueMode.pack(side=tk.LEFT, padx=3, pady=1)

    frameStepMode = tk.Frame(frameDriveSelect)
    frameStepMode.pack(fill=tk.BOTH, expand=False)

    rbnStepMode = tk.Radiobutton(
        frameStepMode,
        text="Step",
        value="1",
        variable=var,
        command=step_mode,
        width=13,
        anchor="w",
    )
    rbnStepMode.pack(side=tk.LEFT, padx=3, pady=1)

    txtStep = tk.Entry(frameStepMode, width=16)
    txtStep.insert(tk.END, "1000")
    txtStep.pack(side=tk.LEFT, padx=3, pady=1)

    lblStepUnit = tk.Label(frameStepMode, text="Pulse")
    lblStepUnit.pack(side=tk.LEFT, padx=4, pady=1)

    frameOrgMode = tk.Frame(frameDriveSelect)
    frameOrgMode.pack(fill=tk.BOTH, expand=False)

    rbnOrgMode = tk.Radiobutton(
        frameOrgMode,
        text="Origin",
        value="2",
        variable=var,
        command=org_mode,
        width=13,
        anchor="w",
    )
    rbnOrgMode.pack(side=tk.LEFT, padx=3, pady=4)

    org = [
        "ORG 0",
        "ORG 1",
        "ORG 2",
        "ORG 3",
        "ORG 4",
        "ORG 5",
        "ORG 6",
        "ORG 7",
        "ORG 8",
        "ORG 9",
        "ORG 10",
        "ORG 11",
        "ORG 12",
    ]
    orgList = tk.StringVar()
    cmbOrgMode = ttk.Combobox(frameOrgMode, values=org, textvariable=orgList, width=13)
    cmbOrgMode.pack(side=tk.LEFT, padx=3, pady=4)
    cmbOrgMode.set(org[0])

    # Communication setting
    frameConnection = tk.LabelFrame(
        root, text="Communication setting", width=270, height=115
    )
    frameConnection.pack(fill=tk.BOTH, expand=True)
    frameConnection.place(x=302, y=50)
    frameConnection.propagate(False)

    frameCommPort = tk.Frame(frameConnection)
    frameCommPort.pack(fill=tk.BOTH, expand=True)

    lblCommPort = tk.Label(frameCommPort, text="COM port :", width=10)
    lblCommPort.pack(side=tk.LEFT, padx=1, pady=0)

    port = [
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "COM10",
        "COM11",
        "COM12",
        "COM13",
        "COM14",
        "COM15",
        "COM16",
        "COM17",
        "COM18",
        "COM19",
        "COM20",
    ]
    portList = tk.StringVar()
    cmbCommPort = ttk.Combobox(
        frameCommPort, values=port, textvariable=portList, width=10
    )
    cmbCommPort.set(port[0])
    cmbCommPort.pack(side=tk.LEFT)

    btnConnect = tk.Button(frameCommPort, text="Connect", width=20, height=1)
    btnConnect.bind("<Button-1>", connect_button_click)
    btnConnect.pack(side=tk.LEFT, padx=12, pady=0)

    frameBaudrate = tk.Frame(frameConnection)
    frameBaudrate.pack(fill=tk.BOTH, expand=True)

    lblBaudrate = tk.Label(frameBaudrate, text="Baud Rate :", width=10, height=1)
    lblBaudrate.pack(side=tk.LEFT, padx=1, pady=0)

    baudrate = ["38400", "19200", "9600", "4800"]
    baudrateList = tk.StringVar()
    cmbBaudrate = ttk.Combobox(
        frameBaudrate, values=baudrate, textvariable=baudrateList, width=10
    )
    cmbBaudrate.set(baudrate[0])
    cmbBaudrate.pack(side=tk.LEFT)

    btnDisconnect = tk.Button(frameBaudrate, text="Disconnect", width=20, height=1)
    btnDisconnect.bind("<Button-1>", disconnect_button_click)
    btnDisconnect.pack(side=tk.LEFT, padx=12, pady=0)

    # Drive operation
    frameDrive = tk.LabelFrame(root, text="Drive operation", width=270, height=150)
    frameDrive.pack(fill=tk.BOTH, expand=True)
    frameDrive.place(x=302, y=180)
    frameDrive.propagate(False)

    framePosition = tk.Frame(frameDrive)
    framePosition.pack(fill=tk.BOTH, expand=True)

    lblPosition = tk.Label(framePosition, text="Position :", width=10, anchor="w")
    lblPosition.pack(side=tk.LEFT, padx=3, pady=8)

    txtPosition = tk.Entry(framePosition, width=13)
    txtPosition.insert(tk.END, "0")
    txtPosition.pack(side=tk.LEFT, padx=1, pady=0)

    btnPosition = tk.Button(framePosition, text="Set position", width=20, height=1)
    btnPosition.bind("<Button-1>", position_button_click)
    btnPosition.pack(side=tk.LEFT, padx=12, pady=0)

    frameStatus = tk.Frame(frameDrive)
    frameStatus.pack(fill=tk.BOTH, expand=True)

    lblStatus = tk.Label(frameStatus, text="Status :", width=10, anchor="w")
    lblStatus.pack(side=tk.LEFT, padx=3, pady=8)

    lblState = tk.Label(frameStatus, text="Disconnected")
    lblState.pack(side=tk.LEFT)

    frameRun = tk.Frame(frameDrive)
    frameRun.pack(fill=tk.BOTH, expand=True)

    btnCCW = tk.Button(frameRun, text="- (CCW)", width=10, height=2)
    btnCCW.bind("<ButtonPress-1>", ccw_button_press)
    btnCCW.bind("<ButtonRelease-1>", ccw_button_release)
    btnCCW.pack(side=tk.LEFT, padx=4, pady=8)

    btnStop = tk.Button(frameRun, text="Stop", width=10, height=2)
    btnStop.bind("<Button-1>", stop_button_click)
    btnStop.pack(side=tk.LEFT, padx=4, pady=8)

    btnCW = tk.Button(frameRun, text="+ (CW)", width=10, height=2)
    btnCW.bind("<ButtonPress-1>", cw_button_press)
    btnCW.bind("<ButtonRelease-1>", cw_button_release)
    btnCW.pack(side=tk.LEFT, padx=4, pady=8)

    root.mainloop()
