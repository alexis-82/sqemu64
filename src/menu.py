"""Menu interattivo: stessa logica della riga di comando."""

from . import config, deps, disks, network, usb, validate, vm_manager
from .validate import SqemuError

GREEN = "\033[92m"
RESET = "\033[0m"

BANNER = r"""
  6MMMMb\  6MMMMb/  6MMMMb  `MM 6MMb  6MMb  `MM    MM
 MM'    ` 6M'  `MM 6M'  `Mb  MM69 `MM69 `Mb  MM    MM
 YM.      MM    MM MM    MM  MM'   MM'   MM  MM    MM
  YMMMMb  MM    MM MMMMMMMM  MM    MM    MM  MM    MM
      `Mb MM    MM MM        MM    MM    MM  MM    MM
 L    ,MM YM.  ,MM YM        MM    MM    MM  YM.   MM
 MYMMMM9   YMMMM9M  YMMMMM9  MM    MM    MM   YMMM9MM
                MM
                MM            \script qemu/
                MM            /code alexis\
"""

MENU = """
[1] Creare disco
[2] Converti disco in IMG
[3] Converti disco in VMDK
[4] Ridimensiona disco
[4.5] Clona disco
[5] Avviare disco
[6] Avviare dischi multipli
[7] CD Live
[8] CD Live con dischi multipli
[9] Avvio shell

[10] Apri cartella
[11] Lista sistemi installati

[12] Installazione delle dipendenze
[13] Bridge sul Wi-Fi

[0] Uscire
"""


SCREENS = {"1": "spice", "2": "serial", "3": "window"}
NETWORKS = {"1": "user", "2": "bridge", "3": "tap", "4": "none"}


def _ask(prompt, default=None):
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def _required(prompt, default=None):
    answer = _ask(prompt, default)
    if not answer:
        raise SqemuError("Valore obbligatorio.")
    return answer


def _yes(prompt):
    return input(f"{prompt} (s/N): ").strip().lower() in ("s", "si", "sì", "y", "yes")


def _ask_vm():
    """Sceglie una VM; un disco non ancora associato ottiene una VM salvata."""
    names = config.list_names()
    if names:
        print("VM disponibili:", ", ".join(names))
    value = _required("Nome della VM o del disco")
    if "/" not in value and config.exists(value):
        return value

    path = disks.resolve_existing(value)
    name = validate.name(_required("Nome per la nuova VM", _vm_name_from(path)))
    print("La configurazione verrà salvata per i prossimi avvii.")
    vm_manager.create_vm(
        name,
        disk_values=[path],
        ram=validate.ram(_required("Quanta RAM? (es. 512M o 4G)", "2G")),
        cpus=validate.cpus(_required("Numero di CPU/core", "2")),
    )
    return name


def _vm_name_from(path):
    filename = path.rsplit("/", 1)[-1]
    return filename.rsplit(".", 1)[0]


def _ask_overrides():
    return {
        "ram": _ask("RAM per questo avvio (INVIO = salvata)"),
        "cpus": _ask("CPU per questo avvio (INVIO = salvate)"),
    }


def _ask_usb():
    if not _yes("Avviare con una chiavetta USB?"):
        return None
    found = usb.list_disks()
    if not found:
        raise SqemuError("Nessuna chiavetta USB trovata.")
    for disk in found:
        print(" ", disk)
    return _required("Dispositivo (es. sdb)")


def _choose_screen(name, live=False):
    """Chiede lo schermo e lo salva. Senza una scelta salvata, le ISO propongono SPICE."""
    vm = config.load(name)
    saved = vm.display or ("spice" if live else "serial")
    default = next(key for key, display in SCREENS.items() if display == saved)
    choice = _required("Schermo: 1 = SPICE (per i sistemi grafici), 2 = console seriale, 3 = finestra", default)
    display = SCREENS.get(choice)
    if display is None:
        raise SqemuError(f"Scelta non valida: {choice!r}.")
    port = _required("Porta", str(vm.spice_port)) if display == "spice" else None
    vm_manager.save_screen(name, display, port)


def _choose_network(name):
    """Chiede la rete e la salva: con Invio resta quella della VM."""
    vm = config.load(name)
    kind, _, saved_interface = vm.network.partition(":")
    default = next(key for key, value in NETWORKS.items() if value == kind)
    choice = _required("Rete: 1 = Internet (user), 2 = bridge, 3 = tap, 4 = nessuna", default)
    selected = NETWORKS.get(choice)
    if selected is None:
        raise SqemuError(f"Scelta non valida: {choice!r}.")

    if selected in ("user", "none"):
        vm_manager.save_network(name, selected)
        return
    bridges, taps = vm_manager.list_interfaces()
    found = bridges if selected == "bridge" else taps
    print(f"Disponibili: {', '.join(found) if found else '(nessuna)'}")
    fallback = "br0" if selected == "bridge" else None
    proposal = saved_interface if kind == selected else (found[0] if found else fallback)
    interface = _required("Nome dell'interfaccia", proposal)
    vm_manager.save_network(name, f"{selected}:{interface}")


def _choose_devices(name, live=False):
    _choose_network(name)
    _choose_screen(name, live=live)


def _ask_extra_disks():
    values = [_required("Nome del disco secondario")]
    while _yes("Aggiungere un altro disco?"):
        values.append(_required("Nome del disco"))
    return values


def _create_disk():
    path = disks.create(_required("Nome del disco da creare"), _required("Capacità (es. 8G)"))
    print(f"Disco creato: {path}")


def _convert_to(target_format):
    source = _required("Disco di origine (nome o percorso)")
    target = _required("Nome del nuovo disco")
    print("Attendere prego...")
    print("Disco creato:", disks.convert(source, target, target_format))


def _resize_disk():
    value = _required("Disco da ridimensionare")
    size = _required("Nuova capacità (es. 20G)")
    try:
        disks.resize(value, size)
    except SqemuError as error:
        if "inferiore" not in str(error):
            raise
        print(error)
        if not _yes("Confermi la riduzione?"):
            print("Operazione annullata.")
            return
        disks.resize(value, size, shrink=True)
    print("Disco ridimensionato.")


def _clone_disk():
    source = _required("Disco originale")
    target = _required("Nome della copia")
    print("Attendere prego...")
    print("Copia creata:", disks.clone(source, target))


def _start(name, **options):
    network.prepare_vm(name, confirm=lambda: _yes("Procedere?"))
    vm_manager.start(name, **options)


def _start_disk():
    name = _ask_vm()
    options = _ask_overrides()
    usb_device = _ask_usb()
    _choose_devices(name)
    _start(name, **options, usb_device=usb_device)


def _start_multiple():
    name = _ask_vm()
    options = _ask_overrides()
    disk_values = _ask_extra_disks()
    _choose_devices(name)
    _start(name, **options, disk_values=disk_values)


def _live():
    name = _ask_vm()
    iso = _required("Percorso dell'immagine ISO")
    options = _ask_overrides()
    usb_device = _ask_usb()
    _choose_devices(name, live=True)
    _start(name, **options, iso=iso, usb_device=usb_device)


def _live_multiple():
    name = _ask_vm()
    disk_values = _ask_extra_disks()
    iso = _required("Percorso dell'immagine ISO")
    options = _ask_overrides()
    _choose_devices(name, live=True)
    _start(name, **options, iso=iso, disk_values=disk_values)


def _shell():
    if not config.exists("shell"):
        raise SqemuError("Crea prima la VM con: sqemu64 vm create shell --disk /percorso/shell.img")
    options = _ask_overrides()
    usb_device = _ask_usb()
    _choose_devices("shell")
    _start("shell", **options, usb_device=usb_device)


def _wifi_bridge():
    choice = _required("Bridge sul Wi-Fi: 1 = prepara, 2 = rimuovi", "1")
    if choice not in ("1", "2"):
        raise SqemuError(f"Scelta non valida: {choice!r}.")
    bridge = _required("Nome del bridge", "br0")
    confirm = lambda: _yes("Procedere?")  # noqa: E731
    if choice == "2":
        network.remove_wifi(bridge, confirm=confirm)
        return
    ip = _required("Indirizzo della VM nella rete di questo computer (es. 192.168.1.50)")
    network.setup_wifi(bridge, ip, confirm=confirm)


def _install():
    deps.report()
    if _yes("\nInstallare QEMU, audio e SPICE con sudo?"):
        deps.install()


ACTIONS = {
    "1": _create_disk,
    "2": lambda: _convert_to("raw"),
    "3": lambda: _convert_to("vmdk"),
    "4": _resize_disk,
    "4.5": _clone_disk,
    "5": _start_disk,
    "6": _start_multiple,
    "7": _live,
    "8": _live_multiple,
    "9": _shell,
    "10": vm_manager.open_folder,
    "11": vm_manager.print_inventory,
    "12": _install,
    "13": _wifi_bridge,
}


def run():
    print(BANNER)
    print(f"{GREEN}sQemu versione a 64bit{RESET}")
    while True:
        print(MENU)
        try:
            selection = input(": ").strip()
            if selection == "0":
                return
            action = ACTIONS.get(selection)
            if action is None:
                print("Opzione non trovata.")
                continue
            action()
        except SqemuError as error:
            print(f"Errore: {error}")
        except KeyboardInterrupt:
            print("\nOperazione annullata.")
        except EOFError:
            print()
            return
