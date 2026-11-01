"""Bridge per VM con un IP nella LAN dell'host, anche quando l'host usa il Wi-Fi.

Una scheda Wi-Fi di solito non può entrare in un bridge, perché la rete accetta solo il suo
MAC. Con il proxy ARP l'host risponde sulla LAN al posto della VM e le inoltra il traffico.

plan() calcola i comandi senza eseguirli. Ogni passo eseguito salva il comando che lo annulla,
così remove_wifi() disfa solo ciò che è stato davvero cambiato.
"""

import ipaddress
import json
import os
import shlex
import shutil
from dataclasses import dataclass, field

from . import config, runner, validate, vm_manager
from .validate import SqemuError

BRIDGE_NETWORKS = ("192.168.77.1/24", "192.168.78.1/24", "10.77.77.1/24", "172.31.77.1/24")
_UNDO_TOOLS = ("ip", "sysctl", "iptables", "sed")


@dataclass
class Step:
    argv: list
    undo: list | None = None
    check: list | None = None  # se riesce, il passo è già a posto
    stdin: str | None = None


@dataclass
class Host:
    uplink: str
    address: ipaddress.IPv4Interface
    gateway: ipaddress.IPv4Address
    used_networks: list = field(default_factory=list)
    bridge_exists: bool = False
    bridge_has_address: bool = False
    acl_allowed: bool = False
    forward: int = 0
    proxy_arp: dict = field(default_factory=dict)
    route_exists: bool = False


def _sudo(*argv):
    return ["sudo", *argv]


def parse_guest_ip(value, host):
    """Controlla che l'IP sia utilizzabile dalla VM nella rete dell'host."""
    try:
        ip = ipaddress.IPv4Address(value)
    except ValueError:
        raise SqemuError(f"Indirizzo IP non valido: {value!r}.") from None
    network = host.address.network
    if ip not in network:
        raise SqemuError(f"{ip} non appartiene alla rete di {host.uplink} ({network}).")
    if ip in (network.network_address, network.broadcast_address):
        raise SqemuError(f"{ip} è un indirizzo riservato della rete {network}.")
    if ip == host.address.ip:
        raise SqemuError(f"{ip} è l'indirizzo di questo computer.")
    if ip == host.gateway:
        raise SqemuError(f"{ip} è l'indirizzo del router.")
    return ip


def _free_bridge_network(host):
    for candidate in BRIDGE_NETWORKS:
        network = ipaddress.ip_interface(candidate).network
        if not any(network.overlaps(used) for used in host.used_networks):
            return candidate
    raise SqemuError("Non trovo una rete privata libera per il bridge.")


def plan(bridge, ip, host):
    """Restituisce i passi necessari: ciò che è già a posto non viene toccato."""
    steps = []
    if not host.bridge_exists:
        steps.append(Step(_sudo("ip", "link", "add", bridge, "type", "bridge"), _sudo("ip", "link", "del", bridge)))
    if not host.bridge_has_address:
        address = _free_bridge_network(host)
        undo = None if not host.bridge_exists else _sudo("ip", "addr", "del", address, "dev", bridge)
        steps.append(Step(_sudo("ip", "addr", "add", address, "dev", bridge), undo))
    steps.append(Step(_sudo("ip", "link", "set", bridge, "up")))

    if not host.acl_allowed:
        acl = vm_manager.BRIDGE_ACL
        steps.append(Step(_sudo("mkdir", "-p", os.path.dirname(acl))))
        steps.append(Step(
            _sudo("tee", "-a", acl),
            _sudo("sed", "-i", f"/^allow {bridge}$/d", acl),
            stdin=f"allow {bridge}\n",
        ))

    if host.forward != 1:
        steps.append(Step(
            _sudo("sysctl", "-w", "net.ipv4.ip_forward=1"),
            _sudo("sysctl", "-w", f"net.ipv4.ip_forward={host.forward}"),
        ))
    for interface in (host.uplink, bridge):
        previous = host.proxy_arp.get(interface, 0)
        if previous != 1:
            key = f"net.ipv4.conf.{interface}.proxy_arp"
            steps.append(Step(_sudo("sysctl", "-w", f"{key}=1"), _sudo("sysctl", "-w", f"{key}={previous}")))

    if not host.route_exists:
        steps.append(Step(
            _sudo("ip", "route", "add", f"{ip}/32", "dev", bridge),
            _sudo("ip", "route", "del", f"{ip}/32", "dev", bridge),
        ))
    for direction in ("-i", "-o"):
        rule = ("FORWARD", direction, bridge, "-j", "ACCEPT")
        steps.append(Step(
            _sudo("iptables", "-I", *rule),
            _sudo("iptables", "-D", *rule),
            check=_sudo("iptables", "-C", *rule),
        ))
    return steps


def _read_int(path, default=0):
    try:
        with open(path, encoding="ascii") as file:
            return int(file.read().strip())
    except (OSError, ValueError):
        return default


def _ip_json(*args):
    try:
        return json.loads(runner.capture(["ip", "-j", *args]) or "[]")
    except json.JSONDecodeError:
        raise SqemuError("Risposta non valida dal comando ip.") from None


def _ipv4_addresses(interface):
    entries = _ip_json("-4", "addr", "show", "dev", interface)
    return [info for entry in entries for info in entry.get("addr_info", [])]


def gather(bridge, uplink=None):
    """Legge lo stato della rete senza modificare nulla."""
    routes = _ip_json("route", "show", "default")
    if uplink:
        routes = [route for route in routes if route.get("dev") == uplink]
    if not routes or "gateway" not in routes[0]:
        raise SqemuError(f"Nessuna connessione con gateway trovata{' su ' + uplink if uplink else ''}.")
    uplink = validate.interface(routes[0]["dev"])
    if uplink == bridge:
        raise SqemuError(f"{bridge} è già la connessione principale di questo computer.")

    addresses = _ipv4_addresses(uplink)
    if not addresses:
        raise SqemuError(f"{uplink} non ha un indirizzo IPv4.")

    bridge_path = f"{vm_manager.SYS_NET}/{bridge}"
    bridge_exists = os.path.isdir(f"{bridge_path}/bridge")
    if os.path.exists(bridge_path) and not bridge_exists:
        raise SqemuError(f"{bridge} esiste già ma non è un bridge.")

    used = []
    for route in _ip_json("route", "show"):
        try:
            used.append(ipaddress.ip_network(route["dst"], strict=False))
        except (KeyError, ValueError):
            pass

    try:
        with open(vm_manager.BRIDGE_ACL, encoding="utf-8") as file:
            acl_allowed = vm_manager.bridge_allowed(file.read(), bridge)
    except OSError:
        acl_allowed = False

    return Host(
        uplink=uplink,
        address=ipaddress.IPv4Interface(f"{addresses[0]['local']}/{addresses[0]['prefixlen']}"),
        gateway=ipaddress.IPv4Address(routes[0]["gateway"]),
        used_networks=used,
        bridge_exists=bridge_exists,
        bridge_has_address=bridge_exists and bool(_ipv4_addresses(bridge)),
        acl_allowed=acl_allowed,
        forward=_read_int("/proc/sys/net/ipv4/ip_forward"),
        proxy_arp={
            name: _read_int(f"/proc/sys/net/ipv4/conf/{name}/proxy_arp") for name in (uplink, bridge)
        },
    )


def _route_exists(ip, bridge):
    return any(route.get("dev") == bridge for route in _ip_json("route", "show", "to", "exact", f"{ip}/32"))


def _route_elsewhere(ip, bridge):
    return any(route.get("dev") != bridge for route in _ip_json("route", "show", "to", "exact", f"{ip}/32"))


def _address_in_use(ip):
    if shutil.which("ping") is None:
        print("Attenzione: ping non disponibile, non posso controllare se l'IP è libero.")
        return False
    return runner.quiet(["ping", "-c", "2", "-W", "1", str(ip)]) == 0


def _state_path(bridge):
    return config.data_dir() / "network" / f"{validate.interface(bridge)}.json"


def _load_state(bridge):
    try:
        state = json.loads(_state_path(bridge).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"bridge": bridge, "ips": [], "undo": []}
    except (OSError, json.JSONDecodeError) as error:
        raise SqemuError(f"Stato della rete non valido: {error}") from None
    return state


def _save_state(bridge, state):
    path = _state_path(bridge)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _require_sudo():
    if runner.quiet_interactive(["sudo", "-v"]) != 0:
        raise SqemuError("Servono i permessi di sudo per configurare la rete.")


def _describe(steps):
    for step in steps:
        line = shlex.join(step.argv)
        if step.stdin:
            line = f"echo {shlex.quote(step.stdin.strip())} | {line}"
        print(f"  {line}")


def setup_wifi(bridge, ip_value, uplink=None, confirm=None):
    """Prepara il bridge e fa comparire la VM nella rete dell'host con l'IP indicato."""
    validate.interface(bridge)
    if uplink:
        validate.interface(uplink)
    if shutil.which("iptables") is None:
        raise SqemuError("Il comando iptables non è disponibile.")

    host = gather(bridge, uplink)
    ip = parse_guest_ip(ip_value, host)
    if _route_elsewhere(ip, bridge):
        raise SqemuError(f"Esiste già una route per {ip} verso un'altra interfaccia.")
    host.route_exists = _route_exists(ip, bridge)
    if not host.route_exists and _address_in_use(ip):
        raise SqemuError(f"{ip} risponde già in rete: scegli un altro indirizzo.")

    steps = plan(bridge, ip, host)
    print(f"Connessione: {host.uplink} ({host.address}), router {host.gateway}")
    print("Comandi da eseguire:")
    _describe(steps)
    print("Questi valori durano fino al riavvio, tranne l'autorizzazione in bridge.conf.")
    if confirm is not None and not confirm():
        print("Operazione annullata.")
        return False

    _require_sudo()
    state = _load_state(bridge)
    for step in steps:
        if step.check and runner.quiet(step.check) == 0:
            continue
        try:
            runner.run(step.argv, stdin_text=step.stdin)
        except SqemuError as error:
            raise SqemuError(f"{error}\nPer annullare i passi già eseguiti: sqemu64 network remove-wifi {bridge}") from None
        if step.undo and step.undo not in state["undo"]:
            state["undo"].append(step.undo)
            _save_state(bridge, state)
    if str(ip) not in state["ips"]:
        state["ips"].append(str(ip))
    _save_state(bridge, state)

    print(f"\nBridge {bridge} pronto. Per collegare una VM usa la rete bridge:{bridge}.")
    print("Nel sistema ospite imposta un indirizzo statico:")
    print(f"  IP       {ip}/{host.address.network.prefixlen}")
    print(f"  Gateway  {host.gateway}")
    print(f"  DNS      {host.gateway}")
    print("Il DHCP non funziona con questa configurazione.")
    return True


def _check_undo(argv):
    """Il file di stato è modificabile: esegue con sudo solo i comandi che può aver scritto."""
    if len(argv) < 3 or argv[0] != "sudo" or argv[1] not in _UNDO_TOOLS:
        raise SqemuError(f"Comando non riconosciuto nello stato della rete: {shlex.join(argv)}")


def remove_wifi(bridge, confirm=None):
    """Annulla solo le modifiche fatte da setup_wifi(), dall'ultima alla prima."""
    validate.interface(bridge)
    if not _state_path(bridge).exists():
        raise SqemuError(f"Nessuna configurazione di sQemu64 per {bridge}: non ho nulla da annullare.")
    state = _load_state(bridge)
    undo = state.get("undo", [])
    for argv in undo:
        _check_undo(argv)

    print("Comandi da eseguire:")
    for argv in reversed(undo):
        print(f"  {shlex.join(argv)}")
    if confirm is not None and not confirm():
        print("Operazione annullata.")
        return False

    if undo:
        _require_sudo()
    for argv in reversed(undo):
        if runner.quiet(argv) != 0:
            print(f"Già assente, oppure non rimosso: {shlex.join(argv)}")
    _state_path(bridge).unlink()
    print(f"Configurazione di {bridge} rimossa.")
    return True
