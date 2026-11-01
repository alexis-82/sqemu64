"""Operazioni sui dischi. Il formato è sempre esplicito e nulla viene sovrascritto."""

import json
import os
import shutil
from pathlib import Path

from . import config, runner, validate
from .validate import SqemuError

EXTENSIONS = {"qcow2": ".qcow2", "raw": ".img", "vmdk": ".vmdk", "vdi": ".vdi", "vpc": ".vhd"}
SOURCE_FORMATS = (*validate.DISK_FORMATS, "dmg")


def resolve(value, default_format=None):
    """Un nome semplice indica la cartella dei dischi, altrimenti è un percorso.

    Il percorso restituito è sempre assoluto: non può essere scambiato per
    un'opzione o per un protocollo di QEMU (per esempio 'nbd:' o 'fat:').
    """
    if "/" in value:
        return str(Path(value).expanduser().resolve())
    validate.name(value)
    suffix = EXTENSIONS.get(default_format, "")
    if suffix and not value.endswith(suffix):
        candidate = config.disks_dir() / (value + suffix)
    else:
        candidate = config.disks_dir() / value
    return str(candidate)


def resolve_existing(value):
    """Come resolve(), ma cerca un disco esistente provando le estensioni note."""
    path = resolve(value)
    if os.path.exists(path):
        return path
    if "/" not in value:
        for suffix in EXTENSIONS.values():
            if os.path.exists(path + suffix):
                return path + suffix
    raise SqemuError(f"Disco non trovato: {value}.")


def _require_free(path):
    if os.path.lexists(path):
        raise SqemuError(f"Il file esiste già e non verrà sovrascritto: {path}.")
    os.makedirs(os.path.dirname(path), exist_ok=True)


def info(path):
    output = runner.capture(["qemu-img", "info", "--output=json", path])
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        raise SqemuError(f"Risposta non valida di qemu-img per {path}.") from None


def detect_format(path):
    details = info(path)
    disk_format = details.get("format")
    if disk_format not in SOURCE_FORMATS:
        raise SqemuError(f"Formato non supportato per {path}: {disk_format!r}.")
    if details.get("backing-filename"):
        print(
            f"Attenzione: {path} dipende dal file {details['backing-filename']!r}. "
            "Usalo solo se ti fidi di questa immagine."
        )
    return disk_format


def create(value, disk_size, disk_format="qcow2"):
    validate.choice(disk_format, validate.DISK_FORMATS, "Formato del disco")
    disk_size = validate.size(disk_size)
    path = resolve(value, disk_format)
    _require_free(path)
    runner.run(["qemu-img", "create", "-f", disk_format, path, disk_size])
    return path


def _dmg_argv(source, target):
    if shutil.which("dmg2img"):
        return ["dmg2img", "-i", source, "-o", target]
    return ["qemu-img", "convert", "-p", "-f", "dmg", "-O", "raw", source, target]


def convert(source_value, target_value, target_format, source_format=None):
    validate.choice(target_format, validate.DISK_FORMATS, "Formato di destinazione")
    source = resolve_existing(source_value)
    target = resolve(target_value, target_format)
    if source_format is None:
        source_format = "dmg" if source.lower().endswith(".dmg") else detect_format(source)
    validate.choice(source_format, SOURCE_FORMATS, "Formato di origine")
    _require_free(target)

    if source_format == "dmg":
        if target_format != "raw":
            raise SqemuError("Un'immagine DMG può essere convertita solo in formato raw.")
        runner.run(_dmg_argv(source, target))
    else:
        runner.run(["qemu-img", "convert", "-p", "-f", source_format, "-O", target_format, source, target])
    return target


def resize(value, new_size, shrink=False):
    path = resolve_existing(value)
    new_size = validate.size(new_size)
    details = info(path)
    disk_format = details.get("format")
    validate.choice(disk_format, validate.DISK_FORMATS, "Formato del disco")

    is_smaller = validate.size_to_bytes(new_size) < details.get("virtual-size", 0)
    if is_smaller and not shrink:
        raise SqemuError(
            "La nuova dimensione è inferiore a quella attuale e può distruggere dati. "
            "Riduci prima le partizioni nel sistema ospite, poi conferma la riduzione."
        )
    argv = ["qemu-img", "resize", "-f", disk_format]
    if is_smaller:
        argv.append("--shrink")
    runner.run([*argv, path, new_size])
    return path


def clone(source_value, target_value):
    """Usa qemu-img e non cp: rifiuta i dischi in uso da una VM accesa."""
    source = resolve_existing(source_value)
    disk_format = detect_format(source)
    validate.choice(disk_format, validate.DISK_FORMATS, "Formato del disco")
    target = resolve(target_value, disk_format)
    _require_free(target)
    runner.run(["qemu-img", "convert", "-p", "-f", disk_format, "-O", disk_format, source, target])
    return target


def list_files():
    if not config.disks_dir().is_dir():
        return []
    return sorted(p for p in config.disks_dir().iterdir() if p.is_file())
