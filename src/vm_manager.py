"""Azioni sulle VM, condivise dalla riga di comando e dal menu."""

import dataclasses
import os
import shutil
import stat

from . import config, deps, disks, qemu, runner, usb, validate
from .config import VM, Disk
from .validate import SqemuError

SYS_NET = "/sys/class/net"
BRIDGE_ACL = "/etc/qemu/bridge.conf"
BRIDGE_HELPERS = ("/usr/lib/qemu/qemu-bridge-helper", "/usr/libexec/qemu-bridge-helper")


def describe_disk(value):
    path = disks.resolve_existing(value)
    return Disk(path=path, format=_runnable_format(path))


def _runnable_format(path):
    disk_format = disks.detect_format(path)
    return validate.choice(disk_format, validate.DISK_FORMATS, "Formato del disco")


def create_vm(name, size=None, disk_values=(), **settings):
    """Crea una VM con un disco nuovo nella sua cartella e/o con dischi esistenti."""
    validate.name(name)
    if config.exists(name):
        raise SqemuError(f"La VM {name!r} esiste già.")
    settings = {key: value for key, value in settings.items() if value is not None}
    VM(name=name, **settings)  # segnala i valori errati prima di creare file

    vm_disks = [describe_disk(value) for value in disk_values]
    if size:
        path = str(config.vm_dir(name) / f"{name}.qcow2")
        disks.create(path, size)
        vm_disks.insert(0, Disk(path=path, format="qcow2"))
    vm = VM(name=name, disks=vm_disks, **settings)
    config.save(vm)
    return vm


def bridge_allowed(acl_text, bridge):
    """Legge il file di QEMU che autorizza i bridge; con 'include' decide il programma di QEMU."""
    allowed = False
    for line in acl_text.splitlines():
        words = line.split("#", 1)[0].split()
        if len(words) != 2:
            continue
        if words[0] == "include":
            return True
        if words[0] == "deny" and words[1] in (bridge, "all"):
            return False
        if words[0] == "allow" and words[1] in (bridge, "all"):
            allowed = True
    return allowed


def check_bridge(bridge, sys_net=SYS_NET, acl_path=BRIDGE_ACL, helpers=BRIDGE_HELPERS):
    if not os.path.isdir(f"{sys_net}/{bridge}/bridge"):
        raise SqemuError(
            f"Il bridge {bridge} non esiste. Per crearlo isolato dalla rete:\n"
            f"  sudo ip link add {bridge} type bridge\n"
            f"  sudo ip link set {bridge} up\n"
            "Per portare le VM sulla LAN: con il Wi-Fi usa 'sqemu64 network setup-wifi "
            f"{bridge} <IP>', con il cavo collega al bridge la scheda Ethernet (in genere con NetworkManager)."
        )
    try:
        with open(acl_path, encoding="utf-8") as file:
            allowed = bridge_allowed(file.read(), bridge)
    except OSError:
        allowed = False
    if not allowed:
        raise SqemuError(
            f"Il bridge {bridge} non è autorizzato per QEMU. Per autorizzarlo:\n"
            f"  sudo mkdir -p {os.path.dirname(acl_path)}\n"
            f"  echo 'allow {bridge}' | sudo tee -a {acl_path}"
        )
    for helper in helpers:
        if os.path.exists(helper) and not os.stat(helper).st_mode & stat.S_ISUID:
            raise SqemuError(
                f"{helper} non può configurare la rete senza root. Per abilitarlo:\n"
                f"  sudo chmod u+s {helper}"
            )


def _check_network(vm):
    if vm.network.startswith("bridge:"):
        check_bridge(vm.network.removeprefix("bridge:"))
    if vm.network.startswith("tap:"):
        interface = vm.network.removeprefix("tap:")
        if not os.path.exists(f"/sys/class/net/{interface}"):
            raise SqemuError(
                f"L'interfaccia {interface} non esiste. Per crearla:\n"
                f"  sudo ip tuntap add {interface} mode tap user $USER\n"
                f"  sudo ip link set {interface} up\n"
                "Oppure usa la rete predefinita della VM: network = \"user\"."
            )


def save_screen(name, display, spice_port=None):
    """Ricorda lo schermo scelto, che il menu riproporrà ai prossimi avvii."""
    vm = config.load(name)
    vm.display = validate.choice(display, validate.DISPLAYS, "Schermo")
    if spice_port:
        vm.spice_port = validate.port(spice_port)
    config.save(vm)


def _resolve_screen(vm, display, spice_port):
    """Decide lo schermo di questo avvio: prima le opzioni indicate, poi la scelta salvata."""
    if display:
        validate.choice(display, validate.DISPLAYS, "Schermo")
    if spice_port and display not in (None, "spice"):
        raise SqemuError("La porta SPICE si può indicare solo con lo schermo SPICE.")
    display = "spice" if spice_port else display or vm.display or "serial"
    if display != "spice":
        return display, None
    return display, validate.port(spice_port) if spice_port else vm.spice_port


def save_network(name, network):
    """Ricorda la rete scelta, che il menu riproporrà ai prossimi avvii."""
    vm = config.load(name)
    vm.network = validate.network(network)
    config.save(vm)


def list_interfaces(sys_net=SYS_NET):
    """Restituisce i bridge e le interfacce tap presenti su questo computer."""
    bridges, taps = [], []
    try:
        names = sorted(os.listdir(sys_net))
    except OSError:
        return bridges, taps
    for name in names:
        if os.path.isdir(f"{sys_net}/{name}/bridge"):
            bridges.append(name)
        elif os.path.exists(f"{sys_net}/{name}/tun_flags"):
            taps.append(name)
    return bridges, taps


def prepare_start(
    name, ram=None, cpus=None, iso=None, disk_values=(), usb_device=None, spice_port=None, display=None, network=None
):
    """Controlla tutto e restituisce il comando da eseguire, senza avviare nulla."""
    vm = config.load(name)
    if network:
        vm = dataclasses.replace(vm, network=network)
    if not vm.disks and not iso:
        raise SqemuError(f"La VM {name!r} non ha dischi: indica un'immagine ISO da avviare.")
    display, spice_port = _resolve_screen(vm, display, spice_port)

    iso_path = None
    if iso:
        iso_path = os.path.abspath(os.path.expanduser(iso))
        if not os.path.isfile(iso_path):
            raise SqemuError(f"Immagine ISO non trovata: {iso_path}.")

    options = qemu.RunOptions(
        ram=validate.ram(ram) if ram else None,
        cpus=validate.cpus(cpus) if cpus else None,
        iso=iso_path,
        extra_disks=[describe_disk(value) for value in disk_values],
        usb_device=usb.select(usb_device) if usb_device else None,
        spice_port=spice_port,
        display=display,
    )
    _check_network(vm)
    return qemu.build_argv(vm, options)


def start(name, dry_run=False, **kwargs):
    argv = prepare_start(name, **kwargs)
    if dry_run:
        print(" ".join(argv))
        return
    problem = deps.kvm_problem()
    if problem:
        raise SqemuError(problem)
    vm = config.load(name)
    deps.check_audio(vm.audio)
    display, spice_port = _resolve_screen(vm, kwargs.get("display"), kwargs.get("spice_port"))
    if display == "spice":
        deps.check_spice()
        print(f"Connessione SPICE: remote-viewer spice://127.0.0.1:{spice_port}")
        print("Senza password: può collegarsi chiunque abbia un account su questo computer.")
        print("Per chiudere la VM premi Ctrl-C in questo terminale.")
    elif display == "window":
        deps.check_window()
    else:
        print("Console seriale: per chiudere la VM premi Ctrl-A e poi X.")
        print("Se resta vuota o si ferma dopo il BIOS, il sistema ospite scrive solo sullo schermo: usa SPICE.")
    runner.run(argv)


def print_inventory():
    names = config.list_names()
    print("Macchine virtuali:")
    for name in names:
        vm = config.load(name)
        screen = vm.display or "serial"
        print(f"  {name}  RAM {vm.ram}, CPU {vm.cpus}, dischi {len(vm.disks)}, rete {vm.network}, schermo {screen}")
    if not names:
        print("  (nessuna)")

    files = disks.list_files()
    print(f"\nDischi in {config.disks_dir()}:")
    for path in files:
        print(f"  {path.name}")
    if not files:
        print("  (nessuno)")


def open_folder():
    if shutil.which("xdg-open") is None:
        raise SqemuError(f"xdg-open non è disponibile. La cartella è: {config.data_dir()}")
    config.data_dir().mkdir(parents=True, exist_ok=True)
    runner.run(["xdg-open", str(config.data_dir())])
