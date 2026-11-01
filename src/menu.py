"""Menu interattivo: stessi numeri della versione storica, stessa logica della CLI."""

from . import config, deps, disks, usb, validate, vm_manager
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

[10] SPICE

[11] Apri cartella
[12] Lista sistemi installati

[13] Installazione delle dipendenze

[0] Uscire
"""


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


def _start_disk():
    name = _ask_vm()
    vm_manager.start(name, **_ask_overrides(), usb_device=_ask_usb())


def _start_multiple():
    name = _ask_vm()
    vm_manager.start(name, **_ask_overrides(), disk_values=_ask_extra_disks())


def _live():
    name = _ask_vm()
    iso = _required("Percorso dell'immagine ISO")
    vm_manager.start(name, **_ask_overrides(), iso=iso, usb_device=_ask_usb())


def _live_multiple():
    name = _ask_vm()
    disk_values = _ask_extra_disks()
    iso = _required("Percorso dell'immagine ISO")
    vm_manager.start(name, **_ask_overrides(), iso=iso, disk_values=disk_values)


def _shell():
    if not config.exists("shell"):
        raise SqemuError("Crea prima la VM con: sqemu64 vm create shell --disk /percorso/shell.img")
    vm_manager.start("shell", **_ask_overrides(), usb_device=_ask_usb())


def _spice():
    name = _ask_vm()
    vm_manager.start(name, **_ask_overrides(), spice_port=_required("Porta", "5900"))


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
    "10": _spice,
    "11": vm_manager.open_folder,
    "12": vm_manager.print_inventory,
    "13": _install,
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
