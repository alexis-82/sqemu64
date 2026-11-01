"""Riga di comando. Senza argomenti apre il menu interattivo."""

import argparse
import os
import sys

from . import __version__, deps, disks, menu, network, usb, validate, vm_manager
from .validate import SqemuError


def _add_run_options(parser):
    parser.add_argument("--ram", help="RAM solo per questo avvio, per esempio 4G")
    parser.add_argument("--cpus", help="CPU solo per questo avvio")
    parser.add_argument("--usb", metavar="DEV", help="chiavetta USB da collegare, per esempio sdb")
    parser.add_argument("--display", choices=validate.DISPLAYS, help="schermo solo per questo avvio")
    parser.add_argument("--network", help="rete solo per questo avvio: user, none, tap:<nome>, bridge:<nome>")
    parser.add_argument("--dry-run", action="store_true", help="mostra il comando senza eseguirlo")


def build_parser():
    parser = argparse.ArgumentParser(prog="sqemu64", description="Gestione di macchine virtuali QEMU/KVM senza root.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command")

    commands.add_parser("menu", help="apre il menu interattivo")
    commands.add_parser("list", help="elenca VM e dischi")
    commands.add_parser("folder", help="apre la cartella dei dati")
    commands.add_parser("usb", help="elenca le chiavette USB utilizzabili")
    dependencies = commands.add_parser("deps", help="controlla le dipendenze")
    dependencies.add_argument("--install", action="store_true", help="installa i pacchetti con sudo")

    disk = commands.add_parser("disk", help="gestisce i dischi").add_subparsers(dest="disk_command", required=True)
    create = disk.add_parser("create", help="crea un disco")
    create.add_argument("name")
    create.add_argument("size")
    create.add_argument("--format", default="qcow2", choices=validate.DISK_FORMATS)
    convert = disk.add_parser("convert", help="converte un disco")
    convert.add_argument("source")
    convert.add_argument("target")
    convert.add_argument("--to", required=True, choices=validate.DISK_FORMATS)
    convert.add_argument("--from", dest="source_format", choices=disks.SOURCE_FORMATS)
    resize = disk.add_parser("resize", help="ridimensiona un disco")
    resize.add_argument("name")
    resize.add_argument("size")
    resize.add_argument("--shrink", action="store_true", help="consente la riduzione")
    clone = disk.add_parser("clone", help="clona un disco")
    clone.add_argument("source")
    clone.add_argument("target")

    vm = commands.add_parser("vm", help="gestisce le VM").add_subparsers(dest="vm_command", required=True)
    vm_create = vm.add_parser("create", help="crea una VM")
    vm_create.add_argument("name")
    vm_create.add_argument("--size", help="crea un nuovo disco di questa dimensione")
    vm_create.add_argument("--disk", action="append", default=[], help="collega un disco esistente (ripetibile)")
    vm_create.add_argument("--ram")
    vm_create.add_argument("--cpus")
    vm_create.add_argument("--network", help="user, none, tap:<interfaccia> oppure bridge:<bridge>")
    vm_create.add_argument("--nic", choices=validate.NICS)
    vm_create.add_argument("--vga", choices=validate.VGAS)
    vm_create.add_argument("--disk-interface", choices=validate.DISK_INTERFACES)
    vm_create.add_argument("--audio", choices=validate.AUDIO_DRIVERS)
    vm_create.add_argument("--display", choices=validate.DISPLAYS, help="predefinito: serial")
    vm_create.add_argument("--spice-port", help="porta SPICE salvata (predefinita: 5900)")

    net = commands.add_parser("network", help="gestisce il bridge sul Wi-Fi").add_subparsers(
        dest="network_command", required=True
    )
    setup = net.add_parser("setup-wifi", help="porta le VM nella LAN dell'host tramite proxy ARP")
    setup.add_argument("bridge")
    setup.add_argument("ip", help="indirizzo della VM nella rete dell'host")
    setup.add_argument("--interface", help="connessione da usare (predefinita: quella del gateway)")
    setup.add_argument("--yes", action="store_true", help="non chiede conferma")
    remove = net.add_parser("remove-wifi", help="annulla solo ciò che ha fatto setup-wifi")
    remove.add_argument("bridge")
    remove.add_argument("--yes", action="store_true", help="non chiede conferma")

    start = commands.add_parser("start", help="avvia una VM")
    start.add_argument("name")
    start.add_argument("--iso", help="avvia da questa immagine ISO (Live CD)")
    start.add_argument("--disk", action="append", default=[], help="disco aggiuntivo solo per questo avvio")
    start.add_argument("--spice", metavar="PORTA", help="espone la VM tramite SPICE su 127.0.0.1")
    _add_run_options(start)

    shell = commands.add_parser("shell", help="avvia un sistema minimale (la VM 'shell')")
    _add_run_options(shell)

    return parser


def _run_disk(args):
    if args.disk_command == "create":
        path = disks.create(args.name, args.size, args.format)
    elif args.disk_command == "convert":
        path = disks.convert(args.source, args.target, args.to, args.source_format)
    elif args.disk_command == "resize":
        path = disks.resize(args.name, args.size, args.shrink)
    else:
        path = disks.clone(args.source, args.target)
    print(f"Fatto: {path}")


def _run_vm_create(args):
    settings = {
        "ram": validate.ram(args.ram) if args.ram else None,
        "cpus": validate.cpus(args.cpus) if args.cpus else None,
        "network": args.network,
        "nic": args.nic,
        "vga": args.vga,
        "disk_interface": args.disk_interface,
        "audio": args.audio,
        "display": args.display,
        "spice_port": validate.port(args.spice_port) if args.spice_port else None,
    }
    vm = vm_manager.create_vm(args.name, size=args.size, disk_values=args.disk, **settings)
    print(f"VM creata: {vm.name}")


def _confirm(args):
    if args.yes:
        return None
    return lambda: input("Procedere? (s/N): ").strip().lower() in ("s", "si", "sì", "y", "yes")


def _run_network(args):
    if args.network_command == "setup-wifi":
        network.setup_wifi(args.bridge, args.ip, args.interface, confirm=_confirm(args))
    else:
        network.remove_wifi(args.bridge, confirm=_confirm(args))


def _run_usb():
    found = usb.list_disks()
    for disk in found:
        print(disk)
    if not found:
        print("Nessuna chiavetta USB trovata.")


def dispatch(args):
    command = args.command
    if command in (None, "menu"):
        menu.run()
    elif command == "list":
        vm_manager.print_inventory()
    elif command == "folder":
        vm_manager.open_folder()
    elif command == "usb":
        _run_usb()
    elif command == "deps":
        deps.install() if args.install else deps.report()
    elif command == "disk":
        _run_disk(args)
    elif command == "vm":
        _run_vm_create(args)
    elif command == "network":
        _run_network(args)
    elif command == "start":
        vm_manager.start(
            args.name, dry_run=args.dry_run, ram=args.ram, cpus=args.cpus, iso=args.iso,
            disk_values=args.disk, usb_device=args.usb, spice_port=args.spice, display=args.display,
            network=args.network,
        )
    elif command == "shell":
        vm_manager.start(
            "shell", dry_run=args.dry_run, ram=args.ram, cpus=args.cpus,
            usb_device=args.usb, display=args.display, network=args.network,
        )


def main(argv=None):
    if os.geteuid() == 0:
        print("Attenzione: sQemu64 non ha bisogno di root e i file creati apparterranno a root.", file=sys.stderr)
    args = build_parser().parse_args(argv)
    try:
        dispatch(args)
    except SqemuError as error:
        print(f"Errore: {error}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print()
        return 130
    return 0
