#!/usr/bin/env bash
# Prepara le VM impostate su un bridge, ciascuna con il suo IP, per esempio dopo un riavvio.
# Le VM già pronte o accese non vengono toccate.
# Uso: ./bridge.sh [BRIDGE]   (senza argomenti: tutti i bridge usati dalle VM)

exec "$(dirname "$(realpath "${BASH_SOURCE[0]}")")/sqemu64" network prepare "$@" --yes
