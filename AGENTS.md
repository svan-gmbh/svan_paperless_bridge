# SVAN Accounting - Agent Guidelines

## 🎯 Naming Conventions

### Custom Fields
- **MUSS**: Alle Feldnamen beginnen mit `svan_` (z. B. `svan_direct_debit`)

### Custom DocTypes
- **MUSS**: Alle eigenen DocTypes beginnen mit `Svan` (z. B. `SvanBankMandate`)

### Patch Files
- Naming: `add_svan_<feldname>_to_<doctype>.py`
- Beispiel: `add_svan_direct_debit_to_customer.py`

## 🔧 Patch Implementation

### Required Structure
```python
from svan_paperless_bridge.utils.patch_helpers import tracert

def execute():
    tracert(f"START {__file__}")
    # ... Logic ...
    tracert(f"END {__file__}")
```

### Field Creation Pattern
1. **Prüfe auf Existenz** (`frappe.db.get_value("Custom Field", {...})`)
2. **Idempotente Aktualisierung** falls Feld existiert
3. **`tracert()` für ALLE Meldungen** (nicht `frappe.msgprint` direkt)

### Tracing (tracert)
- **MUSS** in jeder Patch-Datei verwendet werden
- Ersetzt `print()`, `frappe.logger()`, und `frappe.msgprint()`
- Position: START, END, Success, Error, und alle logischen Schritte

## 📝 Commit Guidelines

### Commit Message Format
- **MUSS**: Branch-Name (Ticketnummer) als erstes in der Commit-Message
- Format: `<branch_name>: <Beschreibung>`
- Beispiel: `M12645: svan_direct_debit Feld zum Customer DocType hinzugefügt`

## ✅ Checklist Before Commit
- [ ] Feldname beginnt mit `svan_`
- [ ] DocType-Name beginnt mit `Svan` (falls zutreffend)
- [ ] `tracert()` importiert und für alle Meldungen verwendet
- [ ] Patch in `patches.txt` eingetragen
- [ ] Idempotenz implementiert
- [ ] Docstring mit Zweck vorhanden
- [ ] Commit-Message beginnt mit Branch-Name (Ticketnummer)

nur ein Test bzgl. PR
