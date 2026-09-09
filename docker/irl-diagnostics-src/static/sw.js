// Absichtlich ohne Caching: dies ist ein Live-Diagnose-Dashboard, ein
// zwischengespeicherter alter Zustand (Signalwerte, Verbindungsstatus) waere
// hier aktiv irrefuehrend statt hilfreich. Der leere fetch-Handler existiert
// nur, damit Chrome auf Android die Seite als "installierbar" (PWA, ohne
// Adresszeile im Standalone-Fenster) erkennt - jede Anfrage geht normal ans
// Netzwerk, nichts wird abgefangen oder zwischengespeichert.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
