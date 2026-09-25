"""One headless 86Box: a private Xvfb for its window, a pty for COM1.

Every process is started here and stopped here by the pid it was started
with. Nothing is ever found by matching the process table: a pattern
matches the shell that runs it and whatever wraps that shell. The pids go
to CASE/pids as well, so a run that died without cleaning up can be
finished off by the next run in the same case folder -- after checking
that each pid still runs the binary it was recorded with.
"""

import os
import pty
import shutil
import select
import signal
import subprocess
import threading
import time

_display_lock = threading.Lock()

# Nothing reaches a desktop session or an audio server.
_DROP = ("WAYLAND_DISPLAY", "DISPLAY", "XDG_SESSION_TYPE", "PULSE_SERVER", "PULSE_COOKIE",
         "PIPEWIRE_REMOTE", "PIPEWIRE_RUNTIME_DIR", "XDG_RUNTIME_DIR")


def _exe(pid):
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
    def __init__(self, case, binary):
        self.case = case
        self.binary = binary
        self.procs = []
        self.buf = b""
        self.display = None
        self.log = open(os.path.join(case, "serial.log"), "wb")

    def start(self, cfg_text):
        """cfg_text has "@SERIAL@" where the COM1 pipe path goes."""
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
            for n in range(200, 400):
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
        if not self.display:
            self.stop()
            raise SystemExit("Xvfb would not start")

        self.master, slave = pty.openpty()
        os.set_blocking(self.master, False)
        cfg = os.path.join(self.case, "86box.cfg")
        open(cfg, "w").write(cfg_text.replace("@SERIAL@", os.ttyname(slave)))

        env.update(DISPLAY=self.display, QT_QPA_PLATFORM="xcb", SDL_VIDEODRIVER="x11")
        self.box = subprocess.Popen([self.binary, "-P", self.case, "-C", cfg, "-V", "cputest"],
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

    def alive(self):
        return self.box.poll() is None

    def keys(self, *keys):
        """Keystrokes into the emulator window (the guest sees them unless
        they are one of 86Box's own shortcuts)."""
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
        subprocess.run(["import", "-display", self.display, "-window", "root", path], env=self.env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        return path

    def stop(self):
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
            end = time.time() + 3
            while time.time() < end and any(p.poll() is None for p in self.procs):
                time.sleep(0.1)
        left = [p.pid for p in self.procs if p.poll() is None]
        if left:
            raise SystemExit("%s: pids %s will not stop" % (self.case, left))
        path = os.path.join(self.case, "pids")
        if os.path.exists(path):
            os.unlink(path)
