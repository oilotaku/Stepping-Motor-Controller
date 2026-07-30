„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ
		"DS102PythonSample" Release note Ver1.0.0
„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ„Ÿ

# Definition
DS102PythonSample is a sample program created as a reference for controlling DS102/DS112 controllers using Python. 
It is designed in a simple form, extracting the essential parts without introducing complexity. 
While you are free to use the sample, 
please note that we do not take responsibility for the content and operation of your program.

# Overview
After launching the program, select the COM port to connect to the DS102/DS112 controller from the "Communication Port." 
Press the "Connect" button to establish the connection. 
Press the "- (CCW)" or "+ (CW)" buttons to send drive commands to the DS102/DS112 controller and drive the stage. 
For convenience, drive speed is set each time, but once set, there is no need to configure it every time. 
While driving, receive status from the DS102/DS112 controller and display the drive state in the "Status" section.

# Folder Structure
DS102PythonSample
„¥ main.py		Source file
„¤ Readme_Python.txt

# Development Environment
Windows10
Python 3.11 64bit
pyserial 3.5

## License
2023 SURUGA SEIKI CO., LTD. All rights reserved.