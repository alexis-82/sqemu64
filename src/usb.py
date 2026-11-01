"""Selezione sicura delle chiavette USB da collegare a una VM."""

import getpass
import json
import os
from dataclasses import dataclass

from . import runner
from .validate import SqemuError

LSBLK = ["lsblk", "-J", "-b", "-o", "NAME,PATH,TRAN,TYPE,SIZE,MODEL,MOUNTPOINTS"]


@dataclass
class UsbDisk:
    path: str
    size: int
    model: str
    mounted: list[str]

    def __str__(self):
        gigabytes = self.size / 1024**3
        state = "  [montata]" if self.mounted else ""
        return f"{self.path}  {gigabytes:.1f} GiB  {self.model}{state}"


def _mountpoints(device):
    found = [point for point in device.get("mountpoints") or [] if point]
    for child in device.get("children") or []:
        found += _mountpoints(child)
    return found


def parse_lsblk(output):
    """Tiene solo i dischi USB con un supporto inserito (i lettori vuoti hanno size 0)."""
    disks = []
    for device in json.loads(output).get("blockdevices", []):
        if device.get("type") == "disk" and device.get("tran") == "usb" and device.get("size"):
            disks.append(
                UsbDisk(
                    path=device["path"],
                    size=int(device["size"]),
                    model=(device.get("model") or "").strip() or "USB",
                    mounted=_mountpoints(device),
                )
            )
    return disks


def list_disks():
    return parse_lsblk(runner.capture(LSBLK))


def check_selection(device, disks):
    """Accetta solo un disco USB smontato: mai un disco interno dell'host."""
    path = device if device.startswith("/") else f"/dev/{device}"
    for disk in disks:
        if disk.path == path:
            if disk.mounted:
                raise SqemuError(f"{path} è montata in {', '.join(disk.mounted)}. Smontala prima di usarla.")
            return path
    raise SqemuError(f"{path} non è una chiavetta USB collegata. Usa: sqemu64 usb")


def select(device):
    path = check_selection(device, list_disks())
    if not os.access(path, os.R_OK | os.W_OK):
        raise SqemuError(
            f"Non hai i permessi su {path}. Per concederli fino alla rimozione della chiavetta:\n"
            f"  sudo setfacl -m u:{getpass.getuser()}:rw {path}"
        )
    return path
