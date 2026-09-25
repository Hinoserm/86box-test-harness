"""What an image's COM1 output means.

Each parser reads one run's COM1 text (parse), and judges the runs of one
CPU family together (judge), since some checks compare machines.

  generic   a run passes if it reached its done line, that line matches
            the image's `pass` pattern (when it has one), and nothing
            matched its `fail` pattern
  cputest   the CPU accuracy test's GROUP lines: interpreter against
            recompiler, against a real CPU's capture in golden/, pass
            against pass, and machine against machine
"""

import os
import re


class Generic:
    def parse(self, text, image):
        text = text.replace("\r", "")
        done = [l for l in text.splitlines() if image.done.search(l)]
        failed = bool(image.failed and image.failed.search(text))
        return dict(version=None, cpu=None, done=done, failed=failed)

    def judge(self, family, runs, image, golden_dir):
        lines, ok = [], True
        for r in runs:
            p = r["parsed"]
            bad = []
            if r["status"] != "done":
                bad.append(r["status"])
            elif image.passed and not image.passed.search(p["done"][-1]):
                bad.append("done line does not say it passed: %s" % p["done"][-1])
            if p["failed"]:
                bad.append("a line matched the fail pattern")
            ok = ok and not bad
            lines.append("%s  %s%s" % (r["tag"], "FAIL: " + "; ".join(bad) if bad else "ok", notes(r)))
        return lines, ok

    def golden_text(self, text):
        return None


class CpuTest:
    def parse(self, text, image):
        r = dict(version=None, cpu=None, groups={}, skipped={}, done=[], pass_diff=set())
        for line in text.replace("\r", "").splitlines():
            line = line.strip()
            m = re.match(r"CPUTEST (\S+) .*?cpu=(.*)$", line)
            if m:
                r["version"], r["cpu"] = m.group(1), m.group(2)
            elif line.startswith("GROUP "):
                parts = line.split()
                if len(parts) > 2 and parts[2] == "skipped:":
                    r["skipped"][parts[1]] = " ".join(parts[3:])
                elif len(parts) > 2:
                    g = dict(p.split("=", 1) for p in parts[2:] if "=" in p)
                    prev = r["groups"].get(parts[1])
                    # Every pass must print the same CRCs.
                    if prev and any(prev.get(k) != g.get(k) for k in ("crc_interp", "crc_comp", "crc_defined")):
                        r["pass_diff"].add(parts[1])
                    r["groups"].setdefault(parts[1], g)
            elif image.done.search(line):
                r["done"].append(line)
        return r

    def judge(self, family, runs, image, golden_dir):
        lines, ok = [], True
        w = lines.append
        golden = {}
        for r in runs:
            v = r["parsed"]["version"]
            if v and v not in golden:
                path = os.path.join(golden_dir, "v%s.log" % v)
                golden[v] = self.parse(open(path, errors="replace").read(), image) if os.path.exists(path) else None
        for r in runs:
            p = r["parsed"]
            g = golden.get(p["version"])
            w("%s  [%s, %d s%s]" % (r["tag"], r["status"], r["seconds"], notes(r, sep=", ")))
            if p["cpu"]:
                w("  cpu %s" % p["cpu"])
            if r["status"] != "done":
                ok = False
            for name, gr in p["groups"].items():
                bad = []
                if gr.get("mismatches", "0") != "0":
                    bad.append("%s interp/recompiler mismatches" % gr["mismatches"])
                ref = g["groups"].get(name) if g else None
                if ref and ref.get("crc_defined") != gr.get("crc_defined"):
                    bad.append("defined %s, real CPU %s" % (gr.get("crc_defined"), ref.get("crc_defined")))
                if name in p["pass_diff"]:
                    bad.append("passes disagree")
                note = ""
                if ref and ref.get("crc_raw") != gr.get("crc_raw"):
                    note = "  (undefined flags differ from the real CPU)"
                ok = ok and not bad
                w("  %-4s %-16s defined=%s raw=%s%s%s" % ("FAIL" if bad else "ok", name, gr.get("crc_defined"),
                                                           gr.get("crc_raw"),
                                                           ("  " + "; ".join(bad)) if bad else "", note))
            # One line per reason: the loop.* groups alone skip dozens at a time.
            by_why = {}
            for name, why in p["skipped"].items():
                by_why.setdefault(why, []).append(name)
            for why, names in by_why.items():
                w("  skip %s: %s" % (why, " ".join(names) if len(names) <= 4 else "%d groups (%s ... %s)"
                                      % (len(names), names[0], names[-1])))
            if p["version"] and not g:
                w("  (no real-CPU reference for %s v%s: compared only interpreter against recompiler)"
                  % (family, p["version"]))
        # The same CPU model must give the same answers on every board and
        # at every speed: the clock changes nothing the tests look at.
        for name in sorted({n for r in runs for n in r["parsed"]["groups"]}):
            seen = {r["tag"]: r["parsed"]["groups"][name].get("crc_defined")
                    for r in runs if name in r["parsed"]["groups"]}
            if len(set(seen.values())) > 1:
                ok = False
                w("machines disagree on %s: %s" % (name, ", ".join("%s=%s" % kv for kv in seen.items())))
        return lines, ok

    def golden_text(self, text):
        """A real machine's capture, cut to its header and one pass of
        GROUP lines."""
        keep = [l for l in text.replace("\r", "").splitlines() if l.startswith(("CPUTEST ", "GROUP "))]
        seen, out = set(), []
        for l in keep:
            key = l.split()[1] if l.startswith("GROUP ") else "header"
            if key not in seen:
                seen.add(key)
                out.append(l)
        return "\n".join(out) + "\n"


def notes(r, sep="  "):
    return (sep + ", ".join(r["notes"])) if r["notes"] else ""


PARSERS = {"generic": Generic, "cputest": CpuTest}


def get(name):
    if name not in PARSERS:
        raise SystemExit("no parser %r; there are: %s" % (name, ", ".join(PARSERS)))
    return PARSERS[name]()
