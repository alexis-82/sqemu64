"""Bridge per VM con un IP nella LAN dell'host, anche quando l'host usa il Wi-Fi.

Una scheda Wi-Fi di solito non può entrare in un bridge, perché la rete accetta solo il suo
MAC. Con il proxy ARP l'host risponde sulla LAN al posto della VM e le inoltra il traffico.

Un piccolo dnsmasq sul bridge assegna a ogni VM il suo IP, associato al MAC, insieme a gateway
e DNS, così nel sistema ospite non serve configurare nulla.

plan() calcola i comandi senza eseguirli. Ogni passo eseguito salva il comando che lo annulla,
così remove_wifi() disfa solo ciò che è stato davvero cambiato.
"""

import ipaddress
import json
import os
import shlex
import shutil
import time
from dataclasses import dataclass, field

from . import config, runner, validate, vm_manager
from .validate import SqemuError

BRIDGE_NETWORKS = ("192.168.77.1/24", "192.168.78.1/24", "10.77.77.1/24", "172.31.77.1/24")
_UNDO_TOOLS = ("ip", "sysctl", "iptables", "sed", "pkill")
_SYSTEM_PATHS = ("/usr/sbin", "/sbin")  # su Debian non sono nel PATH degli utenti normali
_FIRST_PROPOSED_HOST = 50


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
    dnsmasq_running: bool = False


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


def _pid_file(bridge):
    return f"/run/sqemu64-dnsmasq-{bridge}.pid"


def dnsmasq_argv(bridge, host, dhcp_hosts):
    """Solo DHCP (port=0 disattiva il DNS) e solo per i MAC delle VM indicate."""
    network = host.address.network
    argv = [
        "dnsmasq",
        "--conf-file=/dev/null",
        "--no-resolv",
        "--no-hosts",
        "--port=0",
        f"--interface={bridge}",
        "--bind-interfaces",
        "--except-interface=lo",
        f"--shared-network={bridge},{network.network_address}",
        "--dhcp-authoritative",
        f"--dhcp-range={network.network_address},static,{network.netmask}",
        f"--dhcp-option=3,{host.gateway}",
        f"--dhcp-option=6,{host.gateway}",
        f"--pid-file={_pid_file(bridge)}",
        f"--dhcp-leasefile=/run/sqemu64-dnsmasq-{bridge}.leases",
    ]
    argv += [f"--dhcp-host={mac},{ip},infinite" for mac, ip in sorted(dhcp_hosts.items())]
    return _sudo(*argv)


def _dhcp_steps(bridge, host, dhcp_hosts, restart):
    rule = ("INPUT", "-i", bridge, "-p", "udp", "--dport", "67", "-j", "ACCEPT")
    steps = [Step(_sudo("iptables", "-I", *rule), _sudo("iptables", "-D", *rule), check=_sudo("iptables", "-C", *rule))]
    stop = _sudo("pkill", "-F", _pid_file(bridge))
    if host.dnsmasq_running and restart:
        steps.append(Step(stop))
    if not host.dnsmasq_running or restart:
        steps.append(Step(dnsmasq_argv(bridge, host, dhcp_hosts), stop))
    return steps


def plan(bridge, ip, host, dhcp_hosts=None, restart_dhcp=False):
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
    if dhcp_hosts:
        steps += _dhcp_steps(bridge, host, dhcp_hosts, restart_dhcp)
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
        dnsmasq_running=_dnsmasq_running(bridge),
    )


def _dnsmasq_running(bridge):
    try:
        with open(_pid_file(bridge), encoding="ascii") as file:
            pid = int(file.read().strip())
        with open(f"/proc/{pid}/comm", encoding="ascii") as file:
            return file.read().strip() == "dnsmasq"
    except (OSError, ValueError):
        return False


def _tool_exists(tool):
    path = os.pathsep.join([os.environ.get("PATH", ""), *_SYSTEM_PATHS])
    return shutil.which(tool, path=path) is not None


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
        return {"bridge": bridge, "ips": [], "undo": [], "hosts": {}, "dhcp_hosts": {}}
    except (OSError, json.JSONDecodeError) as error:
        raise SqemuError(f"Stato della rete non valido: {error}") from None
    state.setdefault("hosts", {})
    state.setdefault("dhcp_hosts", {})
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


def setup_wifi(bridge, ip_value, uplink=None, confirm=None, vm=None):
    """Prepara il bridge e fa comparire nella rete dell'host l'IP indicato.

    Con una VM, il suo MAC riceve quell'IP via DHCP; senza, l'IP va impostato nel sistema ospite.
    """
    validate.interface(bridge)
    if uplink:
        validate.interface(uplink)
    if not _tool_exists("iptables"):
        raise SqemuError("Il comando iptables non è disponibile.")
    if vm and not _tool_exists("dnsmasq"):
        raise SqemuError("Il comando dnsmasq non è disponibile: installalo con sqemu64 deps --install.")

    host = gather(bridge, uplink)
    ip = parse_guest_ip(ip_value, host)
    if _route_elsewhere(ip, bridge):
        raise SqemuError(f"Esiste già una route per {ip} verso un'altra interfaccia.")
    host.route_exists = _route_exists(ip, bridge)
    if not host.route_exists and _address_in_use(ip):
        raise SqemuError(f"{ip} risponde già in rete: scegli un altro indirizzo.")

    state = _load_state(bridge)
    dhcp_hosts = dict(state["dhcp_hosts"])
    for name, assigned in state["hosts"].items():
        if assigned["ip"] == str(ip) and (vm is None or name != vm.name):
            raise SqemuError(f"{ip} è già assegnato alla VM {name}.")
    old_ip = None
    if vm:
        previous = state["hosts"].get(vm.name)
        if previous and previous["mac"] != vm.mac:
            dhcp_hosts.pop(previous["mac"], None)
        if previous and previous["ip"] != str(ip):
            old_ip = previous["ip"]
        dhcp_hosts[vm.mac] = str(ip)

    restart_dhcp = bool(vm) and dhcp_hosts != state["dhcp_hosts"]
    steps = plan(bridge, ip, host, dhcp_hosts if vm or host.dnsmasq_running else None, restart_dhcp)
    old_route = _sudo("ip", "route", "del", f"{old_ip}/32", "dev", bridge) if old_ip else None
    if old_ip and _route_exists(old_ip, bridge):
        # Altrimenti il computer continuerebbe a rispondere sulla LAN per il vecchio indirizzo.
        steps.insert(0, Step(old_route))
    print(f"Connessione: {host.uplink} ({host.address}), router {host.gateway}")
    print("Comandi da eseguire:")
    _describe(steps)
    print("Questi valori durano fino al riavvio, tranne l'autorizzazione in bridge.conf.")
    if confirm is not None and not confirm():
        print("Operazione annullata.")
        return False

    _require_sudo()
    for step in steps:
        if step.check and runner.quiet(step.check) == 0:
            continue
        try:
            runner.run(step.argv, stdin_text=step.stdin)
        except SqemuError as error:
            raise SqemuError(f"{error}\nPer annullare i passi già eseguiti: sqemu64 network remove-wifi {bridge}") from None
        if step.argv[1] == "pkill":
            time.sleep(0.5)  # la porta DHCP deve liberarsi prima del nuovo avvio
        if step.undo and step.undo not in state["undo"]:
            state["undo"].append(step.undo)
            _save_state(bridge, state)
    if str(ip) not in state["ips"]:
        state["ips"].append(str(ip))
    if old_ip:
        state["ips"] = [item for item in state["ips"] if item != old_ip]
        state["undo"] = [argv for argv in state["undo"] if argv != old_route]
    if vm:
        state["hosts"][vm.name] = {"mac": vm.mac, "ip": str(ip)}
        state["dhcp_hosts"] = dhcp_hosts
    _save_state(bridge, state)

    print(f"\nBridge {bridge} pronto.")
    if vm:
        print(f"La VM {vm.name} riceverà {ip} via DHCP: nel sistema ospite non serve configurare nulla.")
        return True
    print(f"Per collegare una VM usa la rete bridge:{bridge} e imposta nel sistema ospite un indirizzo statico:")
    print(f"  IP       {ip}/{host.address.network.prefixlen}")
    print(f"  Gateway  {host.gateway}")
    print(f"  DNS      {host.gateway}")
    return True


def _is_managed(bridge):
    """Un bridge creato da altri (libvirt, NetworkManager, a mano) non viene toccato."""
    return _state_path(bridge).exists() or not os.path.exists(f"{vm_manager.SYS_NET}/{bridge}")


def is_ready(bridge, vm):
    """Controlla ciò che sparisce al riavvio, senza sudo: le regole iptables spariscono con il resto."""
    assigned = _load_state(bridge)["hosts"].get(vm.name)
    if not assigned or assigned["mac"] != vm.mac:
        return False
    if not os.path.isdir(f"{vm_manager.SYS_NET}/{bridge}/bridge"):
        return False
    return (
        _route_exists(assigned["ip"], bridge)
        and _dnsmasq_running(bridge)
        and _read_int("/proc/sys/net/ipv4/ip_forward") == 1
        and _read_int(f"/proc/sys/net/ipv4/conf/{bridge}/proxy_arp") == 1
    )


def propose_ip(bridge, vm, host):
    """Riusa l'IP della VM se è libero, altrimenti cerca il primo libero da .50 in poi."""
    state = _load_state(bridge)
    previous = state["hosts"].get(vm.name)
    if previous and not _route_elsewhere(previous["ip"], bridge):
        if _route_exists(previous["ip"], bridge) or not _address_in_use(previous["ip"]):
            return previous["ip"]

    taken = {assigned["ip"] for assigned in state["hosts"].values()} | set(state["ips"])
    taken |= {str(host.address.ip), str(host.gateway)}
    candidates = list(host.address.network.hosts())
    if len(candidates) > _FIRST_PROPOSED_HOST:
        candidates = candidates[_FIRST_PROPOSED_HOST - 1:]
    for candidate in candidates:
        if str(candidate) in taken or _route_elsewhere(candidate, bridge):
            continue
        if not _address_in_use(candidate):
            return str(candidate)
    raise SqemuError(f"Non trovo un indirizzo libero nella rete {host.address.network}.")


def prepare_vm(name, network_value=None, confirm=None):
    """Prima dell'avvio: se il bridge della VM non è pronto, propone di prepararlo."""
    vm = config.load(name)
    value = validate.network(network_value or vm.network)
    if not value.startswith("bridge:"):
        return
    bridge = value.removeprefix("bridge:")
    if not _is_managed(bridge) or is_ready(bridge, vm):
        return

    host = gather(bridge)
    ip = propose_ip(bridge, vm, host)
    print(f"Il bridge {bridge} non è pronto. IP proposto per la VM {name}: {ip}")
    print("Controlla che sia fuori dall'intervallo che il router assegna via DHCP.")
    if not setup_wifi(bridge, ip, confirm=confirm, vm=vm):
        raise SqemuError(f"Avvio annullato: il bridge {bridge} non è pronto.")


def prepare_all(bridge=None, confirm=None):
    """Prepara tutte le VM impostate su un bridge, oppure su tutti i bridge se non è indicato."""
    if bridge:
        validate.interface(bridge)
    found = False
    for name in config.list_names():
        vm = config.load(name)
        if not vm.network.startswith("bridge:"):
            continue
        vm_bridge = vm.network.removeprefix("bridge:")
        if bridge and vm_bridge != bridge:
            continue
        found = True
        if not _is_managed(vm_bridge):
            print(f"{name}: il bridge {vm_bridge} non è gestito da sQemu64, lo lascio com'è.")
        elif is_ready(vm_bridge, vm):
            ip = _load_state(vm_bridge)["hosts"][name]["ip"]
            print(f"{name}: già pronta su {vm_bridge} con IP {ip}.")
        else:
            prepare_vm(name, confirm=confirm)
    if not found:
        target = f"sul bridge {bridge}" if bridge else "su un bridge"
        raise SqemuError(f"Nessuna VM impostata {target}.")


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
