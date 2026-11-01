"""Unico punto in cui vengono eseguiti comandi esterni, sempre senza shell."""

import subprocess

from .validate import SqemuError

_MISSING = "Comando non trovato: {}. Controlla le dipendenze con: sqemu64 deps"


def run(argv):
    """Esegue un comando collegato al terminale e segnala gli errori."""
    try:
        code = subprocess.run(argv, check=False).returncode
    except FileNotFoundError:
        raise SqemuError(_MISSING.format(argv[0])) from None
    if code != 0:
        raise SqemuError(f"{argv[0]} è terminato con codice di errore {code}.")


def probe(argv, stdin_text):
    """Esegue un comando di prova e ne restituisce l'output, anche se fallisce."""
    try:
        result = subprocess.run(
            argv, check=False, input=stdin_text, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=15
        )
    except FileNotFoundError:
        raise SqemuError(_MISSING.format(argv[0])) from None
    except subprocess.TimeoutExpired:
        raise SqemuError(f"{argv[0]} non ha risposto alla prova.") from None
    return result.stdout


def capture(argv):
    """Esegue un comando e ne restituisce l'output."""
    try:
        result = subprocess.run(argv, check=False, capture_output=True, text=True)
    except FileNotFoundError:
        raise SqemuError(_MISSING.format(argv[0])) from None
    if result.returncode != 0:
        raise SqemuError(f"{argv[0]}: {result.stderr.strip()}")
    return result.stdout
