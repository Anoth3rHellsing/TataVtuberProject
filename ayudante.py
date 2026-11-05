#!/usr/bin/env python3
"""
Ayudante del mapache VTuber (opcional).

El navegador solo ve el ratón cuando está encima de su ventana, y OBS no le pasa ni
el ratón ni (a veces) el mando. Este programita lee el ratón, el teclado y el mando
de TODO el escritorio y se lo cuenta a la página por http://127.0.0.1:8765.

  python ayudante.py                 # arranca y sirve mapache.html en http://127.0.0.1:8765/
  python ayudante.py --demo          # se inventa el ratón y el mando (para probar sin tocar nada)
  python ayudante.py --sin-teclado   # no mira el teclado en absoluto
  python ayudante.py --puerto 9000   # otro puerto (cámbialo también en el panel)

Solo usa la biblioteca estándar de Python 3.8+. Escucha únicamente en 127.0.0.1.

Privacidad: del teclado solo se cuenta CUÁNTAS teclas se pulsan (para que el mapache
teclee), nunca cuáles. La única excepción son los atajos Ctrl+Alt+0…9, que cambian la
expresión del mapache. Con --sin-teclado no se lee nada.

Sistemas:
  Windows : ratón, teclado y mandos XInput (Xbox y compatibles).
  macOS   : ratón y teclado (pide permiso de «Monitorización de entrada»). El mando lo lee
            el propio navegador.
  Linux   : ratón y teclado en X11 (en Wayland puro no se puede leer el ratón global);
            mando por /dev/input/js0.
"""
import argparse
import ctypes
import ctypes.util
import json
import math
import os
import struct
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AQUI = os.path.dirname(os.path.abspath(__file__))
PAGINA = os.path.join(AQUI, 'mapache.html')

# orden del «mapeo estándar» de la Gamepad API del navegador
# 0 A, 1 B, 2 X, 3 Y, 4 LB, 5 RB, 6 LT, 7 RT, 8 Select, 9 Start, 10 LS, 11 RS, 12 Arriba, 13 Abajo, 14 Izq., 15 Der., 16 Casa
N_BOTONES = 17


class Estado:
    """Lo que se manda a la página. Protegido con un Condition para despertar a los clientes."""

    def __init__(self):
        self.cond = threading.Condition()
        self.datos = {'m': [0.5, 0.5], 'b': 0, 'k': 0, 'h': None, 'hs': 0, 'p': None}
        self.version = 0

    def poner(self, **kw):
        with self.cond:
            cambio = False
            for k, v in kw.items():
                if self.datos.get(k) != v:
                    self.datos[k] = v
                    cambio = True
            if cambio:
                self.version += 1
                self.cond.notify_all()

    def esperar(self, version, timeout=1.0):
        with self.cond:
            if self.version == version:
                self.cond.wait(timeout)
            return self.version, json.dumps(self.datos, separators=(',', ':'))


ESTADO = Estado()


# ---------------------------------------------------------------------------
#  LECTORES POR SISTEMA. Cada uno da: raton() -> (x, y, ancho, alto, x0, y0),
#  botones() -> bits (1 izq., 2 der., 4 centro), teclas() -> set de códigos pulsados,
#  y los códigos de Ctrl, Alt y los dígitos 0…9.
# ---------------------------------------------------------------------------
class LectorWindows:
    nombre = 'Windows'

    def __init__(self):
        from ctypes import wintypes
        self.u32 = ctypes.windll.user32
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                self.u32.SetProcessDPIAware()
            except Exception:
                pass

        class POINT(ctypes.Structure):
            _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]
        self.POINT = POINT
        self.u32.GetAsyncKeyState.restype = ctypes.c_short
        self.ctrl = {0x11, 0xA2, 0xA3}
        self.alt = {0x12, 0xA4, 0xA5}
        self.digitos = {0x30 + i: i for i in range(10)}
        self.digitos.update({0x60 + i: i for i in range(10)})          # teclado numérico
        self.modificadores = {0x10, 0x11, 0x12, 0x14, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}
        self.codigos = [c for c in range(0x08, 0xFF) if c not in (0x01, 0x02, 0x04, 0x05, 0x06)]
        # XInput
        self.xi = None
        for dll in ('xinput1_4', 'xinput1_3', 'xinput9_1_0'):
            try:
                self.xi = getattr(ctypes.windll, dll)
                break
            except Exception:
                continue

        class XGAMEPAD(ctypes.Structure):
            _fields_ = [('wButtons', wintypes.WORD), ('bLeftTrigger', ctypes.c_ubyte), ('bRightTrigger', ctypes.c_ubyte),
                        ('sThumbLX', ctypes.c_short), ('sThumbLY', ctypes.c_short), ('sThumbRX', ctypes.c_short), ('sThumbRY', ctypes.c_short)]

        class XSTATE(ctypes.Structure):
            _fields_ = [('dwPacketNumber', wintypes.DWORD), ('Gamepad', XGAMEPAD)]
        self.XSTATE = XSTATE
        self.pad_slot = None
        self.pad_buscar = 0.0

    def raton(self):
        p = self.POINT()
        self.u32.GetCursorPos(ctypes.byref(p))
        gs = self.u32.GetSystemMetrics
        x0, y0, w, h = gs(76), gs(77), gs(78), gs(79)            # escritorio virtual (todas las pantallas)
        if w <= 0 or h <= 0:
            x0, y0, w, h = 0, 0, gs(0), gs(1)
        return p.x, p.y, w, h, x0, y0

    def pulsada(self, c):
        return bool(self.u32.GetAsyncKeyState(c) & 0x8000)

    def botones(self):
        b = 0
        if self.u32.GetSystemMetrics(23):                         # botones del ratón intercambiados (zurdos)
            izq, der = 0x02, 0x01
        else:
            izq, der = 0x01, 0x02
        if self.pulsada(izq): b |= 1
        if self.pulsada(der): b |= 2
        if self.pulsada(0x04): b |= 4
        return b

    def teclas(self):
        return {c for c in self.codigos if self.pulsada(c)}

    def mando(self):
        if not self.xi:
            return None
        ahora = time.time()
        slots = [self.pad_slot] if self.pad_slot is not None else ([] if ahora < self.pad_buscar else range(4))
        for i in slots:
            st = self.XSTATE()
            if self.xi.XInputGetState(i, ctypes.byref(st)) == 0:
                self.pad_slot = i
                g = st.Gamepad
                w = g.wButtons
                bit = lambda m: 1.0 if w & m else 0.0
                b = [bit(0x1000), bit(0x2000), bit(0x4000), bit(0x8000), bit(0x0100), bit(0x0200),
                     g.bLeftTrigger / 255.0, g.bRightTrigger / 255.0, bit(0x0020), bit(0x0010), bit(0x0040), bit(0x0080),
                     bit(0x0001), bit(0x0002), bit(0x0004), bit(0x0008), bit(0x0400)]
                ej = lambda v: max(-1.0, min(1.0, v / 32767.0))
                a = [ej(g.sThumbLX), -ej(g.sThumbLY), ej(g.sThumbRX), -ej(g.sThumbRY)]
                return {'id': f'XInput {i + 1}', 'b': [round(x, 2) for x in b], 'a': [round(x, 2) for x in a]}
        self.pad_slot = None
        self.pad_buscar = ahora + 2.0                             # buscar mandos desconectados es lento: cada 2 s
        return None


class LectorMac:
    nombre = 'macOS'

    def __init__(self):
        cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library('ApplicationServices')
                                     or '/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices')
        cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library('CoreFoundation')
                                     or '/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')

        class CGPoint(ctypes.Structure):
            _fields_ = [('x', ctypes.c_double), ('y', ctypes.c_double)]

        class CGSize(ctypes.Structure):
            _fields_ = [('w', ctypes.c_double), ('h', ctypes.c_double)]

        class CGRect(ctypes.Structure):
            _fields_ = [('o', CGPoint), ('s', CGSize)]
        cg.CGEventCreate.restype = ctypes.c_void_p
        cg.CGEventCreate.argtypes = [ctypes.c_void_p]
        cg.CGEventGetLocation.restype = CGPoint
        cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cg.CGEventSourceButtonState.restype = ctypes.c_bool
        cg.CGEventSourceButtonState.argtypes = [ctypes.c_int32, ctypes.c_uint32]
        cg.CGEventSourceKeyState.restype = ctypes.c_bool
        cg.CGEventSourceKeyState.argtypes = [ctypes.c_int32, ctypes.c_uint16]
        cg.CGGetActiveDisplayList.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
        cg.CGDisplayBounds.restype = CGRect
        cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
        self.cg, self.cf = cg, cf
        self.ctrl = {59, 62}
        self.alt = {58, 61}
        self.digitos = {29: 0, 18: 1, 19: 2, 20: 3, 21: 4, 23: 5, 22: 6, 26: 7, 28: 8, 25: 9,
                        82: 0, 83: 1, 84: 2, 85: 3, 86: 4, 87: 5, 88: 6, 89: 7, 91: 8, 92: 9}
        self.modificadores = {54, 55, 56, 57, 58, 59, 60, 61, 62, 63}
        self.limites = None
        self.limites_t = 0.0

    def pantallas(self):
        if self.limites and time.time() - self.limites_t < 5:
            return self.limites
        ids = (ctypes.c_uint32 * 16)()
        n = ctypes.c_uint32(0)
        self.cg.CGGetActiveDisplayList(16, ids, ctypes.byref(n))
        x0 = y0 = 1e9
        x1 = y1 = -1e9
        for i in range(n.value):
            r = self.cg.CGDisplayBounds(ids[i])
            x0, y0 = min(x0, r.o.x), min(y0, r.o.y)
            x1, y1 = max(x1, r.o.x + r.s.w), max(y1, r.o.y + r.s.h)
        if x1 <= x0:
            x0, y0, x1, y1 = 0, 0, 1440, 900
        self.limites = (x0, y0, x1 - x0, y1 - y0)
        self.limites_t = time.time()
        return self.limites

    def raton(self):
        ev = self.cg.CGEventCreate(None)
        p = self.cg.CGEventGetLocation(ev)
        self.cf.CFRelease(ev)
        x0, y0, w, h = self.pantallas()
        return p.x, p.y, w, h, x0, y0

    def botones(self):
        b = 0
        if self.cg.CGEventSourceButtonState(0, 0): b |= 1
        if self.cg.CGEventSourceButtonState(0, 1): b |= 2
        if self.cg.CGEventSourceButtonState(0, 2): b |= 4
        return b

    def teclas(self):
        return {c for c in range(128) if self.cg.CGEventSourceKeyState(0, c)}

    def mando(self):
        return None


class LectorX11:
    nombre = 'Linux (X11)'

    def __init__(self):
        lib = ctypes.util.find_library('X11')
        if not lib:
            raise OSError('no encuentro libX11')
        x = ctypes.cdll.LoadLibrary(lib)
        x.XOpenDisplay.restype = ctypes.c_void_p
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XDefaultRootWindow.restype = ctypes.c_ulong
        x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x.XQueryPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                                    ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                                    ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_uint)]
        x.XQueryKeymap.argtypes = [ctypes.c_void_p, ctypes.c_char * 32]
        x.XDisplayWidth.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.x = x
        self.d = x.XOpenDisplay(None)
        if not self.d:
            raise OSError('no hay pantalla X11 (¿Wayland sin XWayland, o falta DISPLAY?)')
        self.root = x.XDefaultRootWindow(self.d)
        self.ctrl = {37, 105}
        self.alt = {64, 108}
        self.digitos = {19: 0, **{9 + i: i for i in range(1, 10)}}
        self.modificadores = {37, 105, 50, 62, 64, 108, 133, 134, 66}
        self.mask = 0
        self.js = None
        self.js_buscar = 0.0
        self.js_b = [0.0] * N_BOTONES
        self.js_a = [0.0] * 8

    def raton(self):
        r, c = ctypes.c_ulong(), ctypes.c_ulong()
        rx, ry, wx, wy = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        m = ctypes.c_uint()
        self.x.XQueryPointer(self.d, self.root, ctypes.byref(r), ctypes.byref(c), ctypes.byref(rx), ctypes.byref(ry),
                             ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(m))
        self.mask = m.value
        return rx.value, ry.value, self.x.XDisplayWidth(self.d, 0), self.x.XDisplayHeight(self.d, 0), 0, 0

    def botones(self):
        b = 0
        if self.mask & (1 << 8): b |= 1
        if self.mask & (1 << 10): b |= 2
        if self.mask & (1 << 9): b |= 4
        return b

    def teclas(self):
        km = (ctypes.c_char * 32)()
        self.x.XQueryKeymap(self.d, km)
        raw = bytes(km)
        return {i * 8 + j for i in range(32) for j in range(8) if raw[i] >> j & 1}

    def mando(self):
        # /dev/input/js0 (driver xpad: 0 A, 1 B, 2 X, 3 Y, 4 LB, 5 RB, 6 Back, 7 Start, 8 Guía, 9 LS, 10 RS;
        # ejes 0 LX, 1 LY, 2 LT, 3 RX, 4 RY, 5 RT, 6 cruceta X, 7 cruceta Y)
        if self.js is None:
            if time.time() < self.js_buscar:
                return None
            self.js_buscar = time.time() + 2
            for n in range(4):
                try:
                    self.js = os.open(f'/dev/input/js{n}', os.O_RDONLY | os.O_NONBLOCK)
                    break
                except OSError:
                    continue
            if self.js is None:
                return None
        try:
            while True:
                ev = os.read(self.js, 8)
                if len(ev) < 8:
                    break
                _, valor, tipo, num = struct.unpack('IhBB', ev)
                tipo &= 0x7F
                if tipo == 1:
                    mapa = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 8, 7: 9, 8: 16, 9: 10, 10: 11}
                    if num in mapa:
                        self.js_b[mapa[num]] = 1.0 if valor else 0.0
                elif tipo == 2 and num < 8:
                    self.js_a[num] = valor / 32767.0
        except BlockingIOError:
            pass
        except OSError:
            os.close(self.js)
            self.js = None
            return None
        a = self.js_a
        b = list(self.js_b)
        b[6], b[7] = max(0.0, (a[2] + 1) / 2), max(0.0, (a[5] + 1) / 2)
        b[12], b[13], b[14], b[15] = float(a[7] < -.5), float(a[7] > .5), float(a[6] < -.5), float(a[6] > .5)
        return {'id': 'Mando (Linux)', 'b': [round(x, 2) for x in b], 'a': [round(a[0], 2), round(a[1], 2), round(a[3], 2), round(a[4], 2)]}


class LectorDemo:
    nombre = 'demostración'

    def __init__(self):
        self.ctrl, self.alt, self.digitos, self.modificadores = set(), set(), {}, set()
        self.t0 = time.time()

    def raton(self):
        t = time.time() - self.t0
        return 960 + math.sin(t * .9) * 800, 540 + math.sin(t * 1.7) * 400, 1920, 1080, 0, 0

    def botones(self):
        t = time.time() - self.t0
        return (1 if math.sin(t * 3.1) > .9 else 0) | (2 if math.sin(t * 2.3 + 1) > .96 else 0)

    def teclas(self):
        t = time.time() - self.t0
        return {int(t * 8) % 40} if math.sin(t * 1.9) > .3 else set()

    def mando(self):
        t = time.time() - self.t0
        if int(t / 8) % 2 == 0:
            return None
        b = [1.0 if int(t * 2.2) % 9 == i % 9 else 0.0 for i in range(N_BOTONES)]
        return {'id': 'demo', 'b': b, 'a': [round(math.sin(t * 1.4), 2), round(math.cos(t * 1.1) * .6, 2), round(math.sin(t * .8), 2), 0]}


def crear_lector(demo):
    if demo:
        return LectorDemo()
    if sys.platform.startswith('win'):
        return LectorWindows()
    if sys.platform == 'darwin':
        return LectorMac()
    return LectorX11()


def bucle(lector, args):
    """Lee el sistema a ~120 Hz y actualiza ESTADO. Solo pasa la posición normalizada del ratón."""
    previas = set()
    contador = 0
    atajo_n = 0
    errores = 0
    while True:
        try:
            x, y, w, h, x0, y0 = lector.raton()
            nx = min(1.0, max(0.0, (x - x0) / max(1, w)))
            ny = min(1.0, max(0.0, (y - y0) / max(1, h)))
            datos = {'m': [round(nx, 4), round(ny, 4)], 'b': lector.botones()}
            if not args.sin_teclado:
                ahora = lector.teclas()
                nuevas = ahora - previas
                con_mods = bool(ahora & lector.ctrl) and bool(ahora & lector.alt)
                for c in nuevas:
                    if con_mods and c in lector.digitos:
                        atajo_n += 1
                        datos['h'] = lector.digitos[c]
                        datos['hs'] = atajo_n
                    elif c not in lector.modificadores and not con_mods:
                        contador += 1
                previas = ahora
                datos['k'] = contador
            if not args.sin_mando:
                datos['p'] = lector.mando()
            ESTADO.poner(**datos)
            errores = 0
        except Exception as e:                                      # nunca dejar de servir por un fallo de lectura
            errores += 1
            if errores in (1, 100):
                print('  aviso: no pude leer la entrada:', e)
        time.sleep(1 / 120)


class Manejador(BaseHTTPRequestHandler):
    server_version = 'MapacheAyudante/1.0'

    def log_message(self, *a):
        pass

    def cabeceras(self, codigo, tipo):
        self.send_response(codigo)
        self.send_header('Content-Type', tipo)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()

    def do_GET(self):
        ruta = self.path.split('?')[0]
        if ruta in ('/', '/mapache.html', '/index.html'):
            try:
                with open(PAGINA, 'rb') as f:
                    cuerpo = f.read()
            except OSError:
                self.cabeceras(404, 'text/plain; charset=utf-8')
                self.wfile.write('No encuentro mapache.html junto a ayudante.py'.encode('utf-8'))
                return
            self.cabeceras(200, 'text/html; charset=utf-8')
            self.wfile.write(cuerpo)
        elif ruta == '/eventos':
            self.cabeceras(200, 'text/event-stream')
            version = -1
            try:
                while True:
                    version, txt = ESTADO.esperar(version, timeout=1.0)
                    self.wfile.write(f'data: {txt}\n\n'.encode('utf-8'))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return
        elif ruta == '/estado':
            self.cabeceras(200, 'application/json')
            self.wfile.write(ESTADO.esperar(-2, 0)[1].encode('utf-8'))
        else:
            self.cabeceras(404, 'text/plain; charset=utf-8')
            self.wfile.write(b'404')


def main():
    ap = argparse.ArgumentParser(description='Ayudante del mapache VTuber: ratón, teclado y mando globales.')
    ap.add_argument('--puerto', type=int, default=8765)
    ap.add_argument('--demo', action='store_true', help='inventa ratón, teclas y mando para probar')
    ap.add_argument('--sin-teclado', action='store_true', help='no leer el teclado')
    ap.add_argument('--sin-mando', action='store_true', help='no leer el mando (déjaselo al navegador)')
    ap.add_argument('--abrir', action='store_true', help='abrir la página en el navegador al arrancar')
    args = ap.parse_args()

    try:
        lector = crear_lector(args.demo)
    except Exception as e:
        print('No pude leer el ratón de este sistema:', e)
        print('Prueba con --demo para ver que la conexión funciona.')
        sys.exit(1)

    threading.Thread(target=bucle, args=(lector, args), daemon=True).start()
    try:
        srv = ThreadingHTTPServer(('127.0.0.1', args.puerto), Manejador)
    except OSError as e:
        print(f'El puerto {args.puerto} está ocupado ({e}). Prueba con --puerto 8766 y cámbialo también en el panel.')
        sys.exit(1)
    srv.daemon_threads = True
    url = f'http://127.0.0.1:{args.puerto}/'
    print('Ayudante del mapache en marcha ·', lector.nombre)
    print('  Página para OBS (Fuente de navegador):', url)
    print('  Teclado:', 'apagado' if args.sin_teclado else 'solo se cuentan pulsaciones (y Ctrl+Alt+número)')
    print('  Ctrl+C para salir.')
    if args.abrir:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\nHasta luego.')


if __name__ == '__main__':
    main()
