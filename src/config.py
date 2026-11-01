"""Percorsi dei dati e lettura/scrittura delle VM salvate."""

import json
import os
import secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import validate
from .validate import SqemuError

CONFIG_FILE = "vm.toml"


def data_dir():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "sqemu64"


def vms_dir():
    return data_dir() / "vms"


def disks_dir():
    return data_dir() / "disks"


def vm_dir(name):
    return vms_dir() / validate.name(name)


def random_mac():
    return "52:54:00:" + ":".join(secrets.token_hex(1) for _ in range(3))


@dataclass
class Disk:
    path: str
    format: str

    def __post_init__(self):
        if not os.path.isabs(self.path):
            raise SqemuError(f"Il percorso del disco deve essere assoluto: {self.path!r}.")
        validate.choice(self.format, validate.DISK_FORMATS, "Formato del disco")


@dataclass
class VM:
    name: str
    disks: list[Disk] = field(default_factory=list)
    ram: str = "2G"
    cpus: int = 2
    network: str = "user"
    nic: str = "e1000"
    vga: str = "std"
    disk_interface: str = "ide"
    audio: str = "none"
    display: str = "serial"
    mac: str = field(default_factory=random_mac)

    def __post_init__(self):
        validate.name(self.name)
        validate.ram(self.ram)
        validate.cpus(self.cpus)
        validate.network(self.network)
        validate.choice(self.nic, validate.NICS, "Scheda di rete")
        validate.choice(self.vga, validate.VGAS, "Scheda video")
        validate.choice(self.disk_interface, validate.DISK_INTERFACES, "Interfaccia disco")
        validate.choice(self.audio, validate.AUDIO_DRIVERS, "Driver audio")
        validate.choice(self.display, validate.DISPLAYS, "Schermo")
        validate.mac(self.mac)


def to_toml(vm):
    lines = []
    for key in ("name", "ram", "cpus", "network", "nic", "vga", "disk_interface", "audio", "display", "mac"):
        lines.append(f"{key} = {json.dumps(getattr(vm, key), ensure_ascii=False)}")
    for disk in vm.disks:
        lines += [
            "",
            "[[disks]]",
            f"path = {json.dumps(disk.path, ensure_ascii=False)}",
            f"format = {json.dumps(disk.format)}",
        ]
    return "\n".join(lines) + "\n"


def from_toml(text):
    try:
        data = tomllib.loads(text)
        data["disks"] = [Disk(**disk) for disk in data.get("disks", [])]
        return VM(**data)
    except (tomllib.TOMLDecodeError, TypeError) as error:
        raise SqemuError(f"Configurazione non valida: {error}") from None


def save(vm):
    """Scrive la configurazione in modo atomico, senza lasciare file a metà."""
    directory = vm_dir(vm.name)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / (CONFIG_FILE + ".tmp")
    temporary.write_text(to_toml(vm), encoding="utf-8")
    os.replace(temporary, directory / CONFIG_FILE)


def exists(name):
    return (vm_dir(name) / CONFIG_FILE).is_file()


def load(name):
    path = vm_dir(name) / CONFIG_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SqemuError(f"La VM {name!r} non esiste.") from None
    return from_toml(text)


def list_names():
    if not vms_dir().is_dir():
        return []
    return sorted(p.name for p in vms_dir().iterdir() if (p / CONFIG_FILE).is_file())
