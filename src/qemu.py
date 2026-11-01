"""Costruzione della riga di comando di QEMU.

Questo modulo non esegue nulla: riceve dati e restituisce una lista di argomenti,
così ogni combinazione può essere verificata nei test senza avviare una VM.
"""

from dataclasses import dataclass, field

QEMU = "qemu-system-x86_64"


@dataclass
class RunOptions:
    """Scelte valide solo per un singolo avvio; non modificano la VM salvata."""

    ram: str | None = None
    cpus: int | None = None
    iso: str | None = None
    extra_disks: list = field(default_factory=list)
    usb_device: str | None = None
    spice_port: int | None = None
    display: str | None = None


def _quote(value):
    """Nelle opzioni di QEMU una virgola letterale va scritta raddoppiata."""
    return value.replace(",", ",,")


def _network_args(vm):
    if vm.network == "none":
        return ["-nic", "none"]
    if vm.network == "user":
        backend = "user,id=net0"
    elif vm.network.startswith("bridge:"):
        backend = f"bridge,id=net0,br={vm.network.removeprefix('bridge:')}"
    else:
        interface = vm.network.removeprefix("tap:")
        backend = f"tap,id=net0,ifname={interface},script=no,downscript=no"
    return ["-netdev", backend, "-device", f"{vm.nic},netdev=net0,mac={vm.mac}"]


def _audio_args(vm):
    if vm.audio == "none":
        return []
    return [
        "-audiodev", f"{vm.audio},id=audio0",
        "-device", "intel-hda",
        "-device", "hda-output,audiodev=audio0",
    ]


def build_argv(vm, options=None):
    options = options or RunOptions()
    argv = [
        QEMU,
        "-name", vm.name,
        "-accel", "kvm",
        "-cpu", "host",
        "-smp", str(options.cpus or vm.cpus),
        "-m", options.ram or vm.ram,
    ]

    for disk in [*vm.disks, *options.extra_disks]:
        argv += [
            "-drive",
            f"file={_quote(disk.path)},format={disk.format},if={vm.disk_interface}",
        ]

    if options.iso:
        argv += ["-drive", f"file={_quote(options.iso)},format=raw,media=cdrom,readonly=on"]
        argv += ["-boot", "once=d,menu=on"]
    else:
        argv += ["-boot", "menu=on"]

    argv += _network_args(vm)
    argv += _audio_args(vm)

    if options.usb_device:
        argv += [
            "-device", "qemu-xhci,id=usb",
            "-drive", f"file={_quote(options.usb_device)},format=raw,if=none,id=usbdisk,cache=none",
            "-device", "usb-storage,bus=usb.0,drive=usbdisk",
        ]

    if options.spice_port:
        argv += [
            "-vga", "qxl",
            "-spice", f"port={options.spice_port},addr=127.0.0.1,disable-ticketing=on",
            "-display", "none",
            # canale per l'agente SPICE del guest: copia/incolla con il client
            "-device", "virtio-serial-pci",
            "-chardev", "spicevmc,id=vdagent,name=vdagent",
            "-device", "virtserialport,chardev=vdagent,name=com.redhat.spice.0",
        ]
    else:
        argv += ["-vga", vm.vga]
        if (options.display or vm.display) == "serial":
            argv += ["-nographic"]

    return argv
