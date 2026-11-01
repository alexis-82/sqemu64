"""Controllo e installazione delle dipendenze sulle principali distribuzioni."""

import os
import re
import shutil

from . import runner
from .qemu import QEMU
from .validate import SqemuError

REQUIRED = {"qemu-system-x86_64": "avvio delle VM", "qemu-img": "gestione dei dischi"}
OPTIONAL = {
    "lsblk": "selezione delle chiavette USB",
    "iptables": "bridge sul Wi-Fi",
    "dnsmasq": "indirizzo automatico per le VM sul bridge",
    "remote-viewer": "solo per collegarsi via SPICE",
    "dmg2img": "conversione DMG (in alternativa viene usato qemu-img)",
    "xdg-open": "apertura della cartella",
}

INSTALLERS = {
    "arch": [
        "pacman", "-S", "--needed", "qemu-base", "qemu-audio-pipewire", "qemu-ui-spice-core",
        "qemu-hw-display-qxl", "qemu-chardev-spice", "virt-viewer", "dnsmasq",
    ],
    "debian": [
        "apt-get", "install", "--no-install-recommends", "qemu-system-x86", "qemu-utils",
        "qemu-system-gui", "qemu-system-modules-spice", "virt-viewer", "dnsmasq",
    ],
    "fedora": [
        "dnf", "install", "--setopt=install_weak_deps=False", "qemu-system-x86-core", "qemu-img",
        "qemu-audio-pipewire", "qemu-ui-spice-core", "qemu-device-display-qxl", "qemu-char-spice", "virt-viewer",
        "dnsmasq",
    ],
    "suse": [
        "zypper", "install", "--no-recommends", "qemu-x86", "qemu-tools", "qemu-audio-pipewire",
        "qemu-ui-spice-core", "qemu-hw-display-qxl", "qemu-chardev-spice", "virt-viewer", "dnsmasq",
    ],
}
_FAMILIES = {"ubuntu": "debian", "rhel": "fedora", "opensuse": "suse"}


def parse_os_release(text):
    values = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def family(os_release):
    """Ricava la famiglia della distribuzione, anche per le derivate come CachyOS."""
    candidates = [os_release.get("ID", ""), *os_release.get("ID_LIKE", "").split()]
    for candidate in candidates:
        candidate = _FAMILIES.get(candidate, candidate)
        if candidate in INSTALLERS:
            return candidate
        if candidate.startswith("opensuse"):
            return "suse"
    return None


def install_argv(os_release):
    distribution = family(os_release)
    if distribution is None:
        raise SqemuError("Distribuzione non riconosciuta: installa QEMU con il tuo gestore di pacchetti.")
    return ["sudo", *INSTALLERS[distribution]]


def _read_os_release():
    with open("/etc/os-release", encoding="utf-8") as file:
        return parse_os_release(file.read())


def missing_required():
    return [tool for tool in REQUIRED if shutil.which(tool) is None]


def kvm_problem():
    if not os.path.exists("/dev/kvm"):
        return "/dev/kvm non esiste: abilita la virtualizzazione nel BIOS/UEFI e verifica i moduli KVM."
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        return "Non hai accesso a /dev/kvm: aggiungi il tuo utente al gruppo 'kvm' e riesegui il login."
    return None


def parse_backends(output):
    """Legge l'elenco di '-display help' e '-audiodev help': finisce alla prima riga vuota."""
    names = []
    for line in output.splitlines()[1:]:
        if not line.strip():
            break
        names.append(line.strip())
    return names


def _backends(option):
    return parse_backends(runner.capture([QEMU, option, "help"]))


def _devices():
    return set(re.findall(r'^name "([^"]+)"', runner.capture([QEMU, "-device", "help"]), re.MULTILINE))


def chardev_available(kind):
    """Prova ad avviare QEMU: i moduli non compaiono in '-chardev help' finché non servono."""
    argv = [
        QEMU, "-machine", "none", "-nodefaults", "-display", "none", "-monitor", "stdio",
        "-chardev", f"{kind},id=probe,name=probe",
    ]
    return "is not a valid char driver name" not in runner.probe(argv, "quit\n")


def check_window():
    if not set(_backends("-display")) - {"none"}:
        raise SqemuError(
            "Questa installazione di QEMU non può aprire finestre: usa la console seriale "
            "(--display serial) oppure installa i moduli grafici di QEMU."
        )


def check_audio(driver):
    if driver != "none" and driver not in _backends("-audiodev"):
        raise SqemuError(f"Il driver audio {driver!r} non è disponibile in questa installazione di QEMU.")


def check_spice():
    if "qxl-vga" not in _devices():
        raise SqemuError(
            "SPICE non è disponibile in questa installazione di QEMU. "
            "Su Arch servono i pacchetti qemu-ui-spice-core e qemu-hw-display-qxl."
        )
    if not chardev_available("spicevmc"):
        raise SqemuError(
            "Manca il canale SPICE per il copia/incolla. Su Arch serve il pacchetto qemu-chardev-spice."
        )


def report():
    for label, tools in (("Necessarie", REQUIRED), ("Opzionali", OPTIONAL)):
        print(f"{label}:")
        for tool, purpose in tools.items():
            state = "ok" if shutil.which(tool) else "MANCA"
            print(f"  [{state:^5}] {tool}: {purpose}")
    problem = kvm_problem()
    print(f"\nKVM: {problem or 'disponibile'}")


def install():
    argv = install_argv(_read_os_release())
    print("Eseguo:", " ".join(argv))
    runner.run(argv)
