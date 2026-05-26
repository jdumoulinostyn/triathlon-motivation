#!/usr/bin/env python3
"""
Eenmalig hulpscript: haal de opgeslagen TrainingPeaks cookie op.
Kopieer de output naar GitHub Secrets als 'TP_AUTH_COOKIE'.

Voer dit UIT op jouw lokale machine (niet in GitHub Actions).
"""

import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

try:
    from tp_mcp.auth.storage import get_credential
except ImportError:
    print("FOUT: tp_mcp niet gevonden.")
    print("Zorg dat je in de juiste map staat en de venv geactiveerd is.")
    sys.exit(1)

result = get_credential()

if not result.success or not result.cookie:
    print("FOUT: Geen opgeslagen cookie gevonden.")
    print("Voer eerst 'tp-mcp auth' uit om je TrainingPeaks account te koppelen.")
    sys.exit(1)

print()
print("=" * 70)
print("  Jouw TrainingPeaks cookie — kopieer de waarde hieronder")
print("=" * 70)
print(result.cookie)
print("=" * 70)
print()
print("Stappen om dit als GitHub Secret toe te voegen:")
print("  1. Ga naar jouw GitHub repo → Settings → Secrets and variables → Actions")
print("  2. Klik op 'New repository secret'")
print("  3. Naam:  TP_AUTH_COOKIE")
print("  4. Waarde: plak de cookie hierboven (de volledige regel tussen de lijnen)")
print("  5. Klik op 'Add secret'")
print()
print("De cookie verloopt na enkele weken. Als de notificaties stoppen,")
print("voer dan 'tp-mcp auth' opnieuw uit en herhaal dit script.")
