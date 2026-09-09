// IRL Streamer OS - Lizenz-/Testphasen-Anzeige unten rechts auf dem Desktop.
//
// Liest periodisch eine reine Textdatei (siehe license-widget-status.sh,
// dieses Skript kennt keinerlei Lizenzlogik selbst, nur die fertige
// Anzeigezeile) und positioniert ein dezentes, nicht anklickbares Label
// in der unteren rechten Bildschirmecke - unterhalb aller normalen
// Fenster (Main.layoutManager.addChrome), stoert also nie OBS/Guacamole/
// Browser im Vordergrund.
//
// Alarm-Zustand (Nutzerwunsch 2026-09-04): steht ein "!ALARM!"-Praefix vor
// dem Text (siehe license-widget-status.sh, write_alarm_text - gesetzt bei
// Admin-Sperre/abgelaufener Lizenz/abgelaufener Testphase), wird der Text
// STATT der dezenten grauen Standarddarstellung knallrot UND dauerhaft
// blinkend dargestellt, damit ein gesperrter/abgelaufener Zustand auf dem
// Desktop nicht uebersehen werden kann.
//
// Warn-Zustand (Nutzerwunsch 2026-09-04): steht stattdessen ein
// "!WARN!"-Praefix vor dem Text (siehe license-widget-status.sh,
// write_warn_text - gesetzt wenn eine aktive Jahreslizenz in unter 2
// Wochen ablaeuft), wird der Text knallgelb dargestellt - ausdruecklich
// OHNE Blinken (Nutzerwunsch: "kein Blinken einfach nur knalliges Gelb"),
// da die Lizenz hier noch aktiv/gueltig ist und nicht wie ein bereits
// gesperrter/abgelaufener Zustand wirken soll. ALARM und WARN sind
// exklusiv - ALARM hat Vorrang, falls (theoretisch) beide Praefixe
// vorlaegen.

import St from 'gi://St';
import GLib from 'gi://GLib';
import * as Extension from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const STATUS_FILE = '/opt/irl-streamer-os/state/license-widget-text.txt';
const UPDATE_INTERVAL_SECONDS = 30;
const MARGIN_PX = 12;
const ALARM_PREFIX = '!ALARM!';
const WARN_PREFIX = '!WARN!';
const BLINK_INTERVAL_MS = 600;

const STYLE_NORMAL =
    'font-size: 11px; font-weight: normal; color: rgba(255,255,255,0.6); ' +
    'background-color: rgba(0,0,0,0.32); padding: 3px 9px; border-radius: 6px;';
// Knallrot, deutlich groesser/fett als der Normalzustand, damit der
// Alarm-Zustand schon rein optisch (nicht erst durch den Text) sofort als
// "hier stimmt etwas nicht" erkennbar ist.
const STYLE_ALARM_ON =
    'font-size: 13px; font-weight: bold; color: #ff1a1a; ' +
    'background-color: rgba(40,0,0,0.55); padding: 4px 11px; border-radius: 6px;';
// "Aus"-Phase des Blinkens: Text bleibt an derselben Stelle sichtbar
// (Hintergrund/Groesse gleich), nur die Textfarbe wird stark abgedunkelt -
// vermeidet ein staendiges Springen/Neupositionieren der Box durch
// komplettes Ein-/Ausblenden, das bei sich aenderndem Text ohnehin schon
// noetig ist (_reposition()).
const STYLE_ALARM_OFF =
    'font-size: 13px; font-weight: bold; color: rgba(255,26,26,0.15); ' +
    'background-color: rgba(40,0,0,0.55); padding: 4px 11px; border-radius: 6px;';
// Knalliges Gelb, statisch (kein Blinken) - gleiche Groesse/Fettung wie
// der Alarm-Zustand, damit die Warnung ebenfalls optisch auffaellt, aber
// durch die Farbe klar als "weniger kritisch als rot" erkennbar bleibt.
const STYLE_WARN =
    'font-size: 13px; font-weight: bold; color: #ffe600; ' +
    'background-color: rgba(40,34,0,0.55); padding: 4px 11px; border-radius: 6px;';

export default class IrlLicenseWidgetExtension extends Extension.Extension {
    enable() {
        this._label = new St.Label({
            style_class: 'irl-license-widget-label',
            style: STYLE_NORMAL,
            reactive: false,
            can_focus: false,
            track_hover: false,
        });
        Main.layoutManager.addChrome(this._label);

        this._isAlarm = false;
        this._isWarn = false;
        this._blinkOn = true;
        this._blinkTimeoutId = null;

        this._updateAndReposition();
        this._timeoutId = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT,
            UPDATE_INTERVAL_SECONDS,
            () => {
                this._updateAndReposition();
                return GLib.SOURCE_CONTINUE;
            }
        );
        this._monitorsChangedId = Main.layoutManager.connect(
            'monitors-changed',
            () => this._reposition()
        );
    }

    _updateAndReposition() {
        this._updateLabel();
        // Erst nach dem naechsten Layout-Durchlauf repositionieren, sonst
        // ist this._label.width noch der alte (oder 0 beim allerersten
        // Aufruf), weil Clutter Groessenaenderungen asynchron anwendet.
        GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
            this._reposition();
            return GLib.SOURCE_REMOVE;
        });
    }

    _updateLabel() {
        let text = '';
        try {
            const [ok, contents] = GLib.file_get_contents(STATUS_FILE);
            if (ok) {
                text = new TextDecoder('utf-8').decode(contents).trim();
            }
        } catch (e) {
            text = '';
        }

        this._isAlarm = text.startsWith(ALARM_PREFIX);
        if (this._isAlarm) {
            text = text.slice(ALARM_PREFIX.length);
        }
        this._isWarn = !this._isAlarm && text.startsWith(WARN_PREFIX);
        if (this._isWarn) {
            text = text.slice(WARN_PREFIX.length);
        }

        if (!text) {
            this._label.hide();
            this._stopBlinking();
        } else {
            this._label.set_text(text);
            this._label.show();
            if (this._isAlarm) {
                this._startBlinking();
            } else if (this._isWarn) {
                this._stopBlinking();
                this._label.set_style(STYLE_WARN);
            } else {
                this._stopBlinking();
                this._label.set_style(STYLE_NORMAL);
            }
        }
    }

    _startBlinking() {
        if (this._blinkTimeoutId) return; // laeuft schon
        this._blinkOn = true;
        this._label.set_style(STYLE_ALARM_ON);
        this._blinkTimeoutId = GLib.timeout_add(
            GLib.PRIORITY_DEFAULT,
            BLINK_INTERVAL_MS,
            () => {
                this._blinkOn = !this._blinkOn;
                this._label.set_style(this._blinkOn ? STYLE_ALARM_ON : STYLE_ALARM_OFF);
                return GLib.SOURCE_CONTINUE;
            }
        );
    }

    _stopBlinking() {
        if (this._blinkTimeoutId) {
            GLib.source_remove(this._blinkTimeoutId);
            this._blinkTimeoutId = null;
        }
    }

    _reposition() {
        const monitor = Main.layoutManager.primaryMonitor;
        if (!monitor || !this._label) return;
        this._label.set_position(
            monitor.x + monitor.width - this._label.width - MARGIN_PX,
            monitor.y + monitor.height - this._label.height - MARGIN_PX
        );
    }

    disable() {
        this._stopBlinking();
        if (this._timeoutId) {
            GLib.source_remove(this._timeoutId);
            this._timeoutId = null;
        }
        if (this._monitorsChangedId) {
            Main.layoutManager.disconnect(this._monitorsChangedId);
            this._monitorsChangedId = null;
        }
        if (this._label) {
            Main.layoutManager.removeChrome(this._label);
            this._label.destroy();
            this._label = null;
        }
    }
}
