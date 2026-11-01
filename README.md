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
| 5 | Avviare | `sqemu64 start VM [--ram 4G] [--cpus 4] [--usb sdb]` |
| 6 | Dischi multipli | `sqemu64 start VM --disk ALTRO` |
| 7 | CD Live | `sqemu64 start VM --iso FILE.iso [--usb sdb]` |
| 8 | CD Live con dischi multipli | `sqemu64 start VM --iso FILE.iso --disk ALTRO` |
| 9 | Sistema minimale | `sqemu64 shell [--usb sdb]` |
| 10 | SPICE | `sqemu64 start VM --spice 5900` |
| 11 | Aprire la cartella | `sqemu64 folder` |
| 12 | Elenco | `sqemu64 list` |
| 13 | Dipendenze | `sqemu64 deps [--install]` |

Con `--dry-run` i comandi `start` e `shell` mostrano la riga di QEMU senza eseguirla.

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

- `user` (predefinita): accesso a Internet senza alcuna configurazione.
- `tap:<interfaccia>`: usa un'interfaccia già creata, per esempio con
  `sudo ip tuntap add tap0 mode tap user $USER && sudo ip link set tap0 up`.
- `bridge:<bridge>`: collega la VM a un bridge tramite `qemu-bridge-helper`, senza root a
  ogni avvio. Se manca qualcosa, sQemu64 mostra i comandi da lanciare. Per portare le VM
  sulla LAN serve una scheda Ethernet nel bridge: con il Wi-Fi di solito non funziona.
- `none`: nessuna rete.

### Bridge

Per creare un bridge isolato, in cui le VM comunicano con il computer host:

```bash
sudo ip link add br0 type bridge
sudo ip addr add 192.168.77.1/24 dev br0
sudo ip link set br0 up
sudo mkdir -p /etc/qemu
echo 'allow br0' | sudo tee -a /etc/qemu/bridge.conf
sqemu64 vm create nome --size 40G --network bridge:br0
```

- Un bridge isolato non dà accesso a Internet e non ha un server DHCP: nel sistema ospite
  va assegnato un indirizzo manualmente, per esempio `192.168.77.2/24`.
- Il bridge sparisce al riavvio e va ricreato, mentre l'autorizzazione in `bridge.conf` resta.
- Per dare alle VM un indirizzo nella LAN, collega al bridge una scheda Ethernet, in genere
  con NetworkManager. Questo caso non è ancora stato provato.
- Con il solo Wi-Fi usa la rete `user`: la maggior parte delle reti Wi-Fi non accetta il
  MAC di una VM collegata tramite bridge.

## Chiavette USB

sQemu64 accetta solo dischi USB smontati e rifiuta i dischi interni. Elenco con
`sqemu64 usb`. Se mancano i permessi, il programma indica il comando `setfacl` per
concederli fino alla rimozione della chiavetta.

## Console e grafica

Le VM usano il terminale come console seriale: per chiuderle premi `Ctrl-A` e poi `X`.
Il sistema ospite deve avere una console seriale attiva (`console=ttyS0`): se il
terminale resta vuoto, in genere è questo il motivo. Molte ISO di installazione
scrivono solo sullo schermo, mentre Windows in pratica non è utilizzabile così.

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
