# Stud.IP File Scraper – Uni Osnabrück

Lädt automatisch alle Dateien aus deinen Stud.IP-Kursen herunter und speichert sie sortiert nach Kursname.

## Einrichtung

```bash
# 1. Abhängigkeiten installieren
/opt/homebrew/bin/pip3 install python-dotenv playwright --break-system-packages
/opt/homebrew/bin/python3 -m playwright install chromium

# 2. Login-Daten hinterlegen
cp .env.example .env
# .env öffnen und ausfüllen:
#   STUDIP_USERNAME=dein_uni_kürzel
#   STUDIP_PASSWORD=dein_passwort   ← Sonderzeichen wie # in Anführungszeichen: "passwort#123"
```

## Verwendung

### Alle Kurse des aktuellen Semesters herunterladen

Lädt automatisch alle Kurse aus **SoSe 2026** (immer das erste Semester auf der my_courses-Seite):

```bash
/opt/homebrew/bin/python3 scraper.py
```

Dateien landen in:
```
/Users/maxmacbookpro/Documents/Uni/Cognitive Science [Course]/Courses/
├── Action & Cognition (Motor System)/
│   ├── Vorlesung_01.pdf
│   └── ...
├── Introduction to Deep Learning/
│   └── ...
```

---

### Einen bestimmten Kurs herunterladen

1. Stud.IP im Browser öffnen und auf den gewünschten Kurs klicken
2. Die URL aus der Adressleiste kopieren
3. Mit `--url` übergeben:

```bash
/opt/homebrew/bin/python3 scraper.py --url "<URL hier einfügen>"
```

**Beispiel:**
```bash
/opt/homebrew/bin/python3 scraper.py --url "https://studip.uni-osnabrueck.de/dispatch.php/course/overview?cid=126c88e154f4aabfeb43f38050b68d28"
```

Die URL kann verschiedene Formen haben — das Skript erkennt alle automatisch.

---

### Weitere Optionen

| Option | Beschreibung |
|---|---|
| `--url <URL>` | Nur diesen einen Kurs scrapen |
| `--output <Pfad>` | Anderes Ausgabeverzeichnis (Standard: Courses-Ordner) |
| `--no-headless` | Browser sichtbar machen (gut zum Debuggen) |
| `--debug` | Ausführliches Logging |

**Beispiel mit eigenem Ausgabepfad:**
```bash
/opt/homebrew/bin/python3 scraper.py --output ~/Desktop/Uni-Dateien
```

---

## Hinweise

- **Bereits vorhandene Dateien** werden nicht erneut heruntergeladen.
- **Unterordner** innerhalb von "Dateien" werden automatisch als Unterordner lokal gespiegelt.
- Der Scraper nutzt die **Stud.IP REST API** (`/api.php/...`) für Downloads — schnell und zuverlässig.
- Login-Daten liegen nur lokal in `.env` und werden nie übertragen.
