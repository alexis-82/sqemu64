"""VM in esecuzione: file di runtime, elenco, spegnimento e console seriale in background.

Ogni avvio scrive in una cartella di runtime il PID di QEMU e un socket QMP; le VM
avviate in background con la console seriale espongono anche un socket per socat.
"""

import json
import os
import shutil
import signal
import socket
from pathlib import Path

from . import config, runner, validate
from .validate import SqemuError

PIDFILE = "qemu.pid"
QMP_SOCKET = "qmp.sock"
SERIAL_SOCKET = "serial.sock"
_UNIX_PATH_MAX = 107  # sun_path è di 108 byte, terminatore compreso


def run_root():
    """Cartella in tmpfs se disponibile: si svuota da sola al riavvio del computer."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    return Path(base) / "sqemu64" if base else config.data_dir() / "run"


def run_dir(name, create=False):
    directory = run_root() / validate.name(name)
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        for socket_name in (QMP_SOCKET, SERIAL_SOCKET):
            path = directory / socket_name
            if len(str(path).encode()) > _UNIX_PATH_MAX:
                raise SqemuError(
                    f"Percorso troppo lungo per un socket (massimo {_UNIX_PATH_MAX} byte): {path}. "
                    "Usa un nome di VM più corto o una cartella XDG_RUNTIME_DIR più corta."
                )
    return directory


def _is_qemu(pid):
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").startswith("qemu-system")
    except OSError:
        return False


def running_pid(name):
    """PID della VM se è in esecuzione; un file rimasto da un vecchio avvio non conta."""
    try:
        pid = int((run_dir(name) / PIDFILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if _is_qemu(pid) else None


def check_not_running(name):
    pid = running_pid(name)
    if pid:
        raise SqemuError(f"La VM {name!r} è già in esecuzione (PID {pid}). Per spegnerla: sqemu64 stop {name}")


def has_console(name):
    return running_pid(name) is not None and (run_dir(name) / SERIAL_SOCKET).exists()


def list_running():
    root = run_root()
    if not root.is_dir():
        return []
    result = []
    for directory in sorted(root.iterdir()):
        pid = running_pid(directory.name) if directory.is_dir() else None
        if pid:
            result.append((directory.name, pid))
    return result


def print_running():
    found = list_running()
    print("VM in esecuzione:")
    for name, pid in found:
        hint = f"  console: sqemu64 console {name}" if has_console(name) else ""
        print(f"  {name}  PID {pid}{hint}")
    if not found:
        print("  (nessuna)")


def _read_reply(stream):
    """Legge la risposta QMP saltando gli eventi asincroni."""
    while True:
        line = stream.readline()
        if not line:
            return None
        message = json.loads(line)
        if "event" not in message:
            return message


def qmp(path, command):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        try:
            connection.connect(str(path))
        except OSError as error:
            raise SqemuError(f"Impossibile contattare QEMU su {path}: {error.strerror or error}.") from None
        stream = connection.makefile("rwb")
        _read_reply(stream)  # saluto iniziale
        for execute in ("qmp_capabilities", command):
            stream.write(json.dumps({"execute": execute}).encode() + b"\n")
            stream.flush()
            reply = _read_reply(stream)
            if reply and "error" in reply:
                raise SqemuError(f"QEMU ha rifiutato {execute}: {reply['error'].get('desc', reply['error'])}.")


def _require_running(name):
    pid = running_pid(name)
    if pid is None:
        raise SqemuError(f"La VM {name!r} non è in esecuzione.")
    return pid


def stop(name, force=False):
    """Spegnimento ACPI, come il tasto di accensione; con force QEMU chiude subito."""
    pid = _require_running(name)
    if force:
        os.kill(pid, signal.SIGTERM)
        print(f"VM {name} chiusa.")
        return
    qmp(run_dir(name) / QMP_SOCKET, "system_powerdown")
    print(f"Richiesta di spegnimento inviata a {name}. Se il sistema ospite non risponde: sqemu64 stop {name} --force")


def console_argv(name):
    # Ctrl-] (0x1d) chiude socat lasciando la VM accesa
    return ["socat", "-,raw,echo=0,escape=0x1d", f"UNIX-CONNECT:{run_dir(name) / SERIAL_SOCKET}"]


def console(name):
    _require_running(name)
    if not has_console(name):
        raise SqemuError(
            f"La VM {name!r} non ha una console seriale in background: "
            "è stata avviata in primo piano o con un altro schermo."
        )
    if shutil.which("socat") is None:
        raise SqemuError("Per la console serve socat: installalo con sqemu64 deps --install.")
    print("Console seriale: premi Ctrl-] per staccarti, la VM resta accesa. Premi Invio se lo schermo è vuoto.")
    runner.run(console_argv(name))
