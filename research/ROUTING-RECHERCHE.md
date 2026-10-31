# Bestehende Implementierungen und Algorithmen

Stand: 31.10.2026. Ziel: KiCad 10, zwei Kupferlagen, einfache THT/SMD-Bauteile,
organische Leiterbahnen mit grosszuegiger, lokal gleichmaessiger Verteilung.

Dies ist eine Recherche mit ausgewaehlter Quellcodeinspektion. Keines der
untersuchten Programme wurde hier gebaut, installiert oder auf unseren sechs
Benchmarks ausgefuehrt. Ergebnisse aus fremden Validierungsberichten sind
entsprechend gekennzeichnet.

## Wichtigstes Ergebnis

**Topologisches Routing plus Wire-spreading plus Bogenoptimierung** passt am
besten zum Ziel. Topola ist der engste offene Formverwandte. Fuer KiCad-IPC,
atomare Uebernahme und Regelabbildung ist J-Kreisels Autorouter ein besonders
interessantes separates Beispiel. Native Bogenkonstruktion ist bei mitxela
bereits praktisch umgesetzt.

Die zusaetzliche Nutzeranforderung ist entscheidend: Leiterbahnen sollen freie
Flaeche nutzen und mit ruhigen Abstaenden verteilt sein. Ein gespannter,
kuerzester Gummibandpfad kann sich trotz schoener Boegen eng an andere Bahnen
und Hindernisse anlegen. Daher ist dieser Pfad nur ein Ausgangszustand.

## Implementierungen mit konkretem Nutzen

| Projekt | Beitrag | Grenze fuer unser Ziel |
| --- | --- | --- |
| [Topola](https://topola.dev/) | Rust, topologisches Rubberband-Routing, native interne Bogenformen, GUI und CLI | Ausdruecklich unfertig. Automatische Verteilung grosszuegiger Abstaende muss gesondert untersucht werden. |
| [J-Kreisel/kicad-autorouter](https://github.com/J-Kreisel/kicad-autorouter) | Python/Rust, KiCad-IPC, von KiCad getrennter Kern und eine Undo-Transaktion | Experimentelles MVP mit 45-Grad-Routing, keine fertige Handstil-Engine. |
| [mitxela/kicad-round-tracks](https://github.com/mitxela/kicad-round-tracks) | Native KiCad-Boegen, Verrundung und Behandlung von Verzweigungen | Nachbearbeitung vorhandener Tracks; routet keine fehlenden Netze. |
| [tscircuit-autorouter](https://github.com/tscircuit/tscircuit-autorouter) | TypeScript, Hypergraph-/Pipeline-Ansatz, separate Geometrieeingabe | Fuer KiCad braucht es einen Adapter; im geprueften README kein Nachweis fuer unsere Handstil-/Verteilungsanforderung. |
| [freerouting/freerouting](https://github.com/freerouting/freerouting) | Vergleich fuer Netzverbindung, Umrouten und Optimierung | Keine belegte fertige Loesung fuer unsere grosszuegig verteilte Handstil-Geometrie. |

### Topola genauer

Im gepinnten Quellstand sind `PrimitiveShape::Bend`, ein Theta*-Stepper sowie
Multilayer- und Via-Module vorhanden. `src/router/thetastar.rs` beschreibt
zusaetzliches wiederholtes Rueckverfolgen von Vorgaengern. Die oeffentliche
Entwicklungsdokumentation erklaert Delaunay-basierte Navigation und eine
alternative Voronoi-Navigation. Das ist ein guter Ansatzpunkt fuer die
Entscheidung, auf welcher Seite von Hindernissen eine Verbindung verlaufen soll.

Wichtiger Integrationsbefund: In `src/specctra/design.rs`, Zeilen 121–127,
werden interne Boegen fuer den SES-Export mit festem Segmentwert `100` in
Geraden angenaehert. Ein einfacher DSN/SES-Rundlauf liefert daher nicht die
wenigen editierbaren nativen KiCad-Boegen, die wir anstreben. Ein direkter
Export der internen Geometrie als `ArcTrack` ist ein eigener Integrationsschritt.

Im [Bericht vom August 2026](https://topola.dev/blog/2026/08/31/2026-ngi0-commons-grant/)
nennt das Projekt Leistungsprobleme und einen Architekturumbau. Auch der
untersuchte Multilayer-Rekonfigurationscode enthaelt einen FIXME-Hinweis auf
Zustandskorruption. Das belegt einen offenen Entwicklungsstand, aber keine von
uns reproduzierte Fehlfunktion. Vor Uebernahme sind End-to-End-Tests auf den
kleinen Benchmarks notwendig. Lizenz laut README: MIT, mit gesonderten
Hinweisen fuer Assets und einzelne Dateien.

### KiCad-Integration und etablierte Bogen-Nachbearbeitung

J-Kreisels `kipy_adapter.py` ist fuer Netzkategorien, vorhandene Boegen,
Projektregeln und Undo-Commits interessant. Das README benennt konservative
Pad-Hindernisse und Grenzen bei allgemeinen Custom Rules. Der
[Validierungsbericht](https://github.com/J-Kreisel/kicad-autorouter/blob/efd7f8e065f9bff88cd878aa2afd2ef12d23d678/docs/validation.md)
meldet erfolgreiche KiCad-10.0.6-Versuche unter anderem mit einer unrouteten
ECC83-Demo. Diese Resultate wurden hier nicht nachgemessen. Das Projekt ist
MIT-lizenziert und eignet sich als erster Adapter-Vergleichskandidat.

Mitxelas Plugin erzeugt wahlweise native Boegen oder viele kurze Segmente und
verwendet die bisherige `pcbnew`-API. In `round_tracks_action.py` und
`round_tracks_utils.py` finden sich konkrete Loesungen fuer Radiuswahl,
Trackverkuerzung, Pad-Uebergaenge und Verzweigungen. Geometrische Vorarbeit
ist damit vorhanden; eine moderne IPC-Anbindung und vollstaendige Pruefung
der veraenderten Abstaende bleiben unsere Aufgaben. Hauptlizenz: Apache-2.0.

Tscircuits README beschreibt successive approximation, Hypergraphen und einen
eigenstaendigen `SimpleRouteJson`-Vertrag. Das macht den Code fuer die Trennung
von Adapter, Netzplanung und Detailrouting interessant. Dieser Vertrag bildet
jedoch nicht automatisch jede KiCad-Regel ab. Lizenz: MIT.

### Weitere Referenzen

- [TopoR von Eremex](https://www.eremex.ru/products/delta-design/topor/):
  topologisches Routing in freien Richtungen, mit dokumentierten
  Bogenvarianten. Als funktionaler/visueller Vergleich interessant; hier wurde
  kein offener wiederverwendbarer Quellkern nachgewiesen.
- [gEDA Toporouter](https://github.com/BenBergman/geda-pcb/blob/master/src/toporouter.h):
  historische offene topologische Implementierung. Eine moegliche
  Algorithmenreferenz, keine belegte aktuelle KiCad-10-Integration.
- [Copperroute](https://github.com/emshotton/copperroute): weiterer Rust-Router
  mit KiCad/DSN/SES-Workflow. Laut README GPL-3.0-only und mit aus Freerouting
  beziehungsweise KiCad portierten Teilen. Daher eine ergaenzende Vergleichs-
  und Architekturquelle, kein hier ausgewaehlter Handstil-Kern.

Die Namen sind zu unterscheiden: `freerouting/freerouting` ist das Java-Projekt
auf GitHub. Die Website `freerouting.org` dokumentiert derzeit eine separate
C++-/Qt-Linie. Ihre Algorithmenbeschreibung darf nicht als Quellcodebeschreibung
des GitHub-Projekts ausgegeben werden. Im bisherigen Konzept war dieser
Quellenunterschied nicht ausreichend klar; die Referenzen werden getrennt.

## Algorithmen fuer die einzelnen Aufgaben

### 1. Topologie und Wege: Theta*, Navmesh, Rubberband

Die [Autorenseite zu Theta*](https://idm-lab.org/project-o-content.html) bietet
Papers und Referenzcode. Theta* sucht Wege mit freien Richtungen; es erzeugt
allein noch keine weichen Boegen und ist keine Garantie fuer global kuerzeste
Pfade. Fuer uns muss jede Sichtverbindung gegen die wirklichen Hindernisse
einschliesslich Leiterbahnbreite und Abstand geprueft werden.

Topologische Suchzustaende halten fest, auf welcher Seite von Pads/Hindernissen
und in welcher Reihenfolge benachbarte Bahnen laufen. Eine Navmesh oder ein
Sichtbarkeitsgraph reduziert die Suche. Ein Rubberband-Schritt findet danach
einen Verlauf innerhalb dieses Korridors. Das liefert die Formfreiheit fuer
spaetere Verteilung und verhindert, dass reine Eckverrundung die gesamte
Topologie bestimmt. Welche Variante fuer unsere kleinen Boards schneller und
robuster ist, bleibt eine Benchmarkfrage.

### 2. Freie Flaeche verteilen: Wire-spreading

Der unmittelbar passende Literaturfund ist Hama/Etoh:
[Konferenzarbeit von 1998](https://www.cecs.uci.edu/~papers/compendium94-03/papers/1998/aspdac98/pdffiles/06c_3.pdf)
und [Journalfassung von 1999 bei IBM Research](https://research.ibm.com/publications/curvilinear-detailed-routing-with-simultaneous-wire-spreading-and-wire-fattening).
Die Arbeit behandelt kurvige Detailverdrahtung und verteilt uebrigen Platz
durch vergroesserte lokale Sperrbereiche. Freier Platz kann fuer zusaetzlichen
Abstand und/oder breitere Leiterbahnen eingesetzt werden; die Topologie bleibt
Ausgangspunkt. Abbildung 7 der Konferenzarbeit zeigt den Unterschied zwischen
kompakter und aufgefaecherter Verdrahtung. Obwohl die Beispiele auch
Array-Gehaeuse behandeln, ist die geometrische Idee fuer unsere einfachen
Platinen relevant. Eine direkt uebernehmbare offene Implementierung dieser
Arbeit wurde nicht nachgewiesen.

Unser erster Entwurf verwendet **konstante Breiten und variablen Freiraum**.
In einem Kanal mit mehreren Bahnen ermitteln wir nach Abzug der Breiten und
zwingenden Abstaende die restliche Breite und verteilen sie auf die Luecken.
Pinabgaenge und Engstellen duerfen dichter bleiben. Es geht um lokale,
ruhige Verteilung, nicht darum, jede freie Ecke mit langen Umwegen zu fuellen.

Ein global vergroesserter DRC-Mindestabstand waere kein gleichwertiger Ersatz:
Er kann enge Padabgaenge unmoeglich machen und beruecksichtigt lokalen Platz
nicht. Der gewuenschte grosszuegige Abstand ist ein eigenes Gestaltungsziel;
der vorgeschriebene Mindestabstand bleibt eine harte Untergrenze.

### 3. Mehrere Netze und zwei Lagen: Umrouten und verhandelte Engstellen

[PathFinder von McMurchie/Ebeling](https://janders.eecg.utoronto.ca/1387_2015/readings/pathfinder.pdf)
beschreibt iterative Kosten fuer knappe Routing-Ressourcen. Das Paper stammt
aus dem FPGA-Routing; seine Gitterressourcen sind keine PCB-Kupfergeometrie.
Die Idee ist trotzdem nuetzlich: Engpaesse werden teurer, Netze werden neu
geordnet und umgelegt. J-Kreisels Validierungsbericht beschreibt einen solchen
Ansatz fuer seinen PCB-Router. Provisorische Konflikte gehoeren nur in den
internen Suchzustand; ausgegeben wird ausschliesslich ein geprueftes Ergebnis.

Fuer zwei Kupferlagen benoetigen wir auch Via-Geometrie, Bohr-/Kupferabstaende,
legale Start-/Ziellagen und Kosten fuer Lagenwechsel. Viafreiheit des Originals
ist keine Zusage, dass unser anderer Kurvenverlauf ebenfalls ohne Vias auskommt.

### 4. Formoptimierung: gekoppeltes Glaetten und Abstandshalten

Die [CHOMP-Arbeit](https://publications.ri.cmu.edu/chomp-covariant-hamiltonian-optimization-for-motion-planning)
ist ein Beispiel fuer kontinuierliche Optimierung von Glattheit und
Hindernisnaehe. Sie liefert keinen fertigen PCB-Router. Die uebertragbare Idee
ist ein eigener Optimierungsschritt fuer vorhandene Wege.

Unser Vorschlag: Nach gueltiger Topologie mehrere benachbarte Wege gemeinsam
optimieren. Bewertet werden lokale Lueckenverteilung, Radiuswechsel, sanfte
Richtungen und begrenzte Zusatzlaenge. Padkontakte und noetige
Verzweigungen bleiben fest. Das bisher angedachte Bevorzugen enger Buendel
entfaellt als Standard. Auch ein Potentialfeld allein garantiert keine
kollisionsfreie Loesung; jeder akzeptierte Schritt braucht eine harte
Geometriepruefung und eine Rueckkehr zum letzten gueltigen Zustand.

### 5. KiCad-Ausgabe: Biarcs und toleranzgepruefte Kreisboegen

Ein Biarc verbindet zwei Punkte mit vorgegebenen Tangenten durch zwei
Kreisboegen. [Bertolazzi/Frego 2017](https://arxiv.org/abs/1711.00935) behandeln
robuste Berechnung auch bei schwierigen Winkelkonfigurationen. Biarcs sind
G1-glatt, also tangentenstetig; sie sind nicht automatisch kruemmungsstetig.
Fuer unsere sichtbare Linienfuehrung ist das ein geeigneter erster Ansatz.

Die [Clothoids-Bibliothek](https://github.com/ebertolazzi/Clothoids) enthaelt
konkrete Biarc- und CircleArc-Implementierungen. Der untersuchte Lizenztext
hat BSD-2-Clause-Bedingungen. Echte Clothoiden sind als internes Modell
moeglich, muessen fuer native KiCad-Tracks aber wieder in Kreisboegen und
Geraden ueberfuehrt werden. Endpunkte, Netze, Breiten, Lagen und die gesamte
endgueltige Kupferflaeche einschliesslich Rundungsfehler pruefen.

## Geeignete Geometriebibliotheken

- [Cavalier Contours, Rust](https://github.com/jbuckmccready/cavalier_contours):
  Linien und echte Kreisboegen, Offsets und boolesche Operationen. Interessant
  fuer Abstandsbereiche und Kanalraender. MIT oder Apache-2.0. Das README nennt
  Grenzen etwa fuer Boegen ueber 180 Grad und bestimmte sich schneidende
  Konturen; eine vollstaendige DRC entsteht daraus nicht automatisch.
- [Cyrk](https://codeberg.org/topola/cyrk): exakter Geometriekern fuer Linien,
  Kreise, Boegen und konvexe lineocirculare Polygone; MIT oder Apache-2.0.
  Wertvoll fuer numerische Grenzfaelle. Der Topola-Bericht von Juni 2025 nennt
  die Bibliothek experimentell und zu diesem Zeitpunkt noch nicht integriert;
  daraus folgt keine Aussage ueber eine heute vollstaendige Integration.

## Angepasste Entwicklungsentscheidung

Ein Komplettneubau wird noch nicht festgelegt. Die naechsten technischen
Vergleiche sollen die Wiederverwendung klaeren:

1. Topola auf einem kleinen, kontrollierten Zweilagenfall auf Import,
   Konnektivitaet, Via-Verhalten und Laufzeit untersuchen. Tatsaechlichen
   Geometrieexport getrennt bewerten.
2. Anhand des J-Kreisel-Adapters den KiCad-10-Snapshot und die atomare
   Rueckgabe von Tracks/Vias untersuchen; einen eigenen Bogen-Rundlauf testen.
3. Einen kleinen **Wire-spreading-Prototyp** mit drei bis fuenf Bahnen und
   festem Verlauf um Hindernisse entwickeln. Ziel: lokal aehnliche Luecken
   bei unveraenderten Breiten und sicheren Mindestabstaenden.
4. Danach Topologie, Verteilung und Bogenfit auf den gleichen realen
   Benchmark-Eingaben zusammenfuehren. Gerade, nur gerundete und verteilt
   organische Variante nebeneinander bewerten.

Erst die Tests entscheiden zwischen Topola als Backend, Wiederverwendung
einzelner Bibliotheken oder einem kleinen eigenen Kern. Aktuell wurden keine
Fremdimplementierungen in das Produkt uebernommen.

Zusaetzliche Stilmetriken: Verteilung lokaler Luecken pro Lage/Korridor,
Anteil laengerer Strecken am blossen Mindestabstand und Abstand zu benachbarten
Bahnen/Hindernissen in freien Bereichen. Unvermeidbare Padabgaenge und
Verzweigungen getrennt behandeln. Netzverbindung und DRC stehen vor der
Stilbewertung; Laenge und Viazahl bleiben sichtbare Vergleichswerte.

## Nachweise

[sources.json](sources.json) bindet acht Projekte und 22 gelesene Dateien an
feste Commit-IDs, URLs und SHA-256. Ausgewaehlte Rohquellen und der
read-only-Inspektionslauf liegen ignoriert im zentralen Projekt-`work/`.
Das Manifest enthaelt den exakten Aufruf, Werkzeugversion und Exitstatus.
Eine Quelltextstelle oder ein fremder Testbericht ist kein eigener Laufnachweis.
