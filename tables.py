"""86Box's own machine and CPU tables, read from the source tree of the
build under test, and its rule for which CPUs a machine takes.

The tables are parsed, not copied, so the bench can never disagree with the
emulator it drives: a board or a CPU speed added upstream shows up here the
moment the build that has it is selected. is_eligible() is a straight port
of cpu_is_eligible() in src/cpu/cpu.c.
"""

import os
import re
from dataclasses import dataclass, field


@dataclass
class Cpu:
    name: str
    cpu_type: str
    rspeed: int
    multi: float
    voltage: int
    flags: set
    out_of_spec: bool


@dataclass
class Family:
    internal_name: str
    name: str
    manufacturer: str
    package: set
    cpus: list = field(default_factory=list)


@dataclass
class Machine:
    internal_name: str
    name: str
    init: str
    package: set
    block: set
    min_bus: int
    max_bus: int
    min_voltage: int
    max_voltage: int
    min_multi: float
    max_multi: float


def _blocks(text, start):
    """The text of each top-level { ... } at or after start, up to the
    brace that closes the enclosing list."""
    out = []
    depth = 0
    begin = None
    i = start
    while i < len(text):
        c = text[i]
        if c == "/" and text.startswith("/*", i):
            i = text.index("*/", i) + 2
            continue
        if c == "/" and text.startswith("//", i):
            i = text.index("\n", i)
            continue
        if c == '"':
            i = text.index('"', i + 1) + 1
            continue
        if c == "{":
            if depth == 0:
                begin = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                out.append(text[begin:i + 1])
            if depth < 0:
                break
        i += 1
    return out


def _field(body, name, default=None):
    m = re.search(r"\.%s\s*=\s*([^,\n}]+)" % name, body)
    return m.group(1).strip() if m else default


def _string(body, name):
    m = re.search(r'\.%s\s*=\s*"([^"]*)"' % name, body)
    return m.group(1) if m else ""


def _number(value, default=0):
    if value is None:
        return default
    value = value.strip()
    if re.fullmatch(r"[0-9.]+\s*/\s*[0-9.]+", value):
        a, b = value.split("/")
        return float(a) / float(b)
    try:
        return int(value, 0)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return default


def _names(value):
    """CPU_PKG_A | CPU_PKG_B -> {"CPU_PKG_A", "CPU_PKG_B"}; 0 -> {}."""
    if value is None:
        return set()
    return {n for n in re.findall(r"[A-Z][A-Z0-9_]+", value)}


def _strip_comments(text):
    return re.sub(r"//[^\n]*", "", text)


def load_cpus(src):
    text = open(os.path.join(src, "cpu", "cpu_table.c"), encoding="latin-1").read()
    families = {}
    for m in re.finditer(r"\.internal_name\s*=\s*\"([^\"]+)\",\s*\.cpus\s*=\s*\(const CPU\[\]\)\s*\{", text):
        head = text[text.rfind("{", 0, m.start()):m.start()]
        fam = Family(internal_name=m.group(1), name=_string(head, "name"),
                     manufacturer=_string(head, "manufacturer"),
                     package=_names(_field(head, "package")))
        for body in _blocks(text, m.end()):
            if ".cpu_type" not in body:
                continue
            # "/* out of spec */" sits just inside the entry's opening brace.
            fam.cpus.append(Cpu(name=_string(body, "name"),
                                cpu_type=_field(body, "cpu_type"),
                                rspeed=_number(_field(body, "rspeed")),
                                multi=float(_number(_field(body, "multi"), 1)),
                                voltage=_number(_field(body, "voltage")),
                                flags=_names(_field(body, "cpu_flags")),
                                out_of_spec="out of spec" in body[:80]))
        families[fam.internal_name] = fam
    return families


def load_machines(src):
    text = _strip_comments(open(os.path.join(src, "machine", "machine_table.c"), encoding="latin-1").read())
    machines = {}
    for m in re.finditer(r'\.name\s*=\s*"([^"]+)",\s*\.internal_name\s*=\s*"([^"]+)"', text):
        end = text.find(".aliases", m.end())
        body = text[m.start():end]
        cpu = re.search(r"\.cpu\s*=\s*\{(.*?)\n\s*\}", body, re.S)
        cpu = cpu.group(1) if cpu else ""
        block = re.search(r"\.block\s*=\s*CPU_BLOCK\(([^)]*)\)", cpu)
        machines[m.group(2)] = Machine(
            internal_name=m.group(2), name=m.group(1), init=_field(body, "init", ""),
            package=_names(_field(cpu, "package")),
            block=_names(block.group(1)) if block else set(),
            min_bus=_number(_field(cpu, "min_bus")), max_bus=_number(_field(cpu, "max_bus")),
            min_voltage=_number(_field(cpu, "min_voltage")), max_voltage=_number(_field(cpu, "max_voltage")),
            min_multi=float(_number(_field(cpu, "min_multi"))), max_multi=float(_number(_field(cpu, "max_multi"))))
    return machines


def is_eligible(fam, cpu, mach):
    """cpu_is_eligible(), src/cpu/cpu.c, without the override settings."""
    packages = set(mach.package)
    if "CPU_PKG_SOCKET3" in packages:
        packages.add("CPU_PKG_SOCKET1")
    elif "CPU_PKG_SLOT1" in packages:
        packages |= {"CPU_PKG_SOCKET370", "CPU_PKG_SOCKET8"}
    if not (fam.package & packages):
        return False

    # Cyrix 6x86MX on the NuPRO 592: cyrix_id 0x04xx, not parsed; refuse
    # the family there outright.
    if mach.init == "machine_at_nupro592_init" and cpu.cpu_type == "CPU_Cx6x86MX":
        return False
    if mach.init == "machine_at_cobalt3k_init" and cpu.multi not in (mach.min_multi, mach.max_multi):
        return False
    if cpu.cpu_type in mach.block:
        return False

    bus = int(cpu.rspeed / cpu.multi)
    if mach.init == "machine_at_ibm_pc700_init" and bus == 50000000 and cpu.multi == 2.0:
        return False
    if mach.min_bus and bus < mach.min_bus - 840907:
        return False
    if mach.max_bus and bus > mach.max_bus + 840907:
        return False
    if mach.min_voltage and cpu.voltage < mach.min_voltage - 100:
        return False
    if mach.max_voltage and cpu.voltage > mach.max_voltage + 100:
        return False

    if "CPU_FIXED_MULTIPLIER" in cpu.flags:
        return True
    multi = cpu.multi
    t = cpu.cpu_type
    if "CPU_PKG_SOCKET5_7" in fam.package:
        winchip = t in ("CPU_WINCHIP", "CPU_WINCHIP2")
        cx6x86 = t in ("CPU_Cx6x86", "CPU_Cx6x86L")
        if multi == 1.5 and t == "CPU_5K86" and mach.min_multi > 1.5:
            multi = 2.0
        elif multi == 1.75:
            multi = 2.5
        elif multi == 2.0:
            if t == "CPU_5K86":
                multi = 3.0
            elif t in ("CPU_K6_2P", "CPU_K6_3P"):
                multi = 2.5
            elif winchip and mach.min_multi > 2.0:
                multi = 2.5
        elif multi == 7.0 / 3.0:
            multi = 5.0
        elif multi == 8.0 / 3.0:
            multi = 5.5
        elif multi == 3.0 and cx6x86:
            multi = 1.5
        elif multi == 10.0 / 3.0:
            multi = 2.0
        elif multi == 3.5:
            multi = 1.5
        elif multi == 4.0:
            if winchip:
                if mach.min_multi >= 1.5:
                    multi = 1.5
                elif mach.min_multi >= 3.5:
                    multi = 3.5
                elif mach.min_multi >= 4.5:
                    multi = 4.5
            elif cx6x86:
                multi = 3.0
        elif multi == 5.0 and winchip and mach.min_multi > 5.0:
            multi = 5.5
        elif multi == 6.0:
            multi = 2.0
    if multi < mach.min_multi:
        return False
    if mach.max_multi and multi > mach.max_multi:
        return False
    return True
