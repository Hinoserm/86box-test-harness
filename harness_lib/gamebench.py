"""Game benchmarks: one timed run of a game on one 86Box build.

Each game runs on a machine whose 86box.cfg is in bench/ and whose disks live outside
the repository (they hold commercial games), in the directory local.toml's [bench]
section names for that machine. Every run starts from the machine's saved disks and
CMOS (FILE.base and nvr.base copied over FILE and nvr), held: emulated time passes
only inside the automation socket's step, key and wait requests, so every run and
every host sees the same inputs at the same emulated times. Fast forward is on
throughout; the figure is host seconds.

  doom    doom   (MS-DOS, S3): DOOM 1.9 `-timedemo demo3` looping from AUTOEXEC.BAT;
          host seconds per pass, median of 5 (the first, with the boot, not counted).
  q3      gm98   (Windows 98 SE, Voodoo3 3000): Quake III 1.11 `timedemo 1; demo
          demo001` at 800x600 from Start > Run; host seconds from Quake's first frame
          to the desktop's return. Quake's own frames/seconds/fps from qconsole.log.
  dagger  gmdos  (MS-DOS 7.1, EMM386, 64 MB): Daggerfall 1.07.213 loads save slot 1
          and stands; host seconds for 30 s of emulated time.
  shock   gmdos: Terminator: Future Shock loads save slot 1 and stands; 30 s.
  aitd    gmdos: Alone in the Dark (CD) left alone runs its own demo; 30 s of it from
          100 s after the game starts.

The first and last frames of the timed stretch are saved as TAG-first.png and
TAG-last.png beside the run's log, to check the run got where it should."""

import json
import os
import platform
import shutil
import signal
import socket
import struct
import subprocess
import time
import zlib

GAMES = ("doom", "q3", "dagger", "shock", "aitd")
MACHINE = {"doom": "doom", "q3": "gm98", "dagger": "gmdos", "shock": "gmdos", "aitd": "gmdos"}
DISKS = {"doom": ("dos.img",), "gm98": ("c.img", "games.img"), "gmdos": ("c.img", "games.img")}
CD = {"dagger": "dagger.iso", "shock": "termfs.iso", "aitd": "aitd.cue"}
MEASURE_MS = 30000
SLICE_MS = 500  # the timed stretch runs in slices this long, for the slowest second

SC = {"esc": 0x01, "enter": 0x1c, "shift": 0x2a, "win": 0x15b, "space": 0x39}
ROWS = [("1234567890-=", 0x02), ("qwertyuiop[]", 0x10), ("asdfghjkl;'`", 0x1e), ("\\zxcvbnm,./", 0x2b)]
CH = {c: base + i for chars, base in ROWS for i, c in enumerate(chars)}
CH.update({" ": 0x39, "\n": 0x1c})
SHIFTED = {"+": "=", ":": ";", "_": "-", '"': "'"}
POINTER = {(145, 186, 246), (178, 206, 255), (105, 153, 218)}  # Daggerfall's blue pointer


class Run:
    def __init__(self, binary, game, data, template, out, tag):
        self.binary, self.game, self.data, self.out, self.tag = binary, game, data, out, tag
        self.template = template
        self.sock = os.path.join(data, "a.sock")

    # --- the automation socket ------------------------------------------------------
    def req(self, line, timeout=3600):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(self.sock)
        s.sendall((line + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            c = s.recv(1 << 20)
            if not c:
                break
            buf += c
        s.close()
        return json.loads(buf)

    def step(self, ms):
        return self.req("step %d" % ms)

    def press(self, *codes):
        self.req("key down " + " ".join("0x%x" % c for c in codes))
        self.req("key up " + " ".join("0x%x" % c for c in reversed(codes)))

    def key(self, name):
        self.press(*[SC[p] if p in SC else CH[p] for p in name.split("+")])

    def typ(self, text):
        for c in text:
            if c.isalpha() and c.isupper():
                self.press(SC["shift"], CH[c.lower()])
            elif c in SHIFTED:
                self.press(SC["shift"], CH[SHIFTED[c]])
            else:
                self.press(CH[c])

    def click(self):
        self.req("mouse 0 0 1")
        self.step(200)
        self.req("mouse 0 0 0")
        self.step(300)

    def move(self, dx, dy):
        while dx or dy:
            sx, sy = max(-60, min(60, dx)), max(-60, min(60, dy))
            self.req("mouse %d %d 0" % (sx, sy))
            self.step(150)
            dx, dy = dx - sx, dy - sy

    def shot(self, name):
        p = os.path.join(self.out, "%s-%s.png" % (self.tag, name))
        self.req("screenshot " + p)
        return p

    def text(self):
        t = self.req("text")
        return "\n".join(bytes.fromhex(r).decode("cp437", "replace") for r in t.get("chars", []))

    # --- Daggerfall's pointer, found in a screenshot (no PIL needed) --------------------
    def pointer(self):
        w, h, bpp, rows = png_rgb(self.shot("_ptr"))
        for y in range(h):
            r = rows[y]
            for x in range(w):
                if (r[x * bpp], r[x * bpp + 1], r[x * bpp + 2]) in POINTER:
                    return x, y
        return None

    def goto(self, tx, ty):
        for _ in range(8):
            p = self.pointer()
            if p is None:
                self.move(5, 5)
                continue
            dx, dy = tx - p[0], ty - p[1]
            if abs(dx) <= 2 and abs(dy) <= 2:
                return
            self.move(round(dx / 3.4), round(dy / 1.72))
            self.step(200)
        raise RuntimeError("pointer did not reach %d,%d" % (tx, ty))

    # --- the machine --------------------------------------------------------------------
    def prepare(self):
        m = MACHINE[self.game]
        for n in DISKS[m]:
            shutil.copyfile(os.path.join(self.data, n + ".base"), os.path.join(self.data, n))
        os.makedirs(os.path.join(self.data, "nvr"), exist_ok=True)
        for n in os.listdir(os.path.join(self.data, "nvr.base")):
            shutil.copyfile(os.path.join(self.data, "nvr.base", n), os.path.join(self.data, "nvr", n))
        cfg = open(self.template).read()
        if self.game in CD:
            cfg = cfg.replace("@CD@", CD[self.game])
        open(os.path.join(self.data, "86box.cfg"), "w").write(cfg)
        os.makedirs(self.out, exist_ok=True)

    def start(self):
        if os.path.exists(self.sock):
            os.unlink(self.sock)
        env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY", "PULSE_SERVER")}
        env.update(QT_QPA_PLATFORM="offscreen", PULSE_SERVER="unix:/nonexistent/pulse", ALSOFT_DRIVERS="null",
                   SDL_AUDIODRIVER="dummy")
        pre = ["taskset", "-c", "0-7"] if platform.system() == "Linux" else []
        self.box = subprocess.Popen(pre + [self.binary, "-P", self.data, "--assume_copied_vm", "-L",
                                           os.path.join(self.out, self.tag + ".log"),
                                           "--automation", "unix:" + self.sock, "--automation-hold"],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
        for _ in range(120):
            if os.path.exists(self.sock):
                break
            time.sleep(0.5)
        else:
            self.box.kill()
            raise RuntimeError("no automation socket")
        self.req("ff on")

    def stop(self):
        try:
            self.req("quit", timeout=10)
        except OSError:
            pass
        try:
            self.box.wait(30)
        except subprocess.TimeoutExpired:
            os.kill(self.box.pid, signal.SIGKILL)
            self.box.wait()

    def slice(self, request):
        """One request inside the timed stretch, kept as (host s, emulated s) for the
        slowest second (slowest())."""
        t = time.monotonic()
        r = self.req(request)
        self.slices.append((time.monotonic() - t, (r["ms"] - self.ms) / 1000.0))
        self.ms = r["ms"]
        return r

    def mark(self):
        """Where the timed stretch's slices start."""
        self.ms = self.req("status")["ms"]

    def slowest(self):
        """The slowest stretch of one emulated second, as a speed (1.0 = real time):
        under fast forward, a second the host took longer than a second over is one
        that would have run below 100% without it."""
        worst = None
        for i in range(len(self.slices)):
            h = g = 0.0
            for dh, dg in self.slices[i:]:
                h, g = h + dh, g + dg
                if g >= 1.0:
                    break
            if g >= 1.0 and h > 0:
                worst = g / h if worst is None else min(worst, g / h)
        return worst

    def timed(self, ms):
        self.mark()
        a = self.ms
        self.shot("first")
        t = time.monotonic()
        while self.ms - a < ms:
            self.slice("step %d" % min(SLICE_MS, ms - (self.ms - a)))
        host = time.monotonic() - t
        self.shot("last")
        return host, (self.ms - a) / 1000.0

    def dos_prompt(self):
        if not self.req("wait text 120000 C:\\>", timeout=600).get("matched"):
            raise RuntimeError("no DOS prompt")

    # --- the games ----------------------------------------------------------------------
    def run_doom(self):
        marks = []
        for i in range(6):  # pass 0 has the boot in it
            if i == 0:
                r = self.req("wait text 1800000 timed", timeout=3600)
            else:
                # In slices, for the slowest second (the first pass, with the boot, not).
                for _ in range(4 * 1800):
                    r = self.slice("wait text %d timed" % SLICE_MS)
                    if r.get("matched"):
                        break
            if not r.get("matched"):
                raise RuntimeError("no timedemo result")
            marks.append((time.monotonic(), r["ms"]))
            if i == 0:
                self.shot("first")
                self.step(5000)  # past the result line, into the next pass
                self.mark()
            else:
                self.slice("step 5000")
        self.shot("last")
        # Pass i lasts from result line i to line i + 1 (the 5 s stepped after a line
        # included); pass 0, with the boot, is not counted.
        host = [b[0] - a[0] for a, b in zip(marks[1:], marks[2:])]
        guest = [(b[1] - a[1]) / 1000.0 for a, b in zip(marks[1:], marks[2:])]
        return median(host), median(guest)

    def run_q3(self):
        self.step(50000)  # Windows to the desktop
        self.key("win+r")
        self.step(1500)
        self.typ("d:\\quake3\\quake3.exe +set r_mode 4 +set logfile 2 +set timedemo 1 +set nextdemo quit "
                 "+demo demo001\n")
        t0 = t1 = None
        for _ in range(4 * 600):
            if t0 is None:
                self.step(SLICE_MS)
            else:
                self.slice("step %d" % SLICE_MS)
            s = self.req("status")
            if t0 is None and s["w"] == 800:
                t0, g0 = time.monotonic(), s["ms"]
                self.shot("first")
                self.mark()
            elif t0 is not None and s["w"] == 640:
                t1, g1 = time.monotonic(), s["ms"]
                break
        if t1 is None:
            raise RuntimeError("Quake did not finish")
        self.step(2000)
        self.shot("last")
        return t1 - t0, (g1 - g0) / 1000.0

    def run_dagger(self):
        self.dos_prompt()
        self.typ("d:\ncd \\dagger\ndagger\n")
        self.step(25000)  # the launcher, then the logo video
        self.key("esc")
        self.step(15000)  # main menu
        self.key("l")
        self.step(4000)  # the load screen: six slots, LOAD GAME on top
        self.goto(160, 57)
        self.click()
        self.goto(318, 19)
        self.click()
        self.step(20000)  # the save loads
        return self.timed(MEASURE_MS)

    def run_shock(self):
        self.dos_prompt()
        self.typ("d:\ncd \\games\\shock\nshock\n")
        self.step(25000)  # menu bar; the pointer starts at 325,210
        self.move(-54, -98)  # LOAD
        self.move(-14, -49)
        self.click()
        self.step(3000)
        self.move(60, 55)  # slot 1
        self.click()
        self.step(25000)  # the mission loads
        return self.timed(MEASURE_MS)

    def run_aitd(self):
        self.dos_prompt()
        self.typ("cd \\infogram\\indark\nindark\n")
        self.step(100000)  # logos, credits, the menu left alone, then the game's demo
        return self.timed(MEASURE_MS)

    def go(self):
        self.slices = []
        self.prepare()
        self.start()
        try:
            host, guest = getattr(self, "run_" + self.game)()
        finally:
            self.stop()
        self.slowest_speed = self.slowest()
        extra = ""
        if self.game == "q3":
            log = fat32_read(os.path.join(self.data, "games.img"), "QUAKE3/BASEQ3/QCONSOLE.LOG") or b""
            lines = [l for l in log.decode("latin-1").splitlines() if " frames, " in l]
            extra = lines[-1].strip() if lines else "no result in qconsole.log"
        return host, guest, extra


def median(v):
    v = sorted(v)
    n = len(v)
    return (v[n // 2] + v[(n - 1) // 2]) / 2 if n else 0.0


def png_rgb(path):
    """(width, height, bytes per pixel, rows) of an 8-bit RGB or RGBA PNG."""
    d = open(path, "rb").read()
    pos, idat, w, h, bpp = 8, b"", 0, 0, 3
    while pos < len(d):
        n, t = struct.unpack(">I4s", d[pos:pos + 8])
        body = d[pos + 8:pos + 8 + n]
        if t == b"IHDR":
            w, h, _, ctype = struct.unpack(">IIBB", body[:10])
            bpp = {2: 3, 6: 4}[ctype]
        elif t == b"IDAT":
            idat += body
        pos += 12 + n
    raw, out, prev = zlib.decompress(idat), [], bytearray(w * bpp)
    stride = w * bpp
    for y in range(h):
        f, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + b) & 255
            elif f == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        out.append(bytes(line))
        prev = line
    return w, h, bpp, out


def fat32_read(img, path, part_lba=63):
    """A file out of a FAT32 partition in a disk image (no mtools needed)."""
    f = open(img, "rb")
    f.seek(part_lba * 512)
    bs = f.read(512)
    bps, spc, rsvd, nfats = struct.unpack_from("<HBHB", bs, 11)
    fatsz, root = struct.unpack_from("<I", bs, 36)[0], struct.unpack_from("<I", bs, 44)[0]
    fat0 = part_lba * 512 + rsvd * bps
    data0 = fat0 + nfats * fatsz * bps
    csize = spc * bps

    def read_chain(c):
        out = b""
        while c < 0x0ffffff8:
            f.seek(data0 + (c - 2) * csize)
            out += f.read(csize)
            f.seek(fat0 + c * 4)
            c = struct.unpack("<I", f.read(4))[0] & 0x0fffffff
        return out

    cl, size = root, None
    for part in path.upper().split("/"):
        d, found = read_chain(cl), None
        for i in range(0, len(d), 32):
            e = d[i:i + 32]
            if e[0] == 0:
                break
            if e[0] == 0xe5 or e[11] == 0x0f:
                continue
            name = e[:8].decode("latin-1").rstrip()
            ext = e[8:11].decode("latin-1").rstrip()
            if (name + ("." + ext if ext else "")) == part:
                found = (struct.unpack_from("<H", e, 20)[0] << 16) | struct.unpack_from("<H", e, 26)[0]
                size = struct.unpack_from("<I", e, 28)[0]
                break
        if found is None:
            return None
        cl = found
    return read_chain(cl)[:size]
