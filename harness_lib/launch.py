"""One headless 86Box: a private Xvfb for its window (Qt's offscreen
platform on macOS, which has no X server), and COM1 on a pty or, with a
build that has it, on the automation socket.

Every process is started here and stopped here by the pid it was started
with. Nothing is ever found by matching the process table: a pattern
matches the shell that runs it and whatever wraps that shell. The pids go
to CASE/pids as well, so a run that died without cleaning up can be
finished off by the next run in the same case folder -- after checking
that each pid still runs the binary it was recorded with.
"""

import json
import os
import pty
import shutil
import select
import signal
import socket
import subprocess
import sys
import threading
import time

_display_lock = threading.Lock()
_MACOS = sys.platform == "darwin"

# Nothing reaches a desktop session or an audio server.
_DROP = ("WAYLAND_DISPLAY", "DISPLAY", "XDG_SESSION_TYPE", "PULSE_SERVER", "PULSE_COOKIE",
         "PIPEWIRE_REMOTE", "PIPEWIRE_RUNTIME_DIR", "XDG_RUNTIME_DIR")


def _exe(pid):
    if _MACOS:
        # No /proc: ps names the executable a process was started from.
        out = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return os.path.realpath(out) if out else None
    try:
        return os.readlink("/proc/%d/exe" % pid)
    except OSError:
        return None


def _x_listening(n):
    """Whether display :n answers. On WSL, /tmp/.X11-unix is WSLg's and
    holds no socket of ours: Xvfb listens only on the abstract one."""
    import socket
    sk = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sk.connect("\0/tmp/.X11-unix/X%d" % n)
        return True
    except OSError:
        return False
    finally:
        sk.close()


def clear_stale(case):
    """Stop what a dead earlier run in this case folder left running."""
    path = os.path.join(case, "pids")
    if not os.path.exists(path):
        return
    for line in open(path):
        pid, _, exe = line.strip().partition(" ")
        if not pid.isdigit():
            continue
        pid = int(pid)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if _exe(pid) != exe:
                break
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                break
            time.sleep(1.0)
        if _exe(pid) == exe:
            raise SystemExit("%s: pid %d from an earlier run will not stop" % (case, pid))
    os.unlink(path)


class Box:
    """com1="pty": COM1 is a named pipe on a pseudo-terminal, read as it
    comes. com1="socket": COM1 has 86Box's automation backend
    (serial1_device = automation), and pump() reads it with `serial read`
    over the automation socket, which also gives the emulated time; the
    caller passes the socket's path to connect()."""

    def __init__(self, case, binary, com1="pty"):
        self.case = case
        self.binary = binary
        self.com1 = com1
        self.procs = []
        self.buf = b""
        self.display = None
        self.log = open(os.path.join(case, "serial.log"), "wb")
        self.sk = None
        self.next = 0        # socket: the COM1 position to read from next
        self.emu_ms = 0      # socket: emulated ms at the last read
        self.last_ms = None  # socket: emulated ms of the last byte COM1 sent
        self.lost = 0        # socket: bytes dropped before they were read

    def start(self, cfg_text, args=()):
        """cfg_text has "@SERIAL@" where the COM1 pipe path goes (pty only);
        args go on the emulator's command line."""
        clear_stale(self.case)
        env = {k: v for k, v in os.environ.items() if k not in _DROP}
        runtime = os.path.join(self.case, "runtime")
        os.makedirs(runtime, mode=0o700, exist_ok=True)
        env.update(PULSE_SERVER="unix:/nonexistent/pulse", ALSOFT_DRIVERS="null", SDL_AUDIODRIVER="dummy",
                   AUDIODEV="null", XDG_RUNTIME_DIR=runtime)

        # An explicit display number: -displayfd walks every display from 0
        # and, where /tmp/.X11-unix belongs to someone else (WSLg), never
        # settles on one. A number whose lock another server took first
        # makes this Xvfb exit, and the next number is tried.
        with _display_lock:
            for n in ([] if _MACOS else range(200, 400)):
                if os.path.exists("/tmp/.X%d-lock" % n) or _x_listening(n):
                    continue
                xvfb = subprocess.Popen(["Xvfb", ":%d" % n, "-screen", "0", "1024x768x24", "-nolisten", "tcp"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
                self._track(xvfb, shutil.which("Xvfb"))
                end = time.time() + 10
                while time.time() < end and xvfb.poll() is None and not _x_listening(n):
                    time.sleep(0.1)
                if xvfb.poll() is None and _x_listening(n):
                    self.display = ":%d" % n
                    break
                # SIGTERM, so it removes its own lock file.
                if xvfb.poll() is None:
                    xvfb.terminate()
                xvfb.wait()
        if not self.display and not _MACOS:
            self.stop()
            raise SystemExit("Xvfb would not start")

        if self.com1 == "pty":
            self.master, slave = pty.openpty()
            os.set_blocking(self.master, False)
            cfg_text = cfg_text.replace("@SERIAL@", os.ttyname(slave))
        cfg = os.path.join(self.case, "86box.cfg")
        open(cfg, "w").write(cfg_text)

        if _MACOS:
            env.update(QT_QPA_PLATFORM="offscreen")
        else:
            env.update(DISPLAY=self.display, QT_QPA_PLATFORM="xcb", SDL_VIDEODRIVER="x11")
        self.box = subprocess.Popen([self.binary, "-P", self.case, "-C", cfg, "-V", "cputest"] + list(args),
                                    stdout=open(os.path.join(self.case, "86box.out"), "w"),
                                    stderr=subprocess.STDOUT, env=env, cwd=self.case, start_new_session=True)
        self._track(self.box, self.binary)
        self.env = env

    def _track(self, proc, binary):
        # The binary it was told to run, not /proc/PID/exe: straight after
        # Popen the child can still be a fork of this Python.
        self.procs.append(proc)
        with open(os.path.join(self.case, "pids"), "a") as f:
            f.write("%d %s\n" % (proc.pid, os.path.realpath(binary)))

    def pump(self, seconds):
        if self.com1 == "socket":
            return self._pump_socket(seconds)
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([self.master], [], [], 0.25)
            if r:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    data = b""
                if data:
                    self.buf += data
                    self.log.write(data)
                    self.log.flush()

    def connect(self, sock, seconds=20):
        """Connect to the automation socket at sock, once 86Box has made it.
        The connection stays open: 86Box takes one client at a time. With
        COM1 on a pty, it is read the while."""
        end = time.time() + seconds
        while time.time() < end and self.alive():
            if os.path.exists(sock):
                sk = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sk.settimeout(10)
                try:
                    sk.connect(sock)
                    self.sk, self.rbuf, self.sock = sk, b"", sock
                    return True
                except OSError:
                    sk.close()
            if self.com1 == "pty":
                self.pump(0.2)
            else:
                time.sleep(0.2)
        return False

    def request(self, line):
        """One automation request; its reply, as a dict. OSError when the
        connection is gone (86Box exited, say)."""
        if not self.sk:
            raise OSError("not connected")
        try:
            self.sk.sendall(line.encode() + b"\n")
            while b"\n" not in self.rbuf:
                chunk = self.sk.recv(1 << 20)
                if not chunk:
                    raise OSError("automation socket closed")
                self.rbuf += chunk
        except OSError:
            # A reply that came late would answer the next request: start
            # again on a new connection instead (COM1 is read by position,
            # so nothing is lost).
            self.sk.close()
            self.sk = None
            raise
        reply, self.rbuf = self.rbuf.split(b"\n", 1)
        return json.loads(reply)

    def automation_ff(self, sock, seconds=20):
        """Fast forward on through the automation socket at sock, for a build
        with no other way to start fast-forwarded."""
        if not self.sk and not self.connect(sock, seconds):
            return False
        try:
            return self.request("ff on").get("ff") is True
        except OSError:
            return False

    def _read_com1(self):
        """One `serial read` of whatever COM1 has sent since the last."""
        r = self.request("serial read 1 %d" % self.next)
        if not r.get("ok"):
            raise OSError("serial read: %s" % r.get("error"))
        data = bytes.fromhex(r["data"])
        self.lost += r["lost"]
        self.next = r["next"]
        self.emu_ms = r["ms"]
        if r["pos"]:
            self.last_ms = r["last_ms"]
        if data:
            self.buf += data
            self.log.write(data)
            self.log.flush()
            # When COM1 said what, in emulated time: to the read, not the byte.
            if not hasattr(self, "times"):
                self.times = open(os.path.join(self.case, "serial.times"), "w")
            self.times.write("%d %d\n" % (self.emu_ms, len(data)))
            self.times.flush()
        return len(data)

    def _pump_socket(self, seconds):
        end = time.time() + seconds
        while True:
            if not self.sk and getattr(self, "sock", None) and self.alive():
                self.connect(self.sock, 0.5)
            try:
                got = self._read_com1()
            except OSError:
                time.sleep(max(0, min(0.25, end - time.time())))
                got = 0
            # A full read can mean more is waiting.
            if time.time() >= end:
                return
            if got < 65536:
                time.sleep(min(0.25, max(0, end - time.time())))

    def now(self):
        """The watchdogs' clock, in seconds: emulated with COM1 on the
        socket, the host's with the pty."""
        return self.emu_ms / 1000.0 if self.com1 == "socket" else time.time()

    def com1_last(self):
        """When COM1 last sent anything, on the clock of now(), or None
        (socket only: the pty has no such record)."""
        return None if self.last_ms is None else self.last_ms / 1000.0

    def alive(self):
        return self.box.poll() is None

    def keys(self, *keys):
        """Keystrokes into the emulator window (the guest sees them unless
        they are one of 86Box's own shortcuts). Not on macOS: no X server."""
        if _MACOS:
            return False
        wins = subprocess.run(["xdotool", "search", "--name", "86Box"], env=self.env,
                              capture_output=True, text=True).stdout.split()
        if not wins:
            return False
        subprocess.run(["xdotool", "windowfocus", "--sync", wins[0]], env=self.env, capture_output=True, timeout=10)
        for k in keys:
            subprocess.run(["xdotool", "key", k], env=self.env, capture_output=True, timeout=10)
            # Back to back, the guest BIOS drops all but the first.
            time.sleep(0.5)
        return True

    def shot(self, name):
        path = os.path.join(self.case, name)
        if _MACOS:
            return path  # offscreen: there is no screen to grab
        subprocess.run(["import", "-display", self.display, "-window", "root", path], env=self.env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        return path

    def stop(self):
        if self.sk:
            # What COM1 sent last, before 86Box goes.
            try:
                if self.com1 == "socket":
                    while self._read_com1() == 65536:
                        pass
            except OSError:
                pass
            self.sk.close()
            self.sk = None
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for p in reversed(self.procs):
                if p.poll() is None:
                    try:
                        if p is getattr(self, "box", None):
                            os.killpg(p.pid, sig)
                        else:
                            p.send_signal(sig)
                    except ProcessLookupError:
                        pass
            # COM1 is read the while: on macOS a process that dies with
            # output left in its terminal waits for it to drain first.
            end = time.time() + 3
            while time.time() < end and any(p.poll() is None for p in self.procs):
                if self.com1 == "pty" and hasattr(self, "master"):
                    self.pump(0.1)
                else:
                    time.sleep(0.1)
        left = [p.pid for p in self.procs if p.poll() is None]
        if left:
            raise SystemExit("%s: pids %s will not stop" % (self.case, left))
        # 86Box leaves its socket behind, and one too long for the case
        # folder goes in results/.
        for path in (os.path.join(self.case, "pids"), getattr(self, "sock", None)):
            if path and os.path.exists(path):
                os.unlink(path)
