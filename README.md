# Auto-Hand-Router 0.1.0

Ein CLI-Router fuer **KiCad 10**, der Zweilagenplatinen mit grosszuegig
verteilten, geschwungenen Leiterbahnen erzeugt. Vorbild ist die ruhige
Linienfuehrung handgezeichneter Leiterplatten. Die Kurven sind echte native
KiCad-Kupferboegen.

Freerouting plant Verbindungen und Lagenwechsel. Unsere eigene Verarbeitung
erkennt getrennte Kupfergruppen, ergaenzt fehlende Verbindungen, verteilt
Leiterzuege und rundet sie unter Abstandskontrolle. Optional kann der gepinnte
J-Kreisel-Kern den Routingplan erzeugen. Ergebnisse werden erst nach
Geometriepruefung, KiCad-DRC und Schaltplanabgleich uebernommen.

## Ergebnisse

Alle Bilder unten wurden aus Routing-Ergebnissen erzeugt. Heruntergeladene
historische PCB-Fotos und andere externe Referenzbilder sind nicht enthalten.

### 555 Referenz

Spacious / curved: acht Netze, 22 geschlossene Verbindungen, zehn Vias und
133 native Boegen. Keine offenen Verbindungen oder neuen KiCad-DRC-Befunde.
Der kleine Referenzlauf funktioniert ohne externen Routingkern.

![555 Spacious curved](examples/555-reference/routed.png)

### Unser Router im Vergleich

Gleiche Platzierung und Netzliste. Oben F.Cu, unten B.Cu. Die Adapter und
Regeln sind im [Vergleichsbericht](comparison/README.md) dokumentiert.
Freerouting braucht hier vier Vias, unsere Stilreferenz zehn; die eigene
Ausgabe nutzt den freien Raum grosszuegiger.

![Auto-Hand-Router versus existing routers](comparison/results/comparison.png)

Einfaches Nachrunden eines anderen Routers ersetzt keine abstandsbewusste
Formgebung. Die getesteten mitxela-Varianten erzeugten neue Abstandsfehler:

![Freerouting and actual mitxela rounding](comparison/results/rounding.png)

### Drying Oven Compact

70 × 120 mm, gleiche Platzierung, zwei Kupferlagen. Die Netzteil-Montageflaeche
und Schraubenreserven bleiben kupferfrei. Vias unter aufgesteckten Modulen
sind erlaubt; Pad-, Bohrungs- und mechanische Sperrabstaende bleiben aktiv.

![Drying Oven Compact routed](docs/images/drying-oven-compact.png)

[KiCad-Projekt](examples/drying-oven-compact/drying-oven-compact.kicad_pro) ·
[Messwerte und Pruefstatus](examples/drying-oven-compact/summary.json)

### Drying Fridge

120 × 100 mm, 58 Footprints und 189 Pads. Alle Verbindungen sind geschlossen.
1.064 native Boegen und 45 Vias; keine neuen Routingfehler. Dieses Ergebnis
erfuellt sogar die strengere Regel ohne Vias in Bauteilumrissen.
Kabelbinderschlitze und ihre Sperrflaechen bleiben erhalten.

![Drying Fridge routed](docs/images/drying-fridge.png)

[KiCad-Projekt](examples/drying-fridge/drying-fridge.kicad_pro) ·
[Messwerte und Pruefstatus](examples/drying-fridge/summary.json)

Die Drying-Boards sind **Routing-Kandidaten fuer DRAFT-Hardware**. KiCad 10
meldet schon am Ausgangsstand Bibliotheks- und Symbolfeldwarnungen. Das
strengere Fridge-Profil meldet zudem acht Schrifthoehenwarnungen. Die Berichte
weisen diese aus; sie werden nicht als neue Routingfehler ausgegeben.
Modulmechanik und Laststromauslegung sind damit nicht freigegeben.

## Einrichtung

Getesteter Weg: Linux, Python 3.12+, [uv](https://docs.astral.sh/uv/),
**KiCad 10 mit CLI und `pcbnew`-Pythonmodul**. Der Interpreter fuer `pcbnew`
muss zur KiCad-Installation passen; er ist vom uv-Interpreter getrennt.
Fuer Freerouting wird **Java 25** benoetigt.

```sh
git clone https://github.com/ckbaker10/auto-hand-router.git
cd auto-hand-router
uv sync --locked
uv run --locked auto-hand-router --version
kicad-cli version
java -version
```

Das offizielle Freerouting-2.5.0-JAR separat herunterladen:

```sh
mkdir -p "$HOME/.local/share/auto-hand-router"
curl -fL https://github.com/freerouting/freerouting/releases/download/v2.5.0/freerouting-2.5.0.jar \
  -o "$HOME/.local/share/auto-hand-router/freerouting-2.5.0.jar"
sha256sum "$HOME/.local/share/auto-hand-router/freerouting-2.5.0.jar"
export AUTO_HAND_ROUTER_FREEROUTING_JAR="$HOME/.local/share/auto-hand-router/freerouting-2.5.0.jar"
```

Erwartete SHA-256:
`f6f51bb02245e8e717f9359bd260cc9c5c0b1bc0acc8b7cb2cd5b8ffeb5de3c7`.
Der Router prueft diesen Hash. Getestet ist das Java-JAR, nicht die native
Freerouting-Distribution.

## Eigenes Board per CLI routen

PCB, Projekt und Schaltplan muessen denselben Dateistamm haben, etwa
`controller.kicad_pcb`, `controller.kicad_pro`, `controller.kicad_sch`.
Vorhandene `.kicad_dru` und Bibliothekstabellen werden uebernommen.

```sh
uv run --locked auto-hand-router route \
  --board /pfad/zum/controller.kicad_pcb \
  --output-dir output/controller \
  --work-dir work/controller-001 \
  --via-policy module-interiors \
  --spacing 1.2 \
  --radius 2.5
```

Das Quellprojekt bleibt erhalten. Vorhandene Leiterbahnen werden nur in der
Arbeitskopie entfernt. `--work-dir` muss fuer jeden Lauf neu sein; dort liegen
DSN/SES, Checkpoints und rohe Pruefberichte. Das Ausgabeverzeichnis enthaelt
das native KiCad-Projekt, SVG/PNG und `report.json`. Fehlerhafte Laeufe
ersetzen kein vorher erfolgreiches Ergebnis.

| Option | Bedeutung |
|---|---|
| `--via-policy courtyards` | Standard: gesamte Bauteilumrisse fuer Vias sperren. |
| `--via-policy module-interiors` | Vias unter grossen bedrahteten Modulen erlauben; Pads, Bohrungen und mechanische Sperren bleiben geschuetzt. |
| `--spacing 1.2` | Zusaetzlicher weicher Freiraumwunsch in mm; keine Abschwaechung der Mindestabstaende. |
| `--radius 2.5` | Gewuenschter maximaler Bogenradius in mm; Engstellen verlangen kleinere Radien. |
| `--relax-steps 30` | Zahl der Schritte zur freien Verteilung der Leiterzuege. |
| `--route-timeout 600` | Zeitlimit des Freerouting-Aufrufs in Sekunden. |

Die Modulerkennung verlangt mindestens drei durchkontaktierte Pads, eine
Koerperflaeche ab 50 mm² und einen U-Referenznamen oder Modul-Footprintnamen.
Die erkannten Referenzen stehen im Report. Kleine SMD-Gehaeuse und reine
Montage-Footprints werden nicht pauschal freigegeben. Verzweigungen,
Padkontakte und Vias bleiben feste Anker der Formgebung.

### Routingprofil und Versorgungsnetze

Ohne `--profile` gelten die Projektregeln. Das konservative
[Beispielprofil](profiles/jlcpcb-2layer-1oz.json) setzt unter anderem
0,35-mm-Signale, 0,60-mm-Versorgungswege, 0,25-mm-Kupferabstand und
0,80/0,40-mm-Vias. Staerkere vorhandene Mindestregeln werden nicht abgesenkt.

```sh
uv run --locked auto-hand-router route \
  --board /pfad/zum/controller.kicad_pcb \
  --output-dir output/controller-profile \
  --work-dir work/controller-002 \
  --profile profiles/jlcpcb-2layer-1oz.json \
  --routing-config /pfad/zum/kicad-workflow.json \
  --via-policy module-interiors
```

Beispiel fuer `kicad-workflow.json`:

```json
{"routing": {"power_net_patterns": ["/+5V", "/+3V3", "/GND"]}}
```

Liegt diese Datei neben dem Quellprojekt, wird sie automatisch gelesen.
Profil und Netzklassen werden nur an der Kandidatenkopie angepasst. Die
Beispielbreiten sind keine Stromtragfaehigkeitsberechnung.

### Optionaler Routingkern

Der J-Kreisel-Kern ist an einen festen Quellcommit gebunden. Fuer seinen Build
kann eine Rust-Toolchain erforderlich sein; Java wird fuer diesen Kern nicht
benoetigt.

```sh
uv sync --locked --extra kreisel
uv run --locked --extra kreisel auto-hand-router route \
  --backend kreisel \
  --board /pfad/zum/controller.kicad_pcb \
  --output-dir output/controller-kreisel \
  --work-dir work/controller-003 \
  --via-policy module-interiors
```

Gespeicherte Kupfer-Checkpoints lassen sich mit `--seed-board <datei.kicad_pcb>`
fortsetzen, Specctra-Sessions mit `--seed-session <datei.ses>`. Physische
Geometrie und Netze werden dabei erneut geprueft.

### Eigene KiCad-Installation

Bei einer Installation ausserhalb des Suchpfads diese Optionen ergaenzen:

```sh
  --kicad-cli /pfad/zu/kicad-cli \
  --kicad-python /pfad/zum/passenden/python3 \
  --java /pfad/zu/java \
  --freerouting-jar /pfad/zu/freerouting-2.5.0.jar
```

Alternativ die Variablen `AUTO_HAND_ROUTER_KICAD_CLI`,
`AUTO_HAND_ROUTER_KICAD_PYTHON`, `AUTO_HAND_ROUTER_JAVA` und
`AUTO_HAND_ROUTER_FREEROUTING_JAR` setzen.

## 555 Referenzlauf und Entwicklung

```sh
uv run --locked auto-hand-router --comparison --output-dir output/555
uv run --locked pytest -q
uv run --locked ruff check auto_hand_router comparison tests
uv build
```

Der 555-Lauf nutzt den eigenen A*-Referenzrouter. Fuer zusaetzliche native
Pruefung `--kicad-cli /pfad/zu/kicad-cli --evidence-dir work/555-drc` ergaenzen.

## Unterstuetzung und Grenzen von 0.1.0

Zwei Kupferlagen, Bauteile vorne, numerische und neue benannte KiCad-Netze,
Kreise/Rechtecke/Ovale/Roundrect-Pads, runde Bohrungen und Langloecher,
polygonale Rule Areas und vorhandene Kupferzonen. Zonen werden fuer die
Verbindungsplanung ausgeblendet und danach neu gefuellt. Unterstuetzte
Custom Rules betreffen Pad-Restringe und Bohrungsmindestmasse; unbekannte
elektrische Bedingungen werden ausdruecklich abgelehnt.

Beliebige Mehrlagenplatinen, komplexe Padformen, Rueckseitenbestueckung und
beliebige Custom Rules sind nicht abgedeckt. Eine Loesung fuer jede routbare
Platzierung wird nicht garantiert. Die Pruefungen vergleichen neue Befunde
mit dem Ausgangsstand unter denselben Regeln und kontrollieren die
Kupferverbindungen zusaetzlich unabhaengig.

## Quellen und Bildrechte

Alle eingebetteten Bilder sind aus den dokumentierten Layouts erzeugt.
Externe historische Referenzbilder werden nicht eingecheckt oder eingebettet.
Die 555-Eingabe stammt aus
[Adrian-dre/KiCad_555_Timer_Clock_demo](https://github.com/Adrian-dre/KiCad_555_Timer_Clock_demo)
und ist MIT-lizenziert; der Copyright-/Lizenztext liegt bei den Dateien.
Die Drying-Ergebnisbilder stammen aus den eigenen KiCad-Projekten.

[Freerouting](https://github.com/freerouting/freerouting) ist ein separat
aufgerufener GPL-3.0-Router, der optionale
[J-Kreisel-Kern](https://github.com/J-Kreisel/kicad-autorouter) ist MIT-lizenziert.
Weitere Quellen und Adapter: [comparison/](comparison/README.md).
Fremdbinaerdateien und lokale KiCad-Konfiguration werden nicht mitgeliefert.
