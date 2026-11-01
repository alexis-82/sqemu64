#!/usr/bin/env bash
# Installa sQemu64 per l'utente corrente: comando, icona e voce nel menu delle applicazioni.
# Non richiede root. Il comando resta collegato a questa cartella: se la sposti, rilancia lo script.

set -euo pipefail

project_dir="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")" && pwd)"
data_home="${XDG_DATA_HOME:-$HOME/.local/share}"

bin_dir="$HOME/.local/bin"
link="$bin_dir/sqemu64"
icon="$data_home/icons/hicolor/128x128/apps/sqemu64.png"
desktop="$data_home/applications/sqemu64.desktop"

usage() {
    cat <<EOF
Uso: ./install.sh [--uninstall]

  (nessuna opzione)  installa il comando, l'icona e la voce nel menu
  --uninstall        li rimuove; VM e dischi non vengono toccati
EOF
}

die() {
    echo "Errore: $1" >&2
    exit 1
}

refresh_menus() {
    if command -v update-desktop-database >/dev/null; then
        update-desktop-database -q "$data_home/applications" || true
    fi
    if command -v gtk-update-icon-cache >/dev/null && [[ -f "$data_home/icons/hicolor/index.theme" ]]; then
        gtk-update-icon-cache -q -t "$data_home/icons/hicolor" || true
    fi
}

install_all() {
    # Il comando viene scritto tra virgolette nel file .desktop: questi caratteri richiederebbero altre regole.
    case "$link" in
        *[\"\`\$\\]*) die "il percorso $link contiene caratteri non supportati dal file .desktop." ;;
    esac
    if [[ -e "$link" || -L "$link" ]] && [[ ! -L "$link" ]]; then
        die "$link esiste già e non è un collegamento: non lo sovrascrivo."
    fi
    if [[ -L "$link" && "$(realpath -m "$link")" != "$project_dir/sqemu64" ]]; then
        die "$link punta a $(readlink "$link"): rimuovilo a mano se vuoi sostituirlo."
    fi

    mkdir -p "$bin_dir" "$(dirname "$icon")" "$(dirname "$desktop")"
    chmod +x "$project_dir/sqemu64"
    ln -sfn "$project_dir/sqemu64" "$link"
    install -m 644 "$project_dir/install_os/sqemu64.png" "$icon"

    # Percorso assoluto: il menu grafico spesso non ha ~/.local/bin nel PATH.
    local line temporary
    temporary="$desktop.tmp"
    while IFS= read -r line; do
        case "$line" in
            Exec=*) printf 'Exec="%s"\n' "$link" ;;
            *) printf '%s\n' "$line" ;;
        esac
    done <"$project_dir/install_os/sqemu64.desktop" >"$temporary"
    chmod 644 "$temporary"
    mv "$temporary" "$desktop"

    refresh_menus

    echo "sQemu64 installato:"
    echo "  comando: $link"
    echo "  icona:   $icon"
    echo "  menu:    $desktop"
    case ":$PATH:" in
        *":$bin_dir:"*) ;;
        *)
            echo
            echo "Attenzione: $bin_dir non è nel PATH, quindi 'sqemu64' non si avvia da terminale."
            echo "  fish: fish_add_path $bin_dir"
            echo "  bash/zsh: aggiungi al file di configurazione: export PATH=\"$bin_dir:\$PATH\""
            ;;
    esac
}

uninstall_all() {
    if [[ -L "$link" && "$(realpath -m "$link")" == "$project_dir/sqemu64" ]]; then
        rm "$link"
    elif [[ -e "$link" || -L "$link" ]]; then
        echo "Lascio $link: non appartiene a questa installazione."
    fi
    rm -f "$icon" "$desktop"
    refresh_menus
    echo "sQemu64 rimosso. VM e dischi restano in $data_home/sqemu64."
}

if [[ "$(id -u)" -eq 0 ]]; then
    die "non eseguire questo script come root: installa per il tuo utente."
fi

case "${1:-}" in
    "") install_all ;;
    --uninstall) uninstall_all ;;
    -h | --help) usage ;;
    *)
        usage >&2
        exit 2
        ;;
esac
