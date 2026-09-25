# The test bench

Runs the test image on every emulated machine on file that can hold a given
CPU, and checks what comes out.

```
./cputest sync            # fetch the newest committed test image
./cputest list k6-3       # which boards take it, at which speeds
./cputest k6-3            # run it on all of them
```

## Layout

```
cputest              the runner
local.toml           your builds and paths (not committed; see local.example.toml)
machines/<socket>/<board>/
    86box.cfg        the machine; the runner sets the CPU, disk and COM1 itself
    nvr/*.nvr        its saved CMOS
images/cputest.img   the test disk, from `cputest sync` (not committed)
golden/<family>/v<N>.log
                     a real CPU's output for test version N
results/<time>-<family>/
    summary.txt      the verdict
    <board>-<speed>/ serial.log, screen.png, 86box.out, the cfg it ran
```

## What a run does

For each machine whose socket, bus, voltage and multipliers take the CPU,
by 86Box's own rules read from the source tree of the build under test,
it:

1. copies the machine into `results/` (CMOS only, never a flash dump) and
   sets `cpu_family`, `cpu_speed` and `cpu_multi`;
2. boots it headless and fast-forwarded (`fast_forward = 1`; a build
   without that option is refused), COM1 on a pty;
3. fails it if the `CPUTEST` line is not on COM1 within 10 s of power-on,
   or the first test output within 3 s after that, or if COM1 then goes
   quiet for 30 s;
4. stops at the first `DONE` line (`--passes N` for more).

A board without a saved CMOS is skipped: its BIOS would stop at a setup
prompt instead of booting the disk.

The default speed is the fastest in-spec one the board takes;
`--speed 350` picks one, `--all-speeds` runs them all.

A group fails on interpreter/recompiler mismatches, a defined-result
CRC that differs from the real CPU's (when `golden/` has one for this test
version), passes that disagree, or two machines disagreeing on the same CPU model, at any speed.
Undefined flags that differ from the real CPU are only noted.

## Builds

`local.toml` names builds; `--box master` picks one, `--box PATH` takes any
binary under `<tree>/<builddir>/src/86Box` (anywhere else with `--source
TREE`, e.g. a frozen copy of a build), and `CPUTEST_BOX` sets the default. The source tree is needed: the CPU and machine tables come from it,
and so does the revision printed in the report.

## Real-CPU references

Capture a real machine's COM1 through at least one pass, then:

```
./cputest golden k6-3 k6-3-capture.txt
```

## Machines on file

| socket | board | chipset | CPUs 86Box lets it take |
|---|---|---|---|
| Socket 5 | ASUS PCI/I-P54SP4 | SiS 501 | P54C to 100 |
| Socket 5 | Suk Jung SJ-P54CSR-100 | Intel 430FX | P54C to 133 |
| Socket 7 | ASUS P/I-P55T2P4 | Intel 430HX | P54C, P55C, K6, K6-2 to 400/66 |
| Socket 7 | FIC PA-2012 | VIA VP3 | P54C, P55C, K6, K6-2 to 400/66 |
| Super Socket 7 | ASUS P5A | ALi Aladdin V | P54C, P55C, K6-2, K6-III, K6-III+ |
| Super Socket 7 | FIC VA-503+ | VIA MVP3 | P55C, K6-2, K6-III, K6-III+ (no P54C: 3.2 V max) |

`cputest list CPU` gives the exact speeds, from the build in use.

## Adding a machine

Make `machines/<socket>/<board>/86box.cfg` (copy one and change `machine`),
then set up its BIOS through the bench:

```
./cputest setup BOARD
```

boots it with the image attached, fast-forwarded, and takes commands on
`results/setup-<socket>-<board>/ctl`: `key` (xdotool key names), `type`,
`shot NAME` (a screenshot), `save` (the CMOS it wrote, back into the
template) and `quit`. In the BIOS: the first IDE drive detected and set,
every other channel and both floppies none, halt on no errors, boot from
C first, then save and let it boot; `CPUTEST` on COM1 is the proof. Then
`save`.
