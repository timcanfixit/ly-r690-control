# Modbus RTU Register Map — reverse-engineered from the v5 backup, 2026-08-02

Goal: enable a PC app to monitor live temperatures and update curve/profile data over the
DB9 serial port on the YL302V12 control board, without touching the touchscreen's own project.
Originally built entirely from local backup files (`context.dat`) as a candidate mapping; as of
2026-08-18, key parts of it are now **confirmed against live traffic** captured via a passive
RS485 tap (see "Live verification" below). Anything not explicitly marked as live-confirmed
below is still the original static-analysis candidate mapping.

## Live verification (2026-08-18)

Hardware: DTECH USB-to-RS485 adapter, wired in parallel (listen-only, never transmitting) across
the 2-wire RS485 link between the touchscreen's DB9 "COM" port and pins 7/8 of the control
board's DB9. Capture script: `tools/capture-modbus.ps1` (read-only, opens the COM port and logs
raw bytes — never writes).

- **Serial parameters confirmed**: 9600 baud, no parity, 8 data bits, 1 stop bit. Found by
  brute-force trying standard rates and checking which produced frames with valid Modbus CRC16
  checksums — 9600 produced clean, consistently repeating, fully valid frames.
- **Station/slave address confirmed as 1** — every frame starts with `01`.
- **Addressing offset discovered**: Modbus request addresses are the tag's numeric suffix minus
  1 (e.g. tag `M151` → Modbus coil address 150, `M172` → address 171). This is a 0-indexed vs.
  1-indexed naming convention, not an error in the original mapping — just remember to subtract
  1 from a tag's number to get the actual address to put on the wire.
- **Upper/Lower/Infrared/Probe registers resolved by matching live screen readings directly**
  (2026-08-18) — not by offset arithmetic, since it turned out to be a different offset than the
  coils entirely. Read the touchscreen's actual displayed values (Outside 28.2, Upper 29.2, Lower
  29.2, Infrared 28.2) at the same moment as a capture, and matched them against the raw register
  values: the recurring poll `FC3 read, start=20, qty=8` returns 8 registers, and addresses 20-23
  (`292, 292, 281, 282`) are exactly those 4 screen values at ×10 fixed-point precision, while
  addresses 24-27 (`29, 29, 28, 28`) are the *same 4 readings again*, just rounded to plain
  integers — a lower-precision duplicate, not separate data.

  Assigning individual addresses within the matched pairs (20/21 both read 29.2; 22/23 both read
  ~28.2) by following the original screen order confirmed in block 5 (Upper, Lower, Infrared,
  Outside in that sequence):

  | Tag | Meaning | Modbus address (FC3) |
  |-----|---------|----|
  | VW25 | Upper zone temperature | **20** |
  | VW26 | Lower zone temperature | **21** |
  | VW27 | Infrared zone temperature | **22** |
  | VW28 | Outside/Probe temperature | **23** |

  That's tag number **minus 5** — a different, larger offset than the coils' "minus 1." Values
  are ×10 fixed-point (292 = 29.2°C).

  **Infrared-vs-Outside pairing independently confirmed, 2026-08-18**: user manually altered the
  Outside/Probe sensor's reading; the on-screen Outside/Probe value (28.4°C) matched address 23
  exactly (`284`) while address 22 tracked separately — confirms `VW27`(Infrared)=address 22,
  `VW28`(Outside/Probe)=address 23 for certain, not just inferred from screen order.

  **Upper-vs-Lower independently confirmed, 2026-08-18**: user warmed the zones enough to make
  Upper and Lower diverge from each other; a capture taken immediately after reading the
  touchscreen showed `address20=33.6°C` vs on-screen Upper `33.7`, and `address21=35.3°C` vs
  on-screen Lower `35.6` (small gap from continued drift between capture and reading, but the
  relative order is unambiguous and matches). Confirms `VW25`(Upper)=address 20,
  `VW26`(Lower)=address 21 — the original screen-order assumption was correct.

  **All four temperature registers are now fully and independently confirmed** — no remaining
  ambiguity in this section.
- **`M172` = Vacuum, confirmed bidirectionally**: toggling the touchscreen's Vacuum button
  flips exactly one bit in the coil-poll response (`FC1 read, start=150, qty=29`) — byte 2 of
  the response (covering coils 166-173) goes from `0x04` to `0x24` (bit 5, i.e. coil 171 = tag
  `M172`) when Vacuum turns on, and back to `0x04` when it turns off. No other bits change.
- **`M173` = Cooling, confirmed bidirectionally**: same test, same coil-poll response byte goes
  from `0x04` to `0x44` (bit 6, i.e. coil 172 = tag `M173`) when Cooling turns on, back to `0x04`
  when off.
- **Start confirmed, 2026-08-18 — read this carefully, this is a real write command.** The
  machine has *two* separate Start mechanisms: a **physical button** on the machine housing, and
  a **Run button on the touchscreen itself**. They behave completely differently:
  - The **physical button** does not produce any Modbus write at all — it must be wired directly
    into the control board's own I/O, bypassing the touchscreen/Modbus link entirely. Pressing it
    only shows up indirectly, as `M151` (coil 150) going 0→1 in subsequent *reads* (confirmed
    across a real start→stop cycle: 0 idle → 1 while heating → 0 again after stopping, with a
    bonus finding that `M173`/Cooling turns on automatically after stopping — a sensible
    auto-cooldown behavior).
  - The **on-screen Run button** sends an actual write: `01 0F 00 96 00 01 01 01` — Function
    Code 15 (Write Multiple Coils), address 150 (`M151`), quantity 1, data `01` (set ON) — followed
    by the board's ACK. This is the literal command that starts a heating run from software.
  - **So `M151` is not just a status flag** — it doubles as the actual writable Start trigger
    (its "gates the curve-editing buttons" role from earlier is just it being read back
    afterward). Confirmed both directions: writing it starts the machine (matches the on-screen
    button); it also correctly reads back to 0 after Stop.

  **This is now the single most safety-critical fact in this document.** A PC script that knows
  this write can genuinely start the physical heater. Treat any future code that could send this
  write with the same case-by-case explicit-permission discipline used for the machine itself
  throughout this whole project — never trigger it without being asked, in the moment, for that
  specific action.
- Register `181` was also seen polled (`FC3 read, start=181, qty=1`), returning `0` — not yet
  identified in the tag map, outside all currently-documented ranges.
- Registers 20-23 (`292, 293, 282, 281` in one capture) track close to registers 24-27's values
  but roughly ×10 — possibly the same 4 zones at one-decimal precision alongside the integer
  versions, though this isn't confirmed, just an observed pattern worth checking further.

## How this was derived

1. Block 3 (`MCGS_DEV`, the device window) confirmed the touchscreen uses a `ModbusRTU` driver
   (`d:\mcgspro\program\drivers\modbus\modbusrtu\modbusrtuex_str.uid`).
2. A 32-byte-per-record binary table inside block 3 (offset 3229–8156 in the block's
   decompressed content) lists every device-linked data point: a `type` field holding the
   **actual Modbus function code** (1 = Read/Write Coils, 3 = Read/Write Holding Registers),
   and an `addr` field. 154 records total.
3. Cross-checked against the 154 `VW*`/`M*` tag names found in block 0 (`MCGS_DATACENTRE`, the
   real-time database): **every record's `addr` value is exactly the tag's own numeric
   suffix**, with zero mismatches and zero duplicates across all 154 (e.g. `VW131` → holding
   register 131 via FC3, `M165` → coil 165 via FC1). This part is a fully verified 1:1 match,
   not a guess.
4. What the *tag names themselves* mean (which register is which physical reading) came from
   finding each `VW`/`M` name sitting immediately next to its on-screen label text in the
   decompressed screen blocks (5 = status screen, 7 = advanced/PID parameters, 8 = current
   curve) — e.g. `VW25` appears immediately before the string `"UPPER"` in block 5.

## Live readings (status screen, block 5)

| Tag | Function | Address | Meaning |
|-----|----------|---------|---------|
| VW25 | FC3 (holding register) | **20** | UPPER zone temperature (×10 fixed-point) — address corrected 2026-08-18, see "Live verification" |
| VW26 | FC3 | **21** | LOWER zone temperature (×10 fixed-point) |
| VW27 | FC3 | **22** | INFRARED zone temperature (×10 fixed-point) |
| VW28 | FC3 | **23** | OUTSIDE / PROBE temperature (×10 fixed-point; external probe — see [[05-firmware-patches-applied]]) |

Addresses 24-27 also carry the same 4 readings, rounded to plain integers (no decimal) — a
lower-precision duplicate of the same data, confirmed live, not a separate set of registers.

## Control / status bits (block 5)

| Tag | Function | Address | Meaning |
|-----|----------|---------|---------|
| M151 | FC1 (coil) | 150 | Machine busy/running status — gates the STOP button *and* every curve-editing button (Save/Delete/Send curve, Advanced Parameters, BACK) in block 6, consistent with a safety interlock that disables profile editing while actively heating. Originally guessed as a simple "Start/Stop" bit from block 5 alone; block 6 context revised that. **Not yet live-confirmed** — see Start/Stop note above. |
| M172 | FC1 | 171 | VACUUM on/off — **confirmed live, bidirectionally**, 2026-08-18 |
| M173 | FC1 | 172 | COOLING (fan) on/off — **confirmed live, bidirectionally**, 2026-08-18 |

(Address column now reflects the confirmed tag-minus-1 offset from live traffic; see "Live
verification" above.)

`M151`–`M180` (30 coils total) are all confirmed present in the device table.

### Unidentified-coil sweep, 2026-08-19

Prompted by the "custom Pi/browser UI to replace the touchscreen" idea - before recommending
replacing the touchscreen, wanted to know how much of the coil range is still a blind spot.
Two-part investigation:

**Physical panel controls ruled out as the source.** The machine also has a physical Laser
guide button, Lights button, Emergency Stop, and 4 switches for the outer IR heater plates
(the always-on middle-row plates + bottom hot air have no switch - wired straight to power).
Toggled Laser guide and Lights live while watching for any coil change (same method that
confirmed Vacuum/Cooling): **no coil changed for either**, confirming they're direct hardware
I/O like the physical Start button - never touch the Modbus link at all. User confirmed the 4
IR-plate switches are simple direct power cutoffs too, so didn't bother live-testing those
individually - same expected result. **This rules out the exterior panel entirely as a source
for the unidentified coils** - whatever they are, they're internal/software-only, not tied to
any physical control on the machine.

**Static sweep of every extracted screen block for M155-M180 label context**: found two new
confirmed items, no others.

| Tag | Address | Meaning |
|-----|---------|---------|
| M152 | 151 | Upper Hot air self-tuning (PID autotune trigger) - Advanced Parameters screen, block 7 |
| M153 | 152 | Lower Hot air self-tuning (PID autotune trigger) - block 7 |
| M154 | 153 | IR self-tuning (PID autotune trigger) - block 7 |
| M174 | 173 | KEEP - **now confirmed via direct on-screen label adjacency** (block 5, status screen: literal string "KEEP" immediately followed by "M174" in the decompressed block, same reliable method that identified Vacuum/Cooling) - upgrades this from the earlier weaker string-proximity guess to solid confirmation, though still not independently live-tested with an active run. |

(The self-tuning tags appeared in the raw strings as `M152A`/`M153B`/`M154C` - the trailing
letter is almost certainly an adjacent field concatenated by the string extractor, not part of
the real tag; the base tag numbers match the already-verified device table exactly.)

**No label context found anywhere in the extracted blocks for M155-M171 (except the already-
known M157/M172) or M175-M180** - not "not yet swept," genuinely absent from every screen we
have. Combined with the physical-panel result above, the remaining ~20 unidentified coils are
either used on a screen not captured in this backup (e.g. an alarm/history/diagnostic screen),
or defined in the device table but never actually wired to anything in this project - both are
plausible and not distinguishable from static analysis alone. Not resolved further from here;
would need either capturing traffic during an actual fault/alarm condition, or accepting the
residual unknown as unlikely to be exterior-safety-relevant given the panel-control result.

## PID gains (advanced parameters screen, block 7)

Two heating zones (Upper, Lower) use **5-segment gain scheduling** by temperature range — the
touchscreen picks which segment's gains are active locally (via internal-only `SKP1-5`/`SKI1-5`/
`SKD1-5` for Upper and `XKP1-5`/`XKI1-5`/`XKD1-5` for Lower — these never appear in the Modbus
device table, so they're pure HMI-side scratch variables) and pushes only the *currently
selected* gain set to the board via these registers:

| Zone | Segment 1 (0–150°) | Segment 2 (151–180°) | Segment 3 (181–210°) | Segment 4 (211–250°) | Segment 5 (251–300°) |
|------|------|------|------|------|------|
| Upper P | VW201 | VW202 | VW203 | VW204 | VW205 |
| Upper I | VW206 | VW207 | VW208 | VW209 | VW210 |
| Upper D | VW211 | VW212 | VW213 | VW214 | VW215 |
| Lower P | VW216 | VW217 | VW218 | VW219 | VW220 |
| Lower I | VW221 | VW222 | VW223 | VW224 | VW225 |
| Lower D | VW226 | VW227 | VW228 | VW229 | VW230 |

Infrared zone has a single (non-segmented) gain set:

| Tag | Meaning |
|-----|---------|
| VW231 | IR P |
| VW232 | IR I |
| VW233 | IR D |

All of the above are FC3 (holding registers).

**Confirmed live, 2026-08-18**: navigating to the Advanced Parameters screen triggers
`FC3 read, start=200, qty=33` — i.e. `VW201` sits at Modbus address 200, the same "tag minus 1"
offset the coils use (this range does **not** use the temperature registers' "minus 5" offset —
confirms the earlier warning that offsets aren't uniform across the map). Captured values form a
coherent, physically-plausible gain-scheduling pattern: Upper P `26,31,34,34,39` (increasing with
segment), Upper I `870,641,557,525,477` (decreasing), Upper D `248,183,159,150,136` (decreasing),
Lower P `28,34,37,36,34`, Lower I `830,684,579,517,406`, Lower D `237,195,165,147,116`. Column
order (segment 1→5 left-to-right) is now considered confirmed, not just inferred from string
order — the increasing-P/decreasing-I/D shape matches real-world PID gain scheduling and would be
an unlikely coincidence if the order were wrong. IR P/I/D values weren't cleanly extracted from
this capture yet (hex parsing ran into an off-by-one while counting by hand) — worth re-pulling
precisely, but the block-level location (addresses 230-232, tag-minus-1) is solid.

## Curve / profile data (current curve screen block 8, curve editor block 6)

**Fully resolved and verified live, 2026-08-18 — this section supersedes the original
static-analysis model, which had the structure backwards.** The original guess was "9 sections
× 8 attribute-columns" (section as the outer grouping). The real layout, confirmed by editing a
live cell on the touchscreen and matching a full register dump against a photo of the actual
curve editor table, is the **transpose**: **9 attribute rows × 8 sections**, where each attribute
gets its own contiguous run of 8 registers (one per section), not each section getting a run of
8 attributes.

| Registers (= `VW` tag − 1, same offset as coils/PID) | Attribute |
|---|---|
| 100–107 | Upper Temper (target temperature) |
| 108–115 | Upper Rate |
| 116–123 | Upper Holding time |
| 124–131 | Lower Temper |
| 132–139 | Lower Rate |
| 140–147 | Lower Holding time |
| 148–155 | IR Temper |
| 156–163 | IR Rate |
| 164–171 | IR Holding time |

Within each attribute's 8-register run, index 0 = Section 1, index 7 = Section 8 (only 8 sections
exist, not 9 as the register count alone had suggested — 9 attributes × 8 sections = 72,
same total, different factorization). E.g. Section 3's Lower Temper = `124 + 2` = address 126.

**Verification**: captured a full 82-register dump of this range, then had the user edit Section
1's Upper Temper from 130→180 live and provide a photo of the actual curve editor table. Every
single one of the 72 values in the photo matches the captured register data exactly, including
Upper and Lower zones being identical to each other (this particular curve happens to use the
same profile for both) while Infrared differs, and the changed cell (130→180) landing precisely
at address 100. This is now ground truth, not inference — full confidence. **Important caveat,
added 2026-08-19 - this confirms the register MAPPING, not that reads work**: this capture was
taken from the curve editor screen (per the photo). A later investigation (see "PID / temp
compensation read reliability" below) found the curve editor/recipe screen reflects the
touchscreen's own local recipe file and never changes no matter what's written to the board - it
does not do a live PLC read at all. That means the "82-register dump" captured here was almost
certainly the WRITE the editor auto-pushes to the PLC on a live edit (MCGS's recipe "download",
confirmed elsewhere in this doc as a real FC16 write with a real captured ACK), not a genuine FC3
read response - the two look superficially similar (both are just byte-count + raw register data)
and are easy to conflate without checking the function-code byte specifically. The register
ADDRESS MAPPING above is still solid (a write payload carries the same value-to-address
correspondence a read would), but this was never actually proof that reads work, and no session
since has ever produced a confirmed genuine read response for this range - see "Curve/profile
active-read investigation" below.

### "date curve" / Download button — firmware-dump investigation, 2026-08-18

Triggered by the PC app's "Send to machine" reporting `transmitted 3/3 times, not
independently confirmed` while the touchscreen's curve editor appeared to show no change
afterward. Went back into the decompressed v5 project (`docs/raw_extracted_strings/v5/`,
produced by `tools/extract-mcgs-project.ps1` — plain zlib inflate, not a real "decrypt")
to find how the touchscreen itself implements this button, using `index.dat`'s section
directory as a map:

- **This project genuinely uses MCGS Pro's built-in Recipe (`配方`/"曲线") database
  feature** — not a fully custom script. `index.dat` lists a real `MCGS_RECIPE_曲线`
  section. Block 9 (`block_09_off171080`, the "Curve selection" popup window) contains a
  literal script using MCGS's native recipe API to list saved recipes into the picker
  table: `!RecipeSeekToPosition(`, `!RecipeGetName(`, `!RecipeMoveNext(`. Block 1 and
  block 2 hold the recipe field-name list ("Recipe name") and the Save-As dialog's
  messages ("Curve successfully saved!", "Recipe Name Can't Be Empty!", "The Current
  Curves Covered!").
- **Block 6 (the curve editor screen itself) has no visible write-loop script attached to
  the "date curve" button** — just its label, the `M151` (busy) gating condition also
  applied to every other button on this screen, and the message
  `"Parameter download is complete!"`. No `!Recipe...(` call sits next to it the way the
  Curve-selection popup's list-loading script does.
- **Reading of this: "date curve" is almost certainly MCGS's built-in Recipe
  "download/write to device" action type**, a widget property rather than custom script —
  which is why no readable script text exists for it (unlike the Curve-selection popup,
  which needed a real loop because it's iterating recipe records, not just pushing one).
  That built-in action pushes every recipe field with a configured device-tag binding
  (i.e. the `VW100-171` range) to the PLC in one shot — consistent with everything
  observed on the wire so far. It also reinforces the earlier conclusion that recipe
  *names* live purely in the touchscreen's own recipe file and are genuinely
  unreachable over Modbus.
- **Not resolved by this dump**: which Modbus function code MCGS's built-in download
  action actually issues (FC16 bulk vs. 72× FC6 single-register writes) — that logic is
  compiled into the MCGS Pro TPC runtime binary itself, not present as readable text
  anywhere in the project data. No project file distinguishes this.
- **Leading hypothesis for "nothing changed" is still a display-refresh issue, not a
  rejected write**: per the already-documented curve-poll behavior above, the touchscreen
  only re-reads `VW100-171` when the curve editor screen is *(re)loaded* — not
  continuously, and not automatically on external changes. If the touchscreen was left
  sitting on the curve editor screen (or wasn't backed out and back in) after the PC app's
  write, it would keep showing its already-loaded snapshot regardless of whether the PLC's
  registers actually changed. **Next test, zero risk**: after "Send to machine" reports
  "Sent (unconfirmed)", on the touchscreen press BACK out of the curve editor and go back
  in, then check whether the new values now show up. This isolates "did the write reach
  the PLC" from "did the screen just not refresh" without needing any further writes.

  **Tested 2026-08-18: ruled out.** Values still showed the old ones even after forcing a
  screen reload — the write itself isn't taking effect on the PLC, not a display-refresh
  issue.
- **Real root cause, identified 2026-08-18: frame duration, not protocol.** The single
  72-register FC16 frame is ~151 bytes; at 9600 baud (8N1, ~1.04ms/byte) that's ~157ms on
  the wire. The coil writes that *do* work reliably (Vacuum toggling) are only 8 bytes,
  ~8ms. The bus has near-continuous traffic, and the write path's quiet-gap check only
  guarantees the line was silent for 15ms *before* transmission starts - not for the
  remaining ~150ms of a long frame. A frame this long almost certainly gets stomped on
  mid-transmission by the touchscreen's own next poll on every attempt, corrupting it on
  the wire; the PLC's CRC check then silently drops it, which is indistinguishable from
  our side as "transmitted 3/3" (our serial write succeeded) even though nothing reached
  the board intact.

  **Fix applied**: `machine_bus.py` now splits curve writes into one FC16 frame per
  attribute row (8 registers, ~26ms each - a 6x reduction) via `_send_curve_chunked()`,
  instead of one 72-register blast.

  **Live-tested 2026-08-19: chunking alone was not enough.** Sent a small test profile
  (Upper Temper section1/2 = 167/200, Upper Rate section1 = 5, everything else 0) three
  times against a known baseline ("PS5 LF v1", all fields non-zero/distinct) and checked
  the touchscreen's **"Current Curve"** screen (`MCGS_WND` block 8, referenced in that
  screen's own data via a literal `VW101` tag - this is the screen that actually reads
  live PLC register values, unlike the curve editor/recipe screen which reflects the
  touchscreen's own local recipe file and never changes no matter what's written to the
  board). Result: only 2 of 9 attribute-row chunks (Upper and Lower Holding Time) actually
  stuck; the other 7 (including the two temperature/rate rows we deliberately changed)
  still showed the original baseline values. Confirms the root cause is real but only
  partially fixed by shorter frames alone - collisions are still dropping most chunks;
  which ones get through appears to be effectively random per attempt, not tied to
  register range.

  **Also confirmed independently, same test**: captured a genuine FC16 write-ACK on the
  wire and verified its CRC by hand - `01 10 00 64 00 08 80 10` (station 1, function 16,
  start=100, qty=8, CRC 0x1080) is a real, valid success response from the control board,
  not a self-echo of our own request (which would have byte-count+data following the
  qty field, not a CRC terminating it 8 bytes in). The board genuinely does accept and
  ACK these writes when they land cleanly - the problem is purely how often they land
  cleanly, not whether the board honors them.

  **Fix applied, 2026-08-19**: `_send_curve_chunked()` now verifies every chunk
  individually against its real expected ACK (`_build_write_registers_ack()`) and retries
  that specific chunk (up to 20 attempts) until confirmed, instead of firing a fixed number
  of times and assuming success. `WriteResult.independently_verified` is now `True` when
  every chunk was actually ACK-confirmed by the board - "Send to machine" should report
  "Sent and confirmed" rather than "unconfirmed" once this works.

  **Live-tested for real, 2026-08-19 (later the same day)**: single-pass ACK-verified sends
  are a real improvement but not reliably complete on their own - two separate genuine
  production sends (not a synthetic test) landed at 8/9 and 5/9 chunks confirmed, both
  requiring the user to notice the "Range is now partially updated" failure and click Send
  again by hand. Failed chunks were non-contiguous and different between the two attempts
  (`[100]` once, `[100, 108, 140, 164]` the other time) - not tied to a specific register or a
  specific screen (home screen both times, not a heavy-traffic screen), consistent with
  generic, variable-rate collision noise rather than anything address- or screen-specific.
  **Fix applied**: `_send_chunked_registers` now runs up to 5 full passes automatically,
  each pass only re-attempting chunks not yet confirmed (each individual attempt budget
  reduced from 20 to 8 tries, so the total per-chunk budget - 8x5=40 - is actually larger,
  but spread across passes separated by real wall-clock time instead of one long back-to-back
  burst). This automates exactly the manual "click Send again" recovery that was working in
  practice, instead of requiring the user to notice and repeat it. Progress is now shown live
  on the button ("Sending curve... 6/9 confirmed (pass 2)") instead of a static "Sending..."
  for however long the whole operation takes - a single pass alone was observed taking 2-3
  minutes with zero feedback. **Live-tested 2026-08-19, same day**: a real production send with
  the multi-pass version reported "Sent and confirmed" and the touchscreen's Current Curve screen
  showed values matching the sent profile exactly (once the separate temperature-scaling bug below
  was also fixed) - no manual re-click needed this time.

## Fan / vacuum timing registers (advanced parameters screen) — resolved 2026-08-18

Confirmed by reading exact on-screen values on the Advanced Parameters screen and matching them
directly against a live capture (same technique as the temperature-register offset). Same "tag
minus 1" offset as coils/PID — not the temperature registers' "minus 5":

| Tag | Address | Meaning | Value at test time |
|-----|---------|---------|---|
| VW173 | 172 | Vacuum time | 45 (seconds) |
| VW174 | 173 | Fan stop temperature | 450 = 45.0°C (×10 fixed-point) |
| VW175 | 174 | Fan stop delay | 150 (seconds) |
| VW176 | 175 | Advance warning time | 2 (seconds) |

Addresses 176-179 (within the same `start=172, qty=10` poll) all read `0xFFFF` — almost
certainly an "unused/not configured" sentinel rather than real data, not identified further.
Address 180 reads `20`, address 181 reads `0` — neither identified in the tag map yet; low
priority since they don't correspond to any labeled field found so far.

## PID / temp compensation read reliability — 2026-08-19

After wiring up PID gains (write side, via the same chunked ACK-verified path as curve writes -
`machine_bus.py` chunks any write over 10 registers, so the 33-register PID block gets the same
collision protection curve writes needed), live testing found the PID *read* side wasn't
populating even after forcing a fresh Advanced Parameters screen load (back out, back in - the
fix that works for curve data). Fan/vacuum timing (`start=172, qty=10`) read through fine on the
same screen in the same session, so this isn't a blanket "Advanced Parameters doesn't poll"
problem - just PID specifically.

Went back into the firmware dump (block_07, same screen) and found a genuinely new field:
**"Temperature compensation"** - three offset inputs (Up/IR/Low, range shown as `max:150 min:-150`
- a correction offset, not an absolute temperature) sitting on this same screen, with no tag name
visible adjacent to any of the three in the extracted strings (unlike every other field on this
screen, which all have a clean `VW*` label right next to them). Leading guess: the real on-wire
PID poll isn't a clean 33-register read in isolation - it's likely one combined read that also
covers temp compensation, meaning our exact `count==33` match never fires because the true count
is something else (36, if compensation is 3 contiguous registers appended right after IR D at
232). Added a `count == PID_COUNT + 3` (36) branch as a low-cost bet on this specific guess -
**never confirmed live**, just wired in case it happens to be right. A diagnostic catch-all
(logging any unrecognized holding-register shape) was added and run for this investigation but
found nothing else besides the known `count==1` false-positive noise pattern already documented
elsewhere in this file - the real poll simply never fired during any capture window, even across
multiple fresh screen loads. Removed the diagnostic afterward to keep the deployed service's
logs clean; the `machine_bus.py` git history (or this doc) has the exact code if it's worth
re-adding for a future attempt.

**Decision, 2026-08-19**: not chasing this further for now - same category of limitation as
curve reads (opportunistic, unpredictable trigger timing). The **write side is unaffected** and
should work normally (same proven ACK-verified mechanism as everything else) - you can type in
PID values and Send to machine without ever needing a successful machine-read first, same as
manually entering a curve profile.

**Also found in the same firmware block**: a literal on-screen warning string -
`"Please correct parameters, the machine is running Do not modify parameters!"` - the stock
touchscreen itself refuses parameter edits while `M151`/Running is set. Worth treating as a real
constraint for the PC/web app too: avoid sending PID/fan-timing/curve changes while the machine
is actively running, matching the OEM's own built-in restriction, even though nothing currently
enforces this in code.

## Active reads - correcting the passive-only limitation, 2026-08-19

Everything above this point only ever learned values by passively overhearing the touchscreen's
own traffic. User is considering permanently removing the touchscreen's power now that writes are
reliable without it (see machine_bus.py's write path) - but that would leave every *read* frozen
forever, since passive listening would have nothing left to overhear. Added `MachineBus` methods
(`read_temps_active()`, `read_coils_active()`, `read_curve_active()`, `read_pid_active()`,
`read_fan_timing_active()`) that actively request data directly from the board, using the same
proven collision-avoidance mechanism as writes - see "active reads" in `machine_bus.py`.

**Two real bugs found and fixed during live testing, both worth understanding**:

1. **Modbus RTU read responses carry no address information** - only function code + byte count
   + data, unlike write ACKs (which echo the start address). A request for a small/arbitrary
   register count (e.g. qty=4 for just the 4 temps we actually need) can't be told apart from
   some *other* unrelated read of the same byte count that's also on the bus. Confirmed live:
   requesting qty=4 at the temp registers returned values matching the unrelated "24-27 rounded
   duplicate" poll instead (both are byte_count=8) - wrong data, not a crash, so easy to miss.
   Fixed by matching the touchscreen's own exact, already-proven poll shapes instead of the
   smaller count actually needed: qty=8 for temps (not 4), qty=29 for coils (not 24), qty=10 for
   fan timing (not 4) - see the docstrings on each `read_*_active()` method.

2. **Concurrent active operations corrupt each other's pause/resume state.** The
   `_pause_event`/`_paused_ack`/`_resume_event` signaling was only ever exercised by one write at
   a time (naturally serialized - one GUI button click at a time). Adding a continuous background
   active-poll loop (re-requesting temps/coils every ~1s to keep the live graph/status fresh)
   broke that assumption: it can now race against an explicit user-triggered read/write, both
   trying to pause/resume the same background reader concurrently. Confirmed live - reads failed
   almost 100% of the time until fixed by adding `_active_request_lock`, held for an entire
   transmit-and-retry sequence (all attempts), making "one active bus operation at a time" true
   again regardless of how many callers exist.

**Also resolved as a side effect**: the PID read-reliability mystery from the section above this
one. A live capture during an Advanced Parameters screen load (2026-08-19, later in the same day)
caught the real on-wire poll directly - `start=200, qty=33`, exactly the original assumption, data
matching the historically-documented PID values exactly (Upper P 26,31,34,34,39, ...). The earlier
`count == PID_COUNT + 3` (36) speculative branch was never triggered and can be considered ruled
out; the true difficulty was just how rarely that specific poll occurs, same as curve data.

**Tested live, working reliably under normal (non-burst) conditions**: 5/5 active temp reads
succeeded (4 on the first attempt, response times 0.6-3.0s), coil read succeeded, values internally
consistent and matched the touchscreen's own display. **Under heavy contention** (confirmed live:
sitting on the Advanced Parameters screen, which generates its own burst of temps+PID+fan-timing
polls on top of the normal continuous traffic) active reads can still fail outright - this is
expected given the collision-probability reasoning that's driven every fix this session, not a new
problem, and irrelevant once the touchscreen is actually removed (no other traffic left to
collide with at all).

## Curve read false positives — confirmed live and mitigated, 2026-08-19

After the coil-write fixes below, a "curve read" (via the passive listener, status showed "Curve
data: 1 received") displayed values in the Profile Editor table that did NOT match what the
touchscreen's Current Curve screen showed at the same moment (independently verified: the machine
had PS5 APU REFLOW loaded - Temper 130-282, Rate 2-3, Hold time 48-56 - but the app displayed Rate
and Hold-time rows both reading ~290, physically impossible for real curve data). This confirms,
rather than contradicts, the "likely write-only" conclusion above: Modbus RTU responses carry no
address information, this range's byte-count (144/164 bytes) is apparently not unique on this very
busy bus, and something else's real response got mistaken for curve data purely by matching size.

**Mitigation applied** (does not fully solve the underlying ambiguity - that's not solvable without
address information the protocol doesn't provide): `_update_curve_locked()` now rejects a capture
outright if any Rate value exceeds 30 or any Hold-time value exceeds 250 - generous bounds around
real captured data (Rate always 2-3, Hold time always 24-56 across all 14 real profiles), which
catches this exact false positive (and should catch most similarly-shaped ones) without risking
false rejection of genuine profiles. Applies to both the passive listener and `read_curve_active()`
- a rejected capture is now treated as "no valid read", not silently displayed as if real.

## Coil-toggle inconsistency (Start/Stop/Vacuum/Cooling/Keep) — found and fixed, 2026-08-19

User-reported: "sometimes pressing cooling starts it instantly, sometimes I have to press it 2 or
3 times" - same for Start/Stop/Vacuum/Keep. Two separate bugs, both in `machine_bus.py`:

1. **Stale local cache on toggle buttons.** `write_coil()` never updated `_latest_coils` on a
   confirmed write - it relied on the next passive/active poll to refresh (up to ~1s away, longer
   if the touchscreen isn't currently polling coils). Vacuum/Cooling/Keep are toggle buttons -
   they read `get_live_reading()` to decide which direction to flip. A second click landing inside
   that stale window flipped FROM the pre-write state, silently undoing a write that had actually
   already succeeded, with no error shown. **Fixed**: `write_coil()` now updates the cache
   immediately on a confirmed ACK, since the ACK already IS the board's own confirmation.

2. **Genuine write failures under contention.** Confirmed live: coil writes really were exhausting
   their old single-pass 20-attempt budget outright ("never saw a confirming ACK"), same
   collision-based root cause as the curve/PID chunked-write reliability issues above, just
   smaller frames. **Fixed**: `write_coil()` now retries across up to 3 passes of 15 attempts each
   (45 total, up from 20), with a real 1s pause between failed passes - same reasoning as the
   chunked-register multi-pass fix (spreading attempts across real wall-clock gaps succeeds more
   often than the same count crammed into one continuous burst). Automates the manual "press it
   again" recovery instead of requiring it.

## Curve/profile temperature scaling bug — found and fixed, 2026-08-19

While live-testing the multi-pass write retry above, a real production send ("Sent and
confirmed") produced a curve on the touchscreen's Current Curve screen where Rate and Holding
time rows matched the sent profile exactly, but **all three Temper rows (Upper, Lower, IR) read 0
across every section** - not a display glitch, the machine's actual target temperatures were 0.

**Root cause**: `TEMP_ROWS`/`TEMP_ATTRS` scaling logic (in `main_window.py`, `app.js`, and
`profile_import.py` - three separate places, same mistake) applied a x10 multiplier to Temper
values before sending, on the assumption curve registers use the same fixed-point scale as
PID/temp-compensation/live readings. They don't - this contradicted the "Curve / profile data"
section's own ground truth above (130->180 landing at address 100 as literally 180, not 1800), it
just hadn't been cross-checked against this specific code path until now. Sending 1200 instead of
120 for a target temperature passed the write-ACK (which only confirms the write transaction
happened, not that the value passed the board's own application-level sanity check) and was then
silently reset to 0 by the board - exactly matching the symptom, and exactly the three rows
(chunks 1, 4, 7 of 9) the bug touched, with the 6 untouched rows (Rate, Holding time) landing
correctly.

**Fixed**: scaling removed from `main_window.py` (`_populate_table`/`_read_table`, `TEMP_ROWS`
constant deleted), `app.js` (`readTable`/`populateTable`, `TEMP_ROWS` constant deleted), and
`profile_import.py` (`_parse_section_values`, `TEMP_ATTRS` constant deleted) - curve values are
now raw integers end-to-end, matching the register format.

**Also fixed**: all 14 profiles in `pc-app/profiles/` had this corruption baked in from the
original spreadsheet import (every one showed clearly-impossible values like 2540-2840 for a
"degrees C" field before correction). One-time script divided upper_temp/lower_temp/ir_temp by 10
across every saved profile (safe - every affected value was, by construction, an exact multiple of
10). Verified against the touchscreen photo after the fix: NINTENDO SWITCH SOC REBALL now sends
Upper Temper 120/152/176/194/212/230/244/254 and IR Temper starting at 88, both matching the
touchscreen exactly. **Live-tested and confirmed working end-to-end 2026-08-19.**

## Curve/profile active-read investigation — likely write-only, 2026-08-19

`read_curve_active()` (`machine_bus.py`) was built the same way as every other active read in this
app (see "Active reads" above), and initially failed 100% of the time ("No response from the board
after repeated attempts"). Unlike every other active-read bug this session, this one was never
actually a bug in the app - it was chased hard, ruling out every plausible explanation in turn,
before landing on a much bigger realization (see the caveat added to the "Curve / profile data"
section's Verification note above): the original evidence that this range was ever readable at all
was itself almost certainly a misattributed write, not a read.

**Everything tried, all on real hardware, all unsuccessful**:
- One-shot FC3 read, qty=72 (the original, "obviously correct" request shape).
- Chunked FC3 reads, 8 registers/attribute-row (9 requests) - ruled out frame-duration/timeout
  margin as the cause, since even a chunk the same size as the already-reliable temps read (which
  works fine) got silence here.
- Touchscreen screen state: curve editor, home screen, and specifically the **"Current Curve"**
  screen - the one screen the firmware dump suggested does a genuine live PLC read via a literal
  `VW101` tag. No difference - and the app's own passive listener (which recognizes a valid
  response purely by byte count, independent of the active-read code entirely) never observed a
  single curve-range response either, the whole time, from anyone, on any screen.
- A full machine power cycle - no difference.
- FC4 (read input registers) instead of FC3, per chunk - a genuine but ungrounded guess, no
  evidence for it in the firmware dump. No difference.
- FC3, qty=82 instead of 72 (the *other* documented real byte-shape for this range, from the
  original verification capture) - no difference.

**Meanwhile, unaffected and independently confirmed working**: writes to this exact address range
(real captured FC16 ACKs, see the "date curve" section above and `write_registers()`), and reads of
every *other* register range on this same bus (temps, PID, fan-timing, coils) via the identical
active-read mechanism.

**Conclusion**: this register range is very likely write-only as exposed over Modbus on this board
- the PLC accepts and honors FC16 writes here (confirmed), but nothing tried produces a read
response, and the original "verified live" evidence that reads ever worked was itself very likely
a misread write, not a read (see the Verification caveat above). Not chasing this further absent a
new lead - if a future firmware dump pass or a live capture ever catches a *genuine* read response
for this range (address byte + function code 3 or 4 + a full valid CRC, not just a byte-count
coincidence), that would immediately reopen this. Until then: **the write path is the one that
matters and it already works reliably** - `read_curve_active()` is safe to leave wired up (it
degrades to a clear "No response" error, not a crash or bad data) but shouldn't be relied on, and
the app's "currently loaded profile" state should be tracked by what was last successfully sent,
not by reading it back from the board.

## Not yet resolved

- Addresses 176 (val 20) and 181 (val 0, consistently) within the advanced-parameters poll range
  — not identified, likely low-value/unused fields.
- Coils M155-M171 (except the already-known M157/M172) and M175-M180 (~20 coils) — see the
  "Unidentified-coil sweep" above. Ruled out as tied to any physical panel control; no on-screen
  label found in any extracted block either. Either used on a screen not in this backup (alarm/
  history/diagnostic), or defined but unused. Not resolved further without live fault-condition
  capture.
- Coil 157 — tracked identically with `M151` across a full 2-minute run/stop cycle with no
  divergence observed; effectively a duplicate status bit for practical purposes, not resolved
  to an independently distinct meaning.

## Remaining verification work

The adapter is wired in and `tools/capture-modbus.ps1` (read-only, passive) is working — see
"Live verification" above for what's confirmed so far. Still to do:
- Resolve the register-offset ambiguity for `VW25-28`.
- Carefully test Start/Stop (`M151` candidate) — plan this deliberately rather than a casual
  toggle, since it could begin an actual heating cycle.
- Capture a full run (or at least a curve-editing session) to observe the `VW101-172` curve data
  range and the `VW201-233` PID registers actually changing, to verify those the same way
  Vacuum/Cooling were verified.
- Identify register `181` (seen polled, returns `0`, not yet in the tag map).
