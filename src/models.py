from dataclasses import dataclass


@dataclass
class DeviceInfo:
    device_id: str
    port: str
    label: str
    chip_name: str = "未识别"
    features: str = ""
    mac: str = ""
    flash_size: str = ""
    crystal: str = ""
    connected: bool = True
    serial_number: str = ""
    hwid: str = ""
