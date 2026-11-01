# sQemu64

Gestione di macchine virtuali QEMU/KVM da terminale, **senza privilegi di root e
senza interfaccia grafica**.
Si può usare con un menu interattivo o con comandi adatti agli script.

Richiede Python 3.11 o superiore e nessuna libreria esterna.

## Installazione

```bash
git clone https://github.com/alexis-82/sqemu64.git
cd sqemu64
./sqemu64 deps            # controlla le dipendenze
./sqemu64 deps --install  # installa tutto con sudo (Arch, Debian, Fedora, openSUSE e derivate)
```

`deps --install` installa QEMU da terminale, audio PipeWire, SPICE e copia/incolla.

Per avere il comando `sqemu64` da qualsiasi cartella, l'icona e la voce nel menu delle
applicazioni, senza root:

```bash
./install.sh              # installa per il tuo utente
./install.sh --uninstall  # rimuove tutto, ma non VM e dischi
```

Il comando resta collegato alla cartella del progetto: se la sposti, rilancia lo script.

## Pacchetti

Per ogni versione pubblicata, GitHub Actions genera i pacchetti per Debian/Ubuntu (`.deb`),
Fedora/openSUSE (`.rpm`), Arch (`.pkg.tar.zst`) e Alpine (`.apk`), li prova su ogni
distribuzione e li allega alla release.

Per pubblicare una versione, aggiorna `__version__` in `src/__init__.py`, poi crea il tag
corrispondente, per esempio `v2.0.0`.

## Uso rapido

```bash
sqemu64                                        # menu interattivo
sqemu64 vm create debian --size 40G --ram 4G --cpus 4
sqemu64 start debian --iso ~/Scaricati/debian.iso   # installazione o Live CD
sqemu64 start debian                           # avvio normale
```

## Dal menu ai comandi

| Menu | Funzione | Comando |
|---|---|---|
| 1 | Creare disco | `sqemu64 disk create NOME 8G` |
| 2 | Convertire in IMG | `sqemu64 disk convert ORIGINE NUOVO --to raw` |
| 3 | Convertire in VMDK | `sqemu64 disk convert ORIGINE NUOVO --to vmdk` |
| 4 | Ridimensionare | `sqemu64 disk resize NOME 20G` |
| 4.5 | Clonare | `sqemu64 disk clone ORIGINE COPIA` |
| 5 | Avviare | `sqemu64 start VM [--ram 4G] [--cpus 4] [--usb sdb] [--network bridge:br0]` |
| 6 | Dischi multipli | `sqemu64 start VM --disk ALTRO` |
| 7 | CD Live | `sqemu64 start VM --iso FILE.iso [--usb sdb]` |
| 8 | CD Live con dischi multipli | `sqemu64 start VM --iso FILE.iso --disk ALTRO` |
| 9 | Sistema minimale | `sqemu64 shell [--usb sdb]` |
| 10 | Aprire la cartella | `sqemu64 folder` |
| 11 | Elenco | `sqemu64 list` |
| 12 | Dipendenze | `sqemu64 deps [--install]` |
| 13 | Bridge sul Wi-Fi | `sqemu64 network setup-wifi BRIDGE IP` e `network remove-wifi BRIDGE` |

Con `--dry-run` i comandi `start` e `shell` mostrano la riga di QEMU senza eseguirla.

Per usare SPICE dal menu, scegli `1 = SPICE` quando viene chiesto lo schermo. Da riga di
comando, aggiungi `--spice 5900` a `sqemu64 start`, solo per quell'avvio.

## Dove sono i dati

```text
~/.local/share/sqemu64/
├── disks/            dischi creati con "disk create"
└── vms/<nome>/
    ├── vm.toml       configurazione della VM
    └── <nome>.qcow2  disco creato con "vm create --size"
```

Un nome semplice (`win`) indica la cartella `disks/`, mentre un nome con una barra
(`./win.img`, `/dati/win.img`) è un percorso. I vecchi dischi si possono usare dove
si trovano:

```bash
sqemu64 vm create win --disk ./win.img --ram 4G --cpus 4
```

## Rete

- `user` (predefinita): accesso a Internet senza alcuna configurazione. La VM non ha un
  indirizzo nella LAN.
- `bridge:<bridge>`: collega la VM a un bridge tramite `qemu-bridge-helper`, senza root a
  ogni avvio. Se manca qualcosa, sQemu64 mostra i comandi da lanciare.
- `tap:<interfaccia>`: usa un'interfaccia già creata, per esempio con
  `sudo ip tuntap add tap0 mode tap user $USER && sudo ip link set tap0 up`.
- `none`: nessuna rete.

Le opzioni di avvio del menu chiedono la rete e la salvano nella VM, così ai prossimi avvii
basta premere Invio. Da riga di comando, `sqemu64 vm create --network` imposta la rete di
una nuova VM, mentre `sqemu64 start --network` vale solo per quell'avvio.

### Bridge con il Wi-Fi

Una scheda Wi-Fi di solito non può entrare in un bridge, ma la VM può comunque avere un
indirizzo nella stessa rete del computer, con accesso a Internet e alla LAN:

```bash
sqemu64 network setup-wifi br0 192.168.1.50   # oppure opzione 13 del menu
sqemu64 vm create nome --size 40G --network bridge:br0
sqemu64 network remove-wifi br0               # annulla solo ciò che ha fatto setup-wifi
```

Il comando mostra le operazioni, chiede conferma e usa `sudo` solo per queste. Sceglie la
connessione del gateway, altrimenti puoi indicarla con `--interface`. Usa il proxy ARP: il
computer risponde sulla LAN al posto della VM.

- Nel sistema ospite imposta un **indirizzo statico**, con il router come gateway e DNS. Il
  DHCP non funziona. Il gateway è necessario anche per comunicare con il computer host.
- Con NetworkManager, per esempio su Debian con desktop, usa `nmcli`: un indirizzo aggiunto
  con `ip addr` può sparire quando il DHCP fallisce.

  ```bash
  sudo nmcli con add type ethernet ifname ens3 con-name lan ipv4.method manual \
    ipv4.addresses 192.168.1.50/24 ipv4.gateway 192.168.1.1 ipv4.dns 192.168.1.1 \
    connection.autoconnect-priority 10
  sudo nmcli con up lan
  ```
- Scegli un IP libero, fuori dall'intervallo che il router assegna via DHCP.
- Ogni VM ha il suo IP: rilancia il comando per ognuna, sullo stesso bridge.
- Tutto dura fino al riavvio, tranne l'autorizzazione in `bridge.conf`: dopo un riavvio
  rilancia il comando.
- Il broadcast non attraversa il computer, quindi la ricerca automatica di dispositivi in
  rete può non funzionare.

### Bridge con il cavo o isolato

Per un bridge isolato, in cui le VM comunicano solo con il computer:

```bash
sudo ip link add br0 type bridge
sudo ip addr add 192.168.77.1/24 dev br0
sudo ip link set br0 up
sudo mkdir -p /etc/qemu
echo 'allow br0' | sudo tee -a /etc/qemu/bridge.conf
sqemu64 vm create nome --size 40G --network bridge:br0
```

Con il cavo, puoi invece collegare al bridge la scheda Ethernet, in genere con
NetworkManager: la VM ottiene un indirizzo dal router come gli altri dispositivi. Questo
caso non è ancora stato provato.

## Chiavette USB

sQemu64 accetta solo dischi USB smontati e rifiuta i dischi interni. Elenco con
`sqemu64 usb`. Se mancano i permessi, il programma indica il comando `setfacl` per
concederli fino alla rimozione della chiavetta.

## Console e grafica

Le VM usano il terminale come console seriale: per chiuderle premi `Ctrl-A` e poi `X`.
Il sistema ospite deve avere una console seriale attiva (`console=ttyS0`): se il
terminale resta vuoto o si ferma dopo il BIOS, in genere è questo il motivo. Molte ISO
di installazione, come quella di Debian, e molti sistemi installati scrivono solo sullo
schermo: con questi usa SPICE. Windows in pratica non è utilizzabile dalla console seriale.

Tutte le opzioni di avvio del menu chiedono lo schermo e salvano la scelta nella VM, così
ai prossimi avvii basta premere Invio. Senza una scelta salvata, le opzioni 7 e 8 propongono
SPICE e le altre la console seriale. Da riga di comando, `--display` e `--spice` valgono solo
per quell'avvio; altrimenti `sqemu64 start` usa la scelta salvata.

Se un modulo di QEMU manca, sQemu64 lo segnala all'avvio. Con `deps --install` sono
disponibili audio (`vm create --audio pipewire`) e SPICE (`--spice`). La finestra
diretta (`--display window`) richiede in più i moduli grafici di QEMU.

### Copia/incolla

- **Console seriale:** usa quello del terminale, di solito `Ctrl+Shift+C` e `Ctrl+Shift+V`.
- **SPICE:** serve l'agente nel sistema ospite: `spice-vdagent` su Linux con sessione
  grafica, `spice-guest-tools` su Windows.

## Sistema minimale

L'opzione 9 avvia la VM chiamata `shell`, da creare una sola volta:

```bash
sqemu64 vm create shell --disk ./shell.img
```

## Test

```bash
python3 -m unittest discover -s tests -t .
```

## Licenza

GPLv2, vedi [LICENSE](LICENSE).
