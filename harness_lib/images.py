"""Test images: a disk under images/ plus a descriptor, images/NAME.toml,
that says how to tell from COM1 that it started, is working, and is done.
"""

import hashlib
import os
import re
import shutil
import subprocess
import tomllib

# Every machine's saved CMOS has its first IDE drive typed as this: 64
# cylinders, 16 heads, 32 sectors -- 16 MiB. An image may be smaller (it
# is padded); a larger one would not match the CMOS.
DISK_BYTES = 16 * 1024 * 1024
HDD_PARAMETERS = "32, 16, 64, 0, ide"


class Image:
    def __init__(self, root, name):
        self.name = name
        self.root = root
        path = os.path.join(root, "images", name + ".toml")
        if not os.path.exists(path):
            known = ", ".join(names(root)) or "none"
            raise SystemExit("no image %r (images/%s.toml); known: %s" % (name, name, known))
        with open(path, "rb") as f:
            d = tomllib.load(f)
        self.description = d.get("description", "")
        self.disk = os.path.join(root, "images", d.get("disk", name + ".img"))
        self.parser = d.get("parser", "generic")
        self.start = re.compile(d["start"], re.M)
        self.working = re.compile(d["working"], re.M) if d.get("working") else None
        self.done = re.compile(d["done"], re.M)
        self.passed = re.compile(d["pass"], re.M) if d.get("pass") else None
        self.failed = re.compile(d["fail"], re.M) if d.get("fail") else None
        self.start_wait = d.get("start_wait", 10)
        self.working_wait = d.get("working_wait", 5)
        self.stall = d.get("stall", 60)

    def info(self):
        if not os.path.exists(self.disk):
            return "%s: no disk (%s)" % (self.name, os.path.relpath(self.disk, self.root))
        h = hashlib.sha256(open(self.disk, "rb").read()).hexdigest()[:12]
        src = self.disk + ".source"
        origin = open(src).read().strip() if os.path.exists(src) else ""
        return "%s sha256:%s%s" % (os.path.relpath(self.disk, self.root), h, ("  " + origin) if origin else "")

    def copy_to(self, dst):
        """The run's own copy of the disk, padded to the CMOS geometry."""
        if not os.path.exists(self.disk):
            raise SystemExit("%s missing: run harness sync %s, or put it there" % (self.disk, self.name))
        size = os.path.getsize(self.disk)
        if size > DISK_BYTES:
            raise SystemExit("%s is %d bytes; the machines' CMOS describe a 16 MiB disk" % (self.disk, size))
        shutil.copy2(self.disk, dst)
        if size < DISK_BYTES:
            with open(dst, "r+b") as f:
                f.truncate(DISK_BYTES)


def names(root):
    d = os.path.join(root, "images")
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".toml")) if os.path.isdir(d) else []


def sync(root, name, source):
    """Copy an image's disk in from SOURCE (local.toml [images.NAME]
    source). Inside a git tree, the committed version is taken, never a
    file a build is halfway through writing."""
    img = Image(root, name)
    src = os.path.abspath(os.path.expanduser(source))
    repo = subprocess.run(["git", "-C", os.path.dirname(src), "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True).stdout.strip()
    dst = img.disk
    if repo:
        rel = os.path.relpath(src, repo)
        blob = subprocess.run(["git", "-C", repo, "show", "HEAD:" + rel], capture_output=True)
        if blob.returncode:
            raise SystemExit("%s is not committed in %s" % (rel, repo))
        open(dst + ".tmp", "wb").write(blob.stdout)
        os.replace(dst + ".tmp", dst)
        rev = subprocess.run(["git", "-C", repo, "log", "-1", "--format=%h %s"],
                             capture_output=True, text=True).stdout.strip()
        origin = "from %s at %s" % (src, rev)
    else:
        if not os.path.exists(src):
            raise SystemExit("%s does not exist" % src)
        shutil.copy2(src, dst)
        origin = "from %s (not in git)" % src
    open(dst + ".source", "w").write(origin + "\n")
    return img.info()
