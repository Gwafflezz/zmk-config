#!/usr/bin/env python3
"""
Corne layer overlay
===================

Desenha uma miniatura do *seu* Corne no canto superior direito da tela e troca
a camada exibida ao vivo, sempre que ela é ativada no teclado.

Como funciona (híbrido USB + BT + Studio + FileWatcher):
  - USB: o firmware (zmk-keypeek-layer-notifier + zmk-raw-hid) envia um report
    Raw HID de 32 bytes com o bitmask das camadas ativas via /dev/hidrawX.
  - BT: os macros `lower_mo`/`raise_mo` no keymap emitem F13/F14 (lower) e
    F15/F16 (raise) ao entrar/sair de cada camada. O overlay escuta via evdev
    (/dev/input/eventX) — funciona igual por USB e Bluetooth.
  - ZMK Studio: se conectado via CDC-ACM (/dev/ttyACMx), lê as alterações ao vivo do teclado.
  - FileWatcher: monitora alterações no arquivo corne.keymap e atualiza a interface instantaneamente.

Uso:
    python3 corne_layer_overlay.py [--keymap CAMINHO] [--always] [--scale N]

Dependências (Arch):
    sudo pacman -S python-gobject gtk4 gtk4-layer-shell
"""

import argparse
import glob
import gc
import itertools
import os
import re
import select
import sys
import threading
import time

def _ensure_layer_shell_preload():
    candidates = [
        "/usr/lib/libgtk4-layer-shell.so",
        "/usr/lib64/libgtk4-layer-shell.so",
        "/usr/local/lib/libgtk4-layer-shell.so",
    ]
    lib = next((c for c in candidates if os.path.exists(c)), None)
    if not lib:
        return
    current = os.environ.get("LD_PRELOAD", "")
    if "libgtk4-layer-shell" in current:
        return
    os.environ["LD_PRELOAD"] = (current + ":" + lib).lstrip(":")
    os.execvp(sys.executable, [sys.executable] + sys.argv)


if __name__ == "__main__" and os.environ.get("CORNE_OVERLAY_NO_PRELOAD") != "1":
    _ensure_layer_shell_preload()

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Gdk, Pango, PangoCairo  # noqa: E402

try:
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

    HAVE_LAYER_SHELL = True
except (ValueError, ImportError):
    HAVE_LAYER_SHELL = False


# ---------------------------------------------------------------------------
# Raw HID
# ---------------------------------------------------------------------------

_USAGE_PAGE_FF60 = b"\x06\x60\xff"
_USAGE_61 = b"\x09\x61"
LAYER_PACKET_MARKER = 0xFF


def find_raw_hid_device():
    """Retorna /dev/hidrawN do zmk-raw-hid somente se acessível para leitura."""
    for dev in sorted(glob.glob("/dev/hidraw*")):
        name = os.path.basename(dev)
        desc_path = f"/sys/class/hidraw/{name}/device/report_descriptor"
        try:
            with open(desc_path, "rb") as f:
                desc = f.read()
        except OSError:
            continue
        if not (_USAGE_PAGE_FF60 in desc and _USAGE_61 in desc):
            continue
        if os.access(dev, os.R_OK):
            return dev
    return None


class HidReader(threading.Thread):
    """Lê pacotes de camada via Raw HID (USB). Silencioso quando indisponível."""

    def __init__(self, on_layer):
        super().__init__(daemon=True)
        self._on_layer = on_layer
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            path = find_raw_hid_device()
            if not path:
                time.sleep(3)
                continue
            try:
                fd = os.open(path, os.O_RDONLY)
            except OSError:
                time.sleep(3)
                continue
            try:
                while not self._stop.is_set():
                    data = os.read(fd, 64)
                    if not data:
                        break
                    if len(data) >= 10 and data[0] == LAYER_PACKET_MARKER:
                        mask = int.from_bytes(data[6:10], "little")
                        layer = mask.bit_length() - 1 if mask else 0
                        GLib.idle_add(self._on_layer, layer)
            except OSError:
                pass
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
            time.sleep(2)


class EvdevReader(threading.Thread):
    """Detecção de camada via evdev — funciona por BT e USB."""

    _ENTER = {}
    _EXIT  = {}
    _AVAILABLE = False

    def __init__(self, on_layer):
        super().__init__(daemon=True)
        self._on_layer = on_layer
        self._stop = threading.Event()
        self._active = set()

    def stop(self):
        self._stop.set()

    @classmethod
    def _try_import(cls):
        if cls._AVAILABLE:
            return True
        try:
            import evdev
            from evdev import ecodes
            cls._evdev = evdev
            cls._ecodes = ecodes
            cls._ENTER = {ecodes.KEY_F13: 1, ecodes.KEY_F15: 2}
            cls._EXIT  = {ecodes.KEY_F14: 1, ecodes.KEY_F16: 2}
            cls._AVAILABLE = True
            return True
        except ImportError:
            return False

    def _find(self):
        ev = self._evdev
        ec = self._ecodes
        for path in ev.list_devices():
            try:
                d = ev.InputDevice(path)
                if d.info.vendor == 0x1D50 and d.info.product == 0x615E:
                    caps = d.capabilities().get(ec.EV_KEY, [])
                    if ec.KEY_F13 in caps and ec.KEY_A in caps:
                        return d
            except OSError:
                pass
        return None

    def _layer(self):
        if {1, 2} <= self._active:
            return 3
        return max(self._active, default=0)

    def run(self):
        if not self._try_import():
            return

        ec = self._ecodes
        while not self._stop.is_set():
            dev = self._find()
            if not dev:
                time.sleep(3)
                continue
            try:
                while not self._stop.is_set():
                    r, _, _ = select.select([dev.fd], [], [], 1.0)
                    if not r:
                        continue
                    for ev in dev.read():
                        if ev.type != ec.EV_KEY or ev.value != 1:
                            continue
                        changed = False
                        if ev.code in self._ENTER:
                            self._active.add(self._ENTER[ev.code])
                            changed = True
                        elif ev.code in self._EXIT:
                            self._active.discard(self._EXIT[ev.code])
                            changed = True
                        if changed:
                            GLib.idle_add(self._on_layer, self._layer())
            except OSError:
                self._active.clear()
            time.sleep(2)


class KeymapFileWatcher(threading.Thread):
    """Monitora o arquivo corne.keymap no disco e recarrega o overlay se ele for editado."""

    def __init__(self, keymap_path, on_layers):
        super().__init__(daemon=True)
        self.keymap_path = keymap_path
        self.on_layers = on_layers
        self._stop = threading.Event()
        self._last_mtime = 0

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                if os.path.exists(self.keymap_path):
                    mtime = os.path.getmtime(self.keymap_path)
                    if self._last_mtime > 0 and mtime != self._last_mtime:
                        self._last_mtime = mtime
                        layers = parse_keymap(self.keymap_path)
                        if layers:
                            GLib.idle_add(self.on_layers, layers)
                    elif self._last_mtime == 0:
                        self._last_mtime = mtime
            except OSError:
                pass
            time.sleep(1.0)


class StudioReader(threading.Thread):
    """Lê o keymap ao vivo do dispositivo via ZMK Studio RPC (USB, /dev/ttyACMx)."""

    SOF   = 0xAB
    ESC_B = 0xAC
    EOF_B = 0xAD

    def __init__(self, on_layers):
        super().__init__(daemon=True)
        self._on_layers = on_layers
        self._stop = threading.Event()
        self._last_port_check = 0
        self._last_port_result = False

    def stop(self):
        self._stop.set()

    # ── protobuf mínimo ──────────────────────────────────────────────────────

    @staticmethod
    def _vi(n):
        r = bytearray()
        while n > 0x7F:
            r.append((n & 0x7F) | 0x80); n >>= 7
        r.append(n)
        return bytes(r)

    @staticmethod
    def _dvi(d, p=0):
        r = s = 0
        while True:
            b = d[p]; p += 1; r |= (b & 0x7F) << s
            if not (b & 0x80): break
            s += 7
        return r, p

    @classmethod
    def _pf(cls, data):
        pos, out = 0, {}
        while pos < len(data):
            try:
                tag, pos = cls._dvi(data, pos)
                fn, wt = tag >> 3, tag & 7
                if   wt == 0: v, pos = cls._dvi(data, pos)
                elif wt == 2: n, pos = cls._dvi(data, pos); v = bytes(data[pos:pos+n]); pos += n
                elif wt == 5: v = bytes(data[pos:pos+4]); pos += 4
                elif wt == 1: v = bytes(data[pos:pos+8]); pos += 8
                else: break
                out.setdefault(fn, []).append(v)
            except Exception: break
        return out

    def _iv(self, v, d=0):
        if v is None: return d
        if isinstance(v, int): return v
        return self._dvi(v)[0] if isinstance(v, (bytes, bytearray)) and v else d

    def _sb(self, v):
        return v.decode('utf-8', 'replace') if isinstance(v, (bytes, bytearray)) else ''

    @staticmethod
    def _s32(n): return (n >> 1) ^ -(n & 1)

    def _vi_list(self, vals):
        r = []
        for v in vals:
            if isinstance(v, (bytes, bytearray)):
                p = 0
                while p < len(v): n, p = self._dvi(v, p); r.append(n)
            elif isinstance(v, int):
                r.append(v)
        return r

    def _fv(self, fn, n): return self._vi(fn << 3) + self._vi(n)
    def _fb(self, fn, b): return self._vi((fn << 3) | 2) + self._vi(len(b)) + b

    # ── framing ──────────────────────────────────────────────────────────────

    def _pack(self, payload):
        r = bytearray([self.SOF])
        for b in payload:
            if b in (self.SOF, self.ESC_B, self.EOF_B): r.append(self.ESC_B)
            r.append(b)
        r.append(self.EOF_B)
        return bytes(r)

    def _read_frame(self, fd, deadline=None):
        buf = bytearray(); started = esc = False
        while not self._stop.is_set():
            if deadline is not None and time.monotonic() > deadline:
                return None
            t = 0.5 if deadline is None else min(0.5, max(0.0, deadline - time.monotonic()))
            try:
                r, _, _ = select.select([fd], [], [], t)
            except OSError:
                return None
            if not r: return None
            try:
                raw = os.read(fd, 64)
            except OSError:
                return None
            if not raw: return None
            for b in raw:
                if esc:
                    buf.append(b); esc = False; continue
                if b == self.SOF:
                    buf.clear(); started = True
                elif started:
                    if b == self.EOF_B:
                        if buf: return bytes(buf)
                        started = False
                    elif b == self.ESC_B:
                        esc = True
                    else:
                        buf.append(b)
        return None

    # ── serial port ───────────────────────────────────────────────────────────

    @staticmethod
    def _open_tty(path):
        import fcntl, termios
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        fcntl.fcntl(fd, fcntl.F_SETFL,
                    fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
        a = termios.tcgetattr(fd)
        a[0] = a[1] = a[3] = 0
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        a[6][termios.VMIN] = 1; a[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, a)
        return fd

    @staticmethod
    def _find_tty():
        for p in sorted(glob.glob('/dev/ttyACM*')):
            if os.access(p, os.R_OK | os.W_OK): return p
        return None

    def _port_in_use(self, path):
        now = time.monotonic()
        if now - self._last_port_check < 5.0:
            return self._last_port_result

        self._last_port_check = now
        try:
            target_dev = os.stat(path).st_rdev
            my_pid = str(os.getpid())
            for pid_str in os.listdir('/proc'):
                if not pid_str.isdigit() or pid_str == my_pid:
                    continue
                fd_dir = f'/proc/{pid_str}/fd'
                try:
                    for fd_name in os.listdir(fd_dir):
                        try:
                            if os.stat(f'{fd_dir}/{fd_name}').st_rdev == target_dev:
                                self._last_port_result = True
                                return True
                        except OSError:
                            pass
                except (PermissionError, OSError):
                    pass
        except OSError:
            pass
        self._last_port_result = False
        return False

    # ── RPC ───────────────────────────────────────────────────────────────────

    def _rpc(self, fd, payload, req_id, timeout=3.0):
        try:
            os.write(fd, self._pack(payload))
        except OSError:
            return None, []
        notifs = []; deadline = time.monotonic() + timeout
        while not self._stop.is_set():
            raw = self._read_frame(fd, deadline=deadline)
            if raw is None: return None, notifs
            outer = self._pf(raw)
            if 2 in outer: notifs.extend(outer[2]); continue
            if 1 in outer:
                rr = self._pf(outer[1][0])
                if self._iv(rr.get(1, [None])[0]) == req_id:
                    return rr, notifs
        return None, notifs

    # ── requests ──────────────────────────────────────────────────────────────

    def _get_lock_state(self, fd, rid):
        p = self._fv(1, rid) + self._fb(3, self._fv(2, 1))
        rr, _ = self._rpc(fd, p, rid)
        if rr is None: return None
        cr = self._pf(rr.get(3, [b''])[0])
        return self._iv(cr.get(2, [0])[0]) == 1

    def _list_behaviors(self, fd, rid):
        p = self._fv(1, rid) + self._fb(4, self._fv(1, 1))
        rr, _ = self._rpc(fd, p, rid)
        if rr is None: return []
        br  = self._pf(rr.get(4, [b''])[0])
        lab = self._pf(br.get(1, [b''])[0])
        return self._vi_list(lab.get(1, []))

    def _get_beh_name(self, fd, rid, bid):
        p = self._fv(1, rid) + self._fb(4, self._fb(2, self._fv(1, bid)))
        rr, _ = self._rpc(fd, p, rid)
        if rr is None: return ''
        br = self._pf(rr.get(4, [b''])[0])
        dr = self._pf(br.get(2, [b''])[0])
        return self._sb(dr.get(2, [b''])[0])

    def _get_keymap_raw(self, fd, rid):
        p = self._fv(1, rid) + self._fb(5, self._fv(1, 1))
        rr, _ = self._rpc(fd, p, rid)
        if rr is None: return None
        kr = self._pf(rr.get(5, [b''])[0])
        km = kr.get(1, [None])[0]
        return self._pf(km) if km else None

    # ── label HID ─────────────────────────────────────────────────────────────

    _KB = {
        4:'A',5:'B',6:'C',7:'D',8:'E',9:'F',10:'G',11:'H',12:'I',13:'J',
        14:'K',15:'L',16:'M',17:'N',18:'O',19:'P',20:'Q',21:'R',22:'S',
        23:'T',24:'U',25:'V',26:'W',27:'X',28:'Y',29:'Z',
        30:'1',31:'2',32:'3',33:'4',34:'5',35:'6',36:'7',37:'8',38:'9',39:'0',
        40:'⏎',41:'⎋',42:'⌫',43:'⇥',44:'␣',45:'-',46:'=',47:'[',48:']',
        49:'\\',51:';',52:"'",53:'`',54:',',55:'.',56:'/', 70:'BT⌫', 84:'/',
        58:'F1',59:'F2',60:'F3',61:'F4',62:'F5',63:'F6',
        64:'F7',65:'F8',66:'F9',67:'F10',68:'F11',69:'F12',
        70:'PrSc', 74:'Home',75:'PgUp',76:'⌦',77:'End',78:'PgDn',
        79:'→',80:'←',81:'↓',82:'↑',
        94:'BT0',95:'BT1',96:'BT2',97:'BT3',98:'BT4',
        104:'F13',105:'F14',106:'F15',107:'F16',
        127:'Mute',128:'Vol+',129:'Vol-',
        224:'Ctrl',225:'⇧',226:'Alt',227:'Gui',
        228:'Ctrl',229:'⇧',230:'AltG',231:'Gui',
    }
    _CS = {
        0x6F:'Bri+', 0x70:'Bri-', 0x79:'Next', 0x7A:'Prev',
        0xB5:'Next', 0xB6:'Prev', 0xCD:'Play', 0xE2:'Mute', 0xE9:'Vol+', 0xEA:'Vol-'
    }

    def _hid_label(self, param):
        page  = (param >> 16) & 0xFF
        usage = param & 0xFFFF
        if page == 0: page = 7
        if page == 7:  return self._KB.get(usage, f'?{usage:X}')
        if page == 0xC: return self._CS.get(usage, f'C{usage:X}')
        return f'{param:X}'

    # ── binding label ──────────────────────────────────────────────────────────

    def _beh_label(self, bid, p1, p2, beh_map):
        n = beh_map.get(bid, '').lower()
        if not n: return '?'
        if 'transparent' in n or n == 'trans': return ''
        if n in ('none', '&none'): return '✗'
        if 'key press' in n:        return self._hid_label(p1)
        if 'momentary layer' in n:  return f'L{p1}'
        if 'layer tap' in n:
            label2 = self._hid_label(p2)
            return f'L{p1}/{label2}' if label2 else f'L{p1}'
        if 'hold-tap' in n or 'mod tap' in n or 'mod-tap' in n:
            return f'{self._hid_label(p1)}/{self._hid_label(p2)}'
        if 'toggle layer' in n:     return f'⇉{p1}'
        if 'sticky layer' in n or 'sticky key' in n:
            return f'sk/{self._hid_label(p1)}' if p1 else 'sk'
        if 'studio unlock' in n:    return '🔓'
        if 'lower' in n:            return 'L1'
        if 'raise' in n:            return 'L2'
        if 'rgb' in n or 'underglow' in n: return 'RGB'
        if 'bluetooth' in n:        return 'BT'
        if 'bootloader' in n:       return 'Boot'
        if 'reset' in n:            return 'Rst'
        if 'up' in n and ('mmv' in n or 'mouse' in n or 'move' in n):    return '🖱↑'
        if 'down' in n and ('mmv' in n or 'mouse' in n or 'move' in n):  return '🖱↓'
        if 'left' in n and ('mmv' in n or 'mouse' in n or 'move' in n):  return '🖱←'
        if 'right' in n and ('mmv' in n or 'mouse' in n or 'move' in n): return '🖱→'
        if 'mouse button' in n:     return '🖰'
        if 'mouse move' in n or 'scroll' in n: return '🖱'
        words = beh_map.get(bid, '').split()
        return words[0][:5] if words else '?'

    # ── decodifica keymap ──────────────────────────────────────────────────────

    def _build_layers(self, km_fields, beh_map):
        layers = []
        for lb in km_fields.get(1, []):
            lf  = self._pf(lb)
            lid = self._iv(lf.get(1, [0])[0])
            name = self._sb(lf.get(2, [b''])[0]) or f'layer{lid}'
            labels = []
            for bb in lf.get(3, []):
                bf  = self._pf(bb)
                bid = self._s32(self._iv(bf.get(1, [0])[0]))
                p1  = self._iv(bf.get(2, [0])[0])
                p2  = self._iv(bf.get(3, [0])[0])
                labels.append(self._beh_label(bid, p1, p2, beh_map))
            layers.append((name, labels))
        return layers or None

    def _load(self, fd, seq):
        if not self._get_lock_state(fd, next(seq)):
            return None
        bids = self._list_behaviors(fd, next(seq))
        beh_map = {bid: self._get_beh_name(fd, next(seq), bid) for bid in bids}
        km = self._get_keymap_raw(fd, next(seq))
        return self._build_layers(km, beh_map) if km else None

    def _is_saved(self, nb):
        kn = self._pf(nb).get(5)
        if not kn: return False
        sc = self._pf(kn[0]).get(1)
        return sc is not None and self._iv(sc[0]) == 0

    def _is_unlocked(self, nb):
        cn = self._pf(nb).get(2)
        if not cn: return False
        ls = self._pf(cn[0]).get(1)
        return ls is not None and self._iv(ls[0]) == 1

    def _sleep(self, seconds):
        steps = max(1, int(seconds / 0.2))
        for _ in range(steps):
            if self._stop.is_set(): return
            time.sleep(0.2)

    def run(self):
        seq = itertools.count(1)
        last_hash = None

        while not self._stop.is_set():
            path = self._find_tty()
            if not path:
                self._sleep(5)
                continue

            if self._port_in_use(path):
                self._sleep(5)
                continue

            try:
                fd = self._open_tty(path)
            except OSError:
                self._sleep(5)
                continue

            try:
                loaded = False
                layers = self._load(fd, seq)
                if layers:
                    h = str(layers)
                    if h != last_hash:
                        last_hash = h
                        GLib.idle_add(self._on_layers, layers)
                    loaded = True

                while not self._stop.is_set():
                    raw = self._read_frame(fd, deadline=time.monotonic() + 2.0)
                    if raw is None:
                        if self._port_in_use(path):
                            break
                        self._sleep(2.0)
                        continue
                    outer = self._pf(raw)
                    for nb in outer.get(2, []):
                        should_reload = (self._is_saved(nb) or
                                         (not loaded and self._is_unlocked(nb)))
                        if should_reload:
                            layers = self._load(fd, seq)
                            if layers:
                                h = str(layers)
                                if h != last_hash:
                                    last_hash = h
                                    loaded = True
                                    GLib.idle_add(self._on_layers, layers)
            except OSError:
                pass
            finally:
                try: os.close(fd)
                except OSError: pass
            self._sleep(3)


# ---------------------------------------------------------------------------
# Parser do keymap
# ---------------------------------------------------------------------------

_KEYMAP_BLOCK_RE = re.compile(
    r'compatible\s*=\s*"zmk,keymap"\s*;(.*)', re.DOTALL
)
_LAYER_RE = re.compile(
    r"(\w+)\s*\{[^{}]*?bindings\s*=\s*<(.*?)>\s*;", re.DOTALL
)

KEY_LABELS = {
    "TAB": "⇥", "BSPC": "⌫", "RET": "⏎", "SPACE": "␣", "ESC": "⎋",
    "DEL": "⌦", "CAPS": "⇪", "ENTER": "⏎",
    "LCTRL": "Ctrl", "RCTRL": "Ctrl", "LSHFT": "⇧", "RSHFT": "⇧",
    "LGUI": "Gui", "RGUI": "Gui", "LALT": "Alt", "RALT": "Alt",
    "SQT": "'", "DQT": '"', "COMMA": ",", "DOT": ".", "FSLH": "/",
    "BSLH": "\\", "SEMI": ";", "COLON": ":", "GRAVE": "`", "TILDE": "~",
    "MINUS": "-", "EQUAL": "=", "UNDER": "_", "PLUS": "+",
    "LBKT": "[", "RBKT": "]", "LBRC": "{", "RBRC": "}",
    "LPAR": "(", "RPAR": ")", "PIPE": "|",
    "EXCL": "!", "AT": "@", "HASH": "#", "DLLR": "$", "PRCNT": "%",
    "CARET": "^", "AMPS": "&", "ASTRK": "*", "QMARK": "?",
    "LEFT": "←", "RIGHT": "→", "UP": "↑", "DOWN": "↓",
    "HOME": "Home", "END": "End", "PG_UP": "PgUp", "PG_DN": "PgDn",
    "C_VOL_UP": "Vol+", "C_VOL_DN": "Vol-", "C_MUTE": "Mute",
    "C_BRI_UP": "☀️+", "C_BRI_DN": "☀️-", "C_PP": "⏯",
    "PRINTSCREEN": "PrSc", "C_NEXT": "⏭", "C_PREV": "⏮",
}
for i in range(10):
    KEY_LABELS[f"N{i}"] = str(i)
for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    KEY_LABELS[c] = c

RGB_LABELS = {
    "RGB_TOG": "RGB⏻", "RGB_EFF": "RGB+", "RGB_EFR": "RGB-",
    "RGB_HUI": "Hue+", "RGB_HUD": "Hue-", "RGB_SAI": "Sat+", "RGB_SAD": "Sat-",
    "RGB_BRI": "RGB_Bri+", "RGB_BRD": "RGB_Bri-", "RGB_SPI": "Spd+", "RGB_SPD": "Spd-",
}
MOUSE_KEYS = {
    "LCLK": "🖰L", "RCLK": "🖰R", "MCLK": "🖰M",
    "MOVE_UP": "🖱↑", "MOVE_DOWN": "🖱↓", "MOVE_LEFT": "🖱←", "MOVE_RIGHT": "🖱→",
    "SCRL_UP": "⇕↑", "SCRL_DOWN": "⇕↓", "SCRL_LEFT": "⇕←", "SCRL_RIGHT": "⇕→",
    "mmv_td_up": "🖱↑", "mmv_td_down": "🖱↓", "mmv_td_left": "🖱←", "mmv_td_right": "🖱→",
}


def _kp_label(code):
    m = re.fullmatch(r"[LR][ACGS]\((.+)\)", code)
    if m:
        inner = m.group(1)
        if code == "RA(C)":
            return "Ç"
        return _kp_label(inner)
    return KEY_LABELS.get(code, code)


def binding_label(tokens):
    beh = tokens[0]
    args = tokens[1:]

    if beh in ("&trans",):
        return ""
    if beh in ("&none",):
        return "✗"
    if beh in ("&lower_mo", "lower_mo"):
        return "L1"
    if beh in ("&raise_mo", "raise_mo"):
        return "L2"
    if beh in ("&studio_unlock", "studio_unlock"):
        return "🔓"
    if beh == "&kp" and args:
        return _kp_label(args[0])
    if beh == "&mo" and args:
        return f"L{args[0]}"
    if beh in ("&to", "&tog") and args:
        return f"⇉{args[0]}"
    if beh == "&lt" and len(args) >= 2:
        return f"L{args[0]}/{_kp_label(args[1])}"
    if beh == "&mt" and len(args) >= 2:
        return f"{_kp_label(args[0])}/{_kp_label(args[1])}"
    if beh == "&bt":
        if args and args[0] == "BT_CLR":
            return "BT⌫"
        if len(args) >= 2 and args[0] == "BT_SEL":
            return f"BT{args[1]}"
        return "BT"
    if beh == "&rgb_ug" and args:
        return RGB_LABELS.get(args[0], args[0].replace("RGB_", ""))
    if beh == "&mkp" and args:
        return MOUSE_KEYS.get(args[0], args[0])
    if beh == "&mmv" and args:
        return MOUSE_KEYS.get(args[0], args[0])
    if beh == "&msc" and args:
        return MOUSE_KEYS.get(args[0], args[0])
    if beh in ("&mmv_td_up", "&mmv_td_down", "&mmv_td_left", "&mmv_td_right", "mmv_td_up", "mmv_td_down", "mmv_td_left", "mmv_td_right"):
        return MOUSE_KEYS.get(beh.lstrip("&"), beh)
    label = beh.lstrip("&")
    if args:
        label += " " + " ".join(args)
    return label


def parse_keymap(path):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)

    m = _KEYMAP_BLOCK_RE.search(text)
    keymap_text = m.group(1) if m else text

    layers = []
    for name, body in _LAYER_RE.findall(keymap_text):
        tokens = body.split()
        bindings = []
        cur = None
        for tok in tokens:
            if tok.startswith("&"):
                if cur:
                    bindings.append(cur)
                cur = [tok]
            elif cur is not None:
                cur.append(tok)
        if cur:
            bindings.append(cur)
        labels = [binding_label(b) for b in bindings]
        friendly = name[:-6] if name.endswith("_layer") else name
        layers.append((friendly, labels))
    return layers


# ---------------------------------------------------------------------------
# Desenho da miniatura
# ---------------------------------------------------------------------------

LAYER_COLORS = [
    (0.36, 0.42, 0.55),
    (0.20, 0.55, 0.45),
    (0.62, 0.38, 0.25),
    (0.50, 0.32, 0.60),
]


class CornePainter:
    """Geometria + desenho Cairo otimizado."""

    def __init__(self, scale=1.0):
        s = scale
        self.kw = 40 * s
        self.kh = 32 * s
        self.gap = 4 * s
        self.half_gap = 26 * s
        self.margin = 12 * s
        self.header = 30 * s
        self.font = 15 * s
        self.col = self.kw + self.gap
        self.left_x = self.margin
        self.right_x = self.left_x + 6 * self.col + self.half_gap
        self.row_y0 = self.margin + self.header
        self.thumb_y = self.row_y0 + 3 * (self.kh + self.gap) + 8 * s

        self.font_desc = Pango.FontDescription()
        self.font_desc.set_family("sans-serif")
        self.font_desc.set_absolute_size(self.font * Pango.SCALE)

        self.bold_font_desc = Pango.FontDescription()
        self.bold_font_desc.set_family("sans-serif")
        self.bold_font_desc.set_absolute_size((self.font + 3) * Pango.SCALE)
        self.bold_font_desc.set_weight(Pango.Weight.BOLD)

    @property
    def width(self):
        return self.right_x + 6 * self.col - self.gap + self.margin

    @property
    def height(self):
        return self.thumb_y + self.kh + self.margin

    def _key_rect(self, index):
        if index < 36:
            row, col = divmod(index, 12)
            if col < 6:
                x = self.left_x + col * self.col
            else:
                x = self.right_x + (col - 6) * self.col
            y = self.row_y0 + row * (self.kh + self.gap)
            return x, y
        t = index - 36
        if t < 3:
            x = self.left_x + (3 + t) * self.col
        else:
            x = self.right_x + (t - 3) * self.col
        return x, self.thumb_y

    def draw(self, cr, width, height, layer_idx, layer_name, labels):
        color = LAYER_COLORS[layer_idx % len(LAYER_COLORS)]

        self._round_rect(cr, 0, 0, width, height, 14)
        cr.set_source_rgba(0.10, 0.11, 0.13, 0.93)
        cr.fill_preserve()
        cr.set_source_rgba(*color, 0.9)
        cr.set_line_width(2)
        cr.stroke()

        self._text(cr, self.margin, self.margin - 2 + self.header / 2,
                   f"L{layer_idx}  {layer_name}", bold=True, valign="center")

        for i in range(42):
            x, y = self._key_rect(i)
            label = labels[i] if i < len(labels) else ""
            transp = (label == "")
            self._round_rect(cr, x, y, self.kw, self.kh, 6)
            if transp:
                cr.set_source_rgba(*color, 0.10)
            else:
                cr.set_source_rgba(*color, 0.30)
            cr.fill_preserve()
            cr.set_source_rgba(1, 1, 1, 0.10)
            cr.set_line_width(1)
            cr.stroke()
            if label:
                self._fit_text(cr, x, y, self.kw, self.kh, label)

    @staticmethod
    def _round_rect(cr, x, y, w, h, r):
        import math
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
        cr.close_path()

    def _text(self, cr, x, y, text, bold=False, valign="top"):
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(self.bold_font_desc if bold else self.font_desc)
        layout.set_text(text, -1)
        _, ext = layout.get_pixel_extents()
        ty = y - ext.height / 2 if valign == "center" else y
        cr.set_source_rgb(1.0, 1.0, 1.0)
        cr.move_to(x, ty)
        PangoCairo.show_layout(cr, layout)

    def _fit_text(self, cr, x, y, w, h, text):
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(self.font_desc)
        layout.set_text(text, -1)
        _, ext = layout.get_pixel_extents()
        cr.set_source_rgba(0.95, 0.96, 0.98, 1.0)
        cr.move_to(x + (w - ext.width) / 2, y + (h - ext.height) / 2)
        PangoCairo.show_layout(cr, layout)


# ---------------------------------------------------------------------------
# Janela / overlay
# ---------------------------------------------------------------------------

class OverlayWindow(Gtk.ApplicationWindow):
    def __init__(self, app, layers, scale, always):
        super().__init__(application=app)
        self.layers = layers
        self.always = always
        self.current_layer = 0
        self._hide_source = None

        self.painter = CornePainter(scale)
        self.set_title("Corne Layer Overlay")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_default_size(int(self.painter.width), int(self.painter.height))

        self.area = Gtk.DrawingArea()
        self.area.set_content_width(int(self.painter.width))
        self.area.set_content_height(int(self.painter.height))
        self.area.set_draw_func(self._on_draw)
        self.set_child(self.area)

        self.add_css_class("transparent")
        css = Gtk.CssProvider()
        css.load_from_data(b"window.transparent { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        if HAVE_LAYER_SHELL:
            self._setup_layer_shell()

    def _setup_layer_shell(self):
        LayerShell.init_for_window(self)
        LayerShell.set_layer(self, LayerShell.Layer.OVERLAY)
        LayerShell.set_anchor(self, LayerShell.Edge.TOP, True)
        LayerShell.set_anchor(self, LayerShell.Edge.RIGHT, True)
        LayerShell.set_margin(self, LayerShell.Edge.TOP, 12)
        LayerShell.set_margin(self, LayerShell.Edge.RIGHT, 12)
        LayerShell.set_keyboard_mode(self, LayerShell.KeyboardMode.NONE)
        LayerShell.set_namespace(self, "corne-layer-overlay")

    def _on_draw(self, area, cr, width, height):
        name, labels = self.layers[self.current_layer] if \
            self.current_layer < len(self.layers) else ("?", [])
        self.painter.draw(cr, width, height, self.current_layer, name, labels)

    def on_layer(self, layer):
        if self._hide_source is not None:
            GLib.source_remove(self._hide_source)
            self._hide_source = None

        target_visible = self.always or layer != 0
        if self.current_layer == layer and self.get_visible() == target_visible:
            return False

        self.current_layer = layer
        if not self.always and layer == 0:
            self.set_visible(False)
            gc.collect()
            return False

        self.set_visible(True)
        self.area.queue_draw()

        if not self.always:
            def _hide():
                self.set_visible(False)
                self._hide_source = None
                gc.collect()
                return False
            self._hide_source = GLib.timeout_add(1000, _hide)

        return False

    def on_layers(self, layers):
        self.layers = layers
        self.area.queue_draw()
        return False


class OverlayApp(Gtk.Application):
    def __init__(self, keymap_path, layers, scale, always):
        super().__init__(application_id="dev.corne.layeroverlay", flags=0)
        self.keymap_path = keymap_path
        self.layers = layers
        self.scale = scale
        self.always = always
        self.win = None

    def do_activate(self):
        if self.win is None:
            self.win = OverlayWindow(self, self.layers, self.scale, self.always)
            self.reader = HidReader(self.win.on_layer)
            self.evdev_reader = EvdevReader(self.win.on_layer)
            self.file_watcher = KeymapFileWatcher(self.keymap_path, self.win.on_layers)
            self.studio_reader = StudioReader(self.win.on_layers)
            self.reader.start()
            self.evdev_reader.start()
            self.file_watcher.start()
            self.studio_reader.start()
        if self.always:
            self.win.present()
        else:
            self.win.set_visible(False)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    default_keymap = os.path.normpath(os.path.join(here, "..", "config", "corne.keymap"))

    ap = argparse.ArgumentParser(description="Overlay de camada do Corne")
    ap.add_argument("--keymap", default=default_keymap,
                    help="caminho do .keymap (padrão: ../config/corne.keymap)")
    ap.add_argument("--always", action="store_true",
                    help="mantém o overlay sempre visível (não some na camada base)")
    ap.add_argument("--scale", type=float, default=0.7,
                    help="escala da miniatura (padrão 0.7)")
    args = ap.parse_args()

    if not os.path.exists(args.keymap):
        sys.exit(f"keymap não encontrado: {args.keymap}")

    layers = parse_keymap(args.keymap)
    if not layers:
        sys.exit("nenhuma camada encontrada no keymap")

    print(f"[corne-overlay] {len(layers)} camadas: "
          + ", ".join(f"{i}:{n}" for i, (n, _) in enumerate(layers)))
    if not HAVE_LAYER_SHELL:
        print("[corne-overlay] aviso: gtk4-layer-shell ausente — usando janela "
              "normal (pode não ficar ancorada/always-on-top no niri).")

    app = OverlayApp(args.keymap, layers, args.scale, args.always)
    app.run(None)


if __name__ == "__main__":
    main()
