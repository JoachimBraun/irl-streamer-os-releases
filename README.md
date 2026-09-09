# IRL Streamer OS - Releases

Oeffentliches Release-Repo fuer IRL Streamer OS. Enthaelt ausschliesslich
getestete, freigegebene Versionen - laufende Entwicklung findet in einem
separaten, privaten Repository statt.

## Wie Updates funktionieren

Jedes installierte IRL Streamer OS Geraet prueft taeglich die `VERSION`-Datei
in diesem Repo gegen seine lokal installierte Version. Ist eine neuere
Version verfuegbar, wird der Nutzer per Dialog gefragt, ob er aktualisieren
moechte - keine automatischen, unangekuendigten Aenderungen am System.

Ein Update wendet immer den VOLLSTAENDIGEN Ziel-Zustand der neuen Version an
(idempotente Provisionierung, kein Aufeinanderaufbauen einzelner
Zwischen-Patches) - das uebersringen mehrerer Versionen ist deshalb sicher
und fuehrt zum selben Ergebnis wie ein Update ueber jede einzelne
Zwischenversion.

## Versionsschema

`VERSION`-Datei enthaelt die aktuell empfohlene Version (z.B. `1.69`).
Jede veroeffentlichte Version hat zusaetzlich einen Git-Tag `vX.Y`.
