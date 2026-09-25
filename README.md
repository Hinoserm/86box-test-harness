# 86box-test-harness

Boot test images on emulated machines in [86Box](https://github.com/86Box/86Box),
headless and fast-forwarded, many machines in a row, and judge what they print on
COM1.

```
./harness run cputest --family 586 --cpu k6-3      # every Socket 5/7/SS7 board that takes a K6-III
./harness run cputest --test-all                   # every machine, with every CPU on the list it takes
./harness run myimage --socket slot1               # each Slot 1 board with its own CPU
```

A run goes like this: pick the machines (by family, socket or board), pick the
CPUs (or keep each machine's own), then boot each combination, one at a time.
Each machine starts from its saved CMOS, with the test image as its first IDE
disk and COM1 on a pseudo-terminal. The harness watches COM1 and fails the
machine the moment it stops making progress. Then it writes one report.

It started as the bench for [corsac-cputest](https://github.com/Hinoserm/corsac-cputest),
an x86 accuracy test that compares 86Box's interpreter with its recompiler.
Any bootable image that reports on COM1 can use it.

## Requirements

- Linux (WSL2 works), Python 3.11+
- `Xvfb`, `xdotool`, and ImageMagick's `import` (screenshots)
- An 86Box **build and the source tree it came from**. The harness reads that
  tree's machine and CPU tables, so it always agrees with the emulator under
  test, and it reads the git revision for the report. The build must have the
  `fast_forward` config option ([General] `fast_forward = 1`): every machine
  starts fast-forwarded, and a build without the option is refused.
- The 86Box ROM set

## Setup

```
cp local.example.toml local.toml     # your builds, ROM path, image sources
./harness images                     # the images on file
./harness sync cputest               # fetch an image's disk (or copy it into images/)
./harness list --family 686          # the machines on file, and the CPUs each takes
```

## Commands

| command | what it does |
|---|---|
| `run IMAGE [selection] [options]` | boot IMAGE on every selected machine/CPU and judge it |
| `list [selection]` | machines on file, and the in-spec speeds of each CPU family they take |
| `images` | the images on file, with the disk each one has now |
| `sync [IMAGE]` | copy an image's disk in from its `local.toml` source (the committed version, if the source is in git) |
| `setup BOARD` | boot one board for its BIOS setup, driven through a control pipe (see *Adding a machine*) |
| `golden IMAGE CPU LOG` | keep a real machine's serial capture as the reference for that CPU |

**Selecting machines** (all repeatable, and combinable):

- `--family 486|586|686`: groups of sockets, from `families.toml`
- `--socket ss7`: one folder under `machines/`
- `--machine P5A`: one board (or `ss7/P5A`)

**Selecting CPUs:**

- none: each machine runs the CPU in its own `86box.cfg`
- `--cpu k6-3` (repeatable): an alias from `cpus.toml`, or any 86Box CPU family's
  internal name. Machines that can't take it are skipped with the reason.
  Whether a board takes a CPU is decided by 86Box's own rules (package, bus,
  voltage, multiplier), read from the build.
- `--test-all`: every CPU in `cpus.toml [test_all]`, on every selected machine
  that takes it
- `--speed 350` for one speed, or `--all-speeds`. The default is the fastest
  in-spec speed each board takes.

**Options:** `--box NAME|PATH` for the build (a name from `local.toml`, or a
binary; add `--source TREE` for a binary kept outside its tree, e.g. a frozen
copy); `-j N` for machines at once (default 1); `--passes N` for done lines to
wait for; `--timeout S` per run; `--interp` for the recompiler off.

## Images

An image is a disk in `images/` plus a descriptor, `images/NAME.toml`:

```toml
description = "what it tests"
disk = "myimage.img"      # in images/; not committed
parser = "generic"        # or "cputest"

start = "^MYTEST "        # the first COM1 line that proves it booted
working = "^PROGRESS "    # optional: the first line that proves it is working
done = "^DONE "           # the end of a pass
pass = "PASS$"            # generic parser: the done line must match this
fail = "^FAIL"            # generic parser: any line matching this fails the run

start_wait = 10           # wall-clock seconds from power-on to `start`
working_wait = 3          # from `start` to `working`
stall = 30                # the longest COM1 silence allowed after that
```

What an image must do:

- **Boot from a 16 MiB disk.** Every machine's saved CMOS types its first IDE
  drive as 64 cylinders, 16 heads, 32 sectors (16 MiB). A smaller image is
  padded; a larger one is refused. The boot sector is loaded by the BIOS like
  any other.
- **Report on COM1** (3F8h). The harness only reads, so any speed works.
  corsac-cputest uses 115200 8N1.
- **Say something early and keep talking.** Fast-forwarded boards reach the
  boot sector in 2 to 3 seconds. A machine that misses `start_wait`, or goes
  quiet for `stall`, is failed with a screenshot, instead of hanging the whole
  run.

**Parsers:**

- `generic`: the run passes if it reached its done line, that line matches
  `pass`, and nothing matched `fail`.
- `cputest`: reads corsac-cputest's GROUP lines. A group fails on:
  - interpreter/recompiler mismatches;
  - a defined-result CRC that differs from a real CPU's capture in
    `golden/IMAGE/FAMILY/`;
  - two passes that disagree;
  - two machines that disagree on the same CPU model at any speed.

New parsers go in `harness_lib/parsers.py`.

## Machines

`machines/<socket>/<board>/` holds an `86box.cfg` and `nvr/` with the board's
saved CMOS. A run never boots the template itself. It copies it into `results/`:
the CMOS only (flash dumps are rebuilt from the BIOS ROM), and private copies of
the emulator binary and the disk. Then it sets the CPU, the disk and COM1 in
the copy's cfg.

| socket | board | chipset | BIOS |
|---|---|---|---|
| socket5 | ASUS PCI/I-P54SP4 | SiS 501 | Award 4.51G |
| socket5 | Suk Jung SJ-P54CSR-100 | Intel 430FX | Award 4.50G |
| socket7 | ASUS P/I-P55T2P4 | Intel 430HX | Award |
| socket7 | FIC PA-2012 | VIA VP3 | Award 4.51PG |
| ss7 | ASUS P5A | ALi Aladdin V | Award |
| ss7 | FIC VA-503+ | VIA MVP3 | Award 4.60PGA |
| socket8 | AOpen AP61 | Intel 450KX | Award 4.50PG |
| socket8 | ASUS P/I-P6RP4 | Intel 450GX | AMI WinBIOS 1994 |
| socket8 | Gigabyte GA-686NX | Intel 440FX | Award 4.51PG |
| slot1 | ASUS P3B-F | Intel 440BX | Award Medallion 6.0 |

`./harness list` gives the CPUs each takes, from the build in use.

### Adding a machine

1. Copy a board's folder, and change `machine =` to the new board's 86Box
   internal name. The CPU lines set its default CPU.
2. `./harness setup BOARD` boots it with the first image attached,
   fast-forwarded, and takes commands on `results/setup-<socket>-<board>/ctl`,
   one per line:
   - `key K ...`: xdotool key names into the emulator window (Delete, F1,
     Return, Next, Prior, minus, ...)
   - `type TEXT`
   - `shot NAME`: a screenshot, `NAME.png` in the same folder
   - `save`: copies the CMOS the machine has written back into the template
   - `quit`
3. In the BIOS:
   - detect the first IDE drive and keep it (it must come out 64/16/32);
   - set every other channel and both floppies to none;
   - set halt on no errors (AMI: *Wait for F1 if any error* off);
   - boot from C first; quick POST on;
   - save, and let it boot.
   
   The image's `start` line on COM1 is the proof. Then `save` and `quit`.
4. Run the image on the new socket's boards straight away, to prove they
   work from a cold boot.

Tips from setting up the boards above:

- Send one key at a time and take a screenshot after each step. BIOSes drop
  keys that arrive together, and some drop the first key after a menu opens.
- Award's "Select Primary Master (N=Skip)" takes `y` on some BIOSes and `1`
  on others.
- 86Box computes some AMI BIOSes' extended CMOS checksum itself
  (`NVR_AMI_1995` and similar). Where that doesn't fit the BIOS, the saved
  CMOS is rejected on every boot. The AMI Apollo (Socket 5) is left out for
  that reason.
- Some boards find the disk only on a cold boot, not on the warm reboot
  straight after leaving setup. Runs are always cold boots.

## Builds

`local.toml` names builds: `--box master` picks one, `HARNESS_BOX` sets the
default, and `--box PATH [--source TREE]` takes any binary. Each run gets its
own copy of the binary, so rebuilding in the tree mid-run changes nothing.

## Results

```
results/<time>-<image>/
    summary.txt                      the verdict, one section per CPU family
    <socket>-<board>-<cpu>-<speed>/  serial.log, screen.png, 86box.out, the cfg it ran
```

`RESULT PASS` or `RESULT FAIL` ends the summary, and the exit code follows it.

## How processes are handled

Each machine gets a private Xvfb (displays :200 and up), and its COM1 on a pty.
Every process is started by the harness and stopped by the pid it was started
with. Nothing is ever found by matching the process table. The pids are also
written to the case folder, so a run that died can be cleaned up by the next
run in the same folder, after checking that each pid still runs the binary
recorded for it.

## License

MIT; see [LICENSE](LICENSE).
