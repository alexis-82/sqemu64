"""Validazione di tutto ciò che arriva dall'utente o dai file di configurazione."""

import re

DISK_FORMATS = ("qcow2", "raw", "vmdk", "vdi", "vpc")
DISK_INTERFACES = ("ide", "virtio", "scsi")
NICS = ("e1000", "virtio-net-pci", "rtl8139")
VGAS = ("std", "qxl", "virtio", "vmware", "cirrus", "none")
AUDIO_DRIVERS = ("none", "pa", "pipewire", "alsa", "sdl")
DISPLAYS = ("serial", "window")

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_SIZE = re.compile(r"[1-9][0-9]*(\.[0-9]+)?[KMGT]")
_RAM = re.compile(r"[1-9][0-9]*[MG]")
_MAC = re.compile(r"([0-9a-f]{2}:){5}[0-9a-f]{2}")
_INTERFACE = re.compile(r"[A-Za-z0-9_.-]{1,15}")

_UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


class SqemuError(Exception):
    """Errore previsto: viene mostrato all'utente senza traceback."""


def name(value):
    if not _NAME.fullmatch(value):
        raise SqemuError(
            f"Nome non valido: {value!r}. Usa lettere, numeri, '.', '_' e '-' "
            "(massimo 64 caratteri, iniziando con una lettera o un numero)."
        )
    return value


def size(value):
    value = value.strip().upper()
    if not _SIZE.fullmatch(value):
        raise SqemuError(f"Dimensione non valida: {value!r}. Esempi: 512M, 8G, 1.5T.")
    return value


def size_to_bytes(value):
    value = size(value)
    return int(float(value[:-1]) * _UNITS[value[-1]])


def ram(value):
    value = value.strip().upper()
    if not _RAM.fullmatch(value):
        raise SqemuError(f"RAM non valida: {value!r}. Esempi: 512M, 4G.")
    return value


def cpus(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise SqemuError(f"Numero di CPU non valido: {value!r}.") from None
    if not 1 <= number <= 1024:
        raise SqemuError(f"Numero di CPU non valido: {value!r}.")
    return number


def port(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise SqemuError(f"Porta non valida: {value!r}.") from None
    if not 1024 <= number <= 65535:
        raise SqemuError(f"Porta non valida: {value!r}. Usa un valore tra 1024 e 65535.")
    return number


def choice(value, allowed, label):
    if value not in allowed:
        raise SqemuError(f"{label} non valido: {value!r}. Valori ammessi: {', '.join(allowed)}.")
    return value


def mac(value):
    if not _MAC.fullmatch(value):
        raise SqemuError(f"Indirizzo MAC non valido: {value!r}.")
    return value


def network(value):
    """Accetta 'user', 'none', 'tap:<interfaccia>' oppure 'bridge:<bridge>'."""
    if value in ("user", "none"):
        return value
    kind, _, interface = value.partition(":")
    if kind in ("tap", "bridge") and _INTERFACE.fullmatch(interface):
        return value
    raise SqemuError(
        f"Rete non valida: {value!r}. Valori ammessi: user, none, tap:<interfaccia>, bridge:<bridge>."
    )
