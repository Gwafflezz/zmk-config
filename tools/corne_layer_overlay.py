#!/usr/bin/env python3
"""
Corne layer overlay
===================

Desenha uma miniatura do *seu* Corne no canto superior direito da tela e troca
a camada exibida ao vivo, sempre que ela é ativada no teclado.

Como funciona (híbrido USB + BT):
  - USB: o firmware (zmk-keypeek-layer-notifier + zmk-raw-hid) envia um report
    Raw HID de 32 bytes com o bitmask das camadas ativas via /dev/hidrawX.
  - BT: os macros `lower_mo`/`raise_mo` no keymap emitem F13/F14 (lower) e
    F15/F16 (raise) ao entrar/sair de cada camada. O overlay escuta via evdev
    (/dev/input/eventX) — funciona igual por USB e Bluetooth.
  - Ambos os caminhos rodam simultaneamente; qualquer um que conectar dispara
    a atualização da camada.
  - O keymap (labels das teclas) é lido localmente do corne.keymap.

Uso:
    python3 corne_layer_overlay.py [--keymap CAMINHO] [--always] [--scale N]

Dependências (Arch):
    sudo pacman -S python-gobject gtk4 gtk4-layer-shell
"""

import argparse
import glob
import os
import re
import select
import sys
import threading
import time

# gtk4-layer-shell precisa ser carregado ANTES do libwayland-client, senão o
# ancoramento do overlay falha (vira janela normal). Em Python isso só é possível
# via LD_PRELOAD. Aqui detectamos a lib e nos re-executamos com o preload setado,
# de forma transparente — assim o usuário roda o script normalmente.
def _ensure_layer_shell_preload():
    candidates = [
        "/usr/lib/libgtk4-layer-shell.so",
        "/usr/lib64/libgtk4-layer-shell.so",
        "/usr/local/lib/libgtk4-layer-shell.so",
    ]
    lib = next((c for c in candidates if os.path.exists(c)), None)
    if not lib:
        return  # sem layer-shell: o app cai no fallback de janela normal
    current = os.environ.get("LD_PRELOAD", "")
    if "libgtk4-layer-shell" in current:
        return  # já preloaded (provavelmente já re-executamos)
    os.environ["LD_PRELOAD"] = (current + ":" + lib).lstrip(":")
    os.execvp(sys.executable, [sys.executable] + sys.argv)


# Só re-executa quando rodado como script (não ao ser importado em testes).
if __name__ == "__main__" and os.environ.get("CORNE_OVERLAY_NO_PRELOAD") != "1":
    _ensure_layer_shell_preload()

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Gdk, Pango, PangoCairo  # noqa: E402

# gtk4-layer-shell é o jeito certo de fazer um overlay always-on-top, ancorado
# e sem roubar foco no Wayland (niri, sway, Hyprland...). Se faltar, caímos para
# uma janela normal.
try:
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

    HAVE_LAYER_SHELL = True
except (ValueError, ImportError):
    HAVE_LAYER_SHELL = False


# ---------------------------------------------------------------------------
# Raw HID
# ---------------------------------------------------------------------------

# zmk-raw-hid usa Usage Page 0xFF60, Usage 0x61 (padrão "raw HID" estilo QMK).
# No report descriptor isso aparece como os bytes abaixo.
_USAGE_PAGE_FF60 = b"\x06\x60\xff"
_USAGE_61 = b"\x09\x61"
LAYER_PACKET_MARKER = 0xFF


def find_raw_hid_device():
    """Procura o /dev/hidrawN cujo report descriptor é o do zmk-raw-hid."""
    for dev in sorted(glob.glob("/dev/hidraw*")):
        name = os.path.basename(dev)
        desc_path = f"/sys/class/hidraw/{name}/device/report_descriptor"
        try:
            with open(desc_path, "rb") as f:
                desc = f.read()
        except OSError:
            continue
        if _USAGE_PAGE_FF60 in desc and _USAGE_61 in desc:
            return dev
    return None


class HidReader(threading.Thread):
    """Lê os pacotes de camada do teclado e chama on_layer/on_status."""

    def __init__(self, on_layer, on_status):
        super().__init__(daemon=True)
        self._on_layer = on_layer
        self._on_status = on_status
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def _emit_status(self, msg):
        GLib.idle_add(self._on_status, msg)

    def _emit_layer(self, layer):
        GLib.idle_add(self._on_layer, layer)

    def run(self):
        while not self._stop.is_set():
            path = find_raw_hid_device()
            if not path:
                self._emit_status("Teclado não encontrado (conecte/ligue o Corne)")
                time.sleep(2)
                continue
            try:
                fd = os.open(path, os.O_RDONLY)
            except PermissionError:
                self._emit_status(f"Sem permissão em {path} — veja 99-zmk-raw-hid.rules")
                time.sleep(3)
                continue
            except OSError:
                time.sleep(2)
                continue

            self._emit_status(None)  # conectado
            try:
                while not self._stop.is_set():
                    data = os.read(fd, 64)
                    if not data:
                        break
                    if len(data) >= 10 and data[0] == LAYER_PACKET_MARKER:
                        mask = int.from_bytes(data[6:10], "little")
                        layer = mask.bit_length() - 1 if mask else 0
                        self._emit_layer(layer)
            except OSError:
                pass
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
            time.sleep(1)  # device sumiu, tenta de novo


class EvdevReader(threading.Thread):
    """Detecção de camada via evdev — funciona por BT e USB.

    Os macros lower_mo/raise_mo no keymap emitem:
      F13 → entrou na lower (layer 1)   F14 → saiu da lower
      F15 → entrou na raise (layer 2)   F16 → saiu da raise
    O tri-layer (mouse, layer 3) é inferido quando ambas estão ativas.

    Silencioso: não envia status, apenas on_layer. Roda em paralelo com
    HidReader; qualquer um que disparar primeiro atualiza o overlay.
    """

    _ENTER = {}   # preenchido após tentar importar evdev
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
            return  # python-evdev não instalado; caminho BT indisponível

        ec = self._ecodes
        while not self._stop.is_set():
            dev = self._find()
            if not dev:
                time.sleep(2)
                continue
            try:
                while not self._stop.is_set():
                    r, _, _ = select.select([dev.fd], [], [], 0.5)
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
            time.sleep(1)


# ---------------------------------------------------------------------------
# Parser do keymap
# ---------------------------------------------------------------------------

_LAYER_RE = re.compile(
    r"(\w+)\s*\{[^{}]*?bindings\s*=\s*<(.*?)>\s*;", re.DOTALL
)

# Códigos de tecla -> rótulo curto exibido na miniatura.
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
}
for i in range(10):
    KEY_LABELS[f"N{i}"] = str(i)
for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    KEY_LABELS[c] = c

# rgb_ug / outras behaviors com argumento "verboso"
RGB_LABELS = {
    "RGB_TOG": "RGB⏻", "RGB_EFF": "RGB+", "RGB_EFR": "RGB-",
    "RGB_HUI": "Hue+", "RGB_HUD": "Hue-", "RGB_SAI": "Sat+", "RGB_SAD": "Sat-",
    "RGB_BRI": "Bri+", "RGB_BRD": "Bri-", "RGB_SPI": "Spd+", "RGB_SPD": "Spd-",
}
MOUSE_KEYS = {
    "LCLK": "🖰L", "RCLK": "🖰R", "MCLK": "🖰M",
    "MOVE_UP": "🖱↑", "MOVE_DOWN": "🖱↓", "MOVE_LEFT": "🖱←", "MOVE_RIGHT": "🖱→",
    "SCRL_UP": "⇕↑", "SCRL_DOWN": "⇕↓", "SCRL_LEFT": "⇕←", "SCRL_RIGHT": "⇕→",
}


def _kp_label(code):
    """&kp <code> -> rótulo. Trata modificadores tipo RA(C), LS(N1)..."""
    m = re.fullmatch(r"[LR][ACGS]\((.+)\)", code)
    if m:
        inner = m.group(1)
        # caso especial do teu keymap: RA(C) gera Ç
        if code == "RA(C)":
            return "Ç"
        return _kp_label(inner)
    return KEY_LABELS.get(code, code)


def binding_label(tokens):
    """Converte uma binding (lista de tokens) num rótulo curto."""
    beh = tokens[0]
    args = tokens[1:]

    if beh in ("&trans",):
        return ""          # transparente
    if beh in ("&none",):
        return "✗"
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
    # fallback: nome da behavior sem & + args
    label = beh.lstrip("&")
    if args:
        label += " " + " ".join(args)
    return label


def parse_keymap(path):
    """Retorna lista de (nome_da_camada, [rótulos...]) na ordem do arquivo."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    # remove comentários (inclusive os diagramas ASCII com '|')
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)

    layers = []
    for name, body in _LAYER_RE.findall(text):
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
        # nome amigável: default_layer -> "default"
        friendly = name[:-6] if name.endswith("_layer") else name
        layers.append((friendly, labels))
    return layers


# ---------------------------------------------------------------------------
# Desenho da miniatura
# ---------------------------------------------------------------------------

# Cor de destaque por camada (cicla se houver mais).
LAYER_COLORS = [
    (0.36, 0.42, 0.55),   # 0 default - azul acinzentado
    (0.20, 0.55, 0.45),   # 1 lower   - verde
    (0.62, 0.38, 0.25),   # 2 raise   - laranja
    (0.50, 0.32, 0.60),   # 3 mouse   - roxo
]


class CornePainter:
    """Geometria + desenho Cairo de um Corne 3x6 + 3 thumbs por lado."""

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

    @property
    def width(self):
        return self.right_x + 6 * self.col - self.gap + self.margin

    @property
    def height(self):
        return self.thumb_y + self.kh + self.margin

    def _key_rect(self, index):
        """Posição (x, y) de cada uma das 42 teclas pelo índice da binding."""
        if index < 36:
            row, col = divmod(index, 12)
            if col < 6:
                x = self.left_x + col * self.col
            else:
                x = self.right_x + (col - 6) * self.col
            y = self.row_y0 + row * (self.kh + self.gap)
            return x, y
        # thumbs: 36,37,38 (esq) e 39,40,41 (dir)
        t = index - 36
        if t < 3:
            x = self.left_x + (3 + t) * self.col
        else:
            x = self.right_x + (t - 3) * self.col
        return x, self.thumb_y

    def draw(self, cr, width, height, layer_idx, layer_name, labels):
        color = LAYER_COLORS[layer_idx % len(LAYER_COLORS)]

        # fundo arredondado
        self._round_rect(cr, 0, 0, width, height, 14)
        cr.set_source_rgba(0.10, 0.11, 0.13, 0.93)
        cr.fill_preserve()
        cr.set_source_rgba(*color, 0.9)
        cr.set_line_width(2)
        cr.stroke()

        # cabeçalho
        self._text(cr, self.margin, self.margin - 2 + self.header / 2,
                   f"L{layer_idx}  {layer_name}", self.font + 3,
                   (1, 1, 1), bold=True, valign="center")

        # teclas
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

    # -- helpers de desenho --
    @staticmethod
    def _round_rect(cr, x, y, w, h, r):
        import math
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
        cr.close_path()

    def _text(self, cr, x, y, text, size, rgb, bold=False, valign="top"):
        layout = PangoCairo.create_layout(cr)
        desc = Pango.FontDescription()
        desc.set_family("sans-serif")
        desc.set_absolute_size(size * Pango.SCALE)
        if bold:
            desc.set_weight(Pango.Weight.BOLD)
        layout.set_font_description(desc)
        layout.set_text(text, -1)
        _, ext = layout.get_pixel_extents()
        ty = y - ext.height / 2 if valign == "center" else y
        cr.set_source_rgb(*rgb)
        cr.move_to(x, ty)
        PangoCairo.show_layout(cr, layout)

    def _fit_text(self, cr, x, y, w, h, text):
        """Centraliza o rótulo na tecla, encolhendo se não couber."""
        size = self.font
        layout = PangoCairo.create_layout(cr)
        desc = Pango.FontDescription()
        desc.set_family("sans-serif")
        while size > 6:
            desc.set_absolute_size(size * Pango.SCALE)
            layout.set_font_description(desc)
            layout.set_text(text, -1)
            _, ext = layout.get_pixel_extents()
            if ext.width <= w - 4 and ext.height <= h - 2:
                break
            size -= 1
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
        self.status_msg = None

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

        # transparência
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
        if self.status_msg is not None:
            self.painter._round_rect(cr, 0, 0, width, height, 14)
            cr.set_source_rgba(0.10, 0.11, 0.13, 0.93)
            cr.fill()
            self.painter._text(cr, 14, height / 2, self.status_msg,
                               12, (1, 0.7, 0.4), valign="center")
            return
        name, labels = self.layers[self.current_layer] if \
            self.current_layer < len(self.layers) else ("?", [])
        self.painter.draw(cr, width, height, self.current_layer, name, labels)

    # chamados pela thread de HID via GLib.idle_add
    def on_layer(self, layer):
        self.status_msg = None
        self.current_layer = layer
        if not self.always and layer == 0:
            self.set_visible(False)
            return False
        self.set_visible(True)
        self.area.queue_draw()
        return False

    def on_status(self, msg):
        self.status_msg = msg
        self.area.queue_draw()
        if msg is None:
            if self.always:
                self.set_visible(True)
        else:
            self.set_visible(True)
        return False


class OverlayApp(Gtk.Application):
    def __init__(self, layers, scale, always):
        super().__init__(application_id="dev.corne.layeroverlay",
                         flags=0)
        self._args = (layers, scale, always)
        self.win = None
        self.reader = None

    def do_activate(self):
        if self.win is None:
            self.win = OverlayWindow(self, *self._args)
            self.reader = HidReader(self.win.on_layer, self.win.on_status)
            self.evdev_reader = EvdevReader(self.win.on_layer)
            self.reader.start()
            self.evdev_reader.start()
        # janela já presente; só mostra se 'always', senão fica oculta até evento
        if self._args[2]:  # always
            self.win.present()
        else:
            self.win.set_visible(False)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

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

    app = OverlayApp(layers, args.scale, args.always)
    app.run(None)


if __name__ == "__main__":
    main()
