# Hardware

PCB source and bill of materials for the InkTime frame.

| File | What it is |
|---|---|
| `ESP32-S3-InkDisplay-v1.0.epro2` | EasyEDA Pro project (opens in EasyEDA Pro) |
| `project2.json` | Project manifest, extracted from the `.epro2` |
| `bom.csv` | Per-designator bill of materials, 26 line items |

## Board

**ESP32-S3-InkDisplay v1.0** — 100.0 × 33.0 mm, 2-layer.

Originally an open-source design by `Donald` (the silkscreen credits him);
modified by HXJ08. The `.epro2` dated 2026-10-02 is the modified revision.

**Power path:**

```
LiPo ──► TP4056 (charge) ──► VBAT ──► MIC5219-3.3 LDO ──► 3V3 ──► ESP32-S3
                                    └──► 100k/100k divider ──► ADC (battery sense)
```

**Display:** 8-pin SPI header (BUSY / CS / DC / RST / DIN / SCK / 3V3 / GND) to the
GDEM075F52 panel.

**USB:** D+/D− routed as a declared differential pair (DP1/DP2).

## BOM summary

26 line items, 8 of which are fitted capacitors (C1-C7) and 4 are tactile
switches. Consolidated:

| Qty | Part | Value | Designators |
|---|---|---|---|
| 4 | `RC1608F103CS` | 10 kΩ | R1, R4, R5, R6 |
| 4 | `KH-6X6X5H-STM` | tactile switch | SW1-SW4 |
| 3 | TO-56 socket, 2.54 mm 2P | | H2, H5, H6 |
| 2 | `CL10A475KO8NNNC` | 4.7 µF | C1, C2 |
| 2 | `CL10C470JB8NNNC` | 47 pF | C5, C6 |
| 2 | `0603WAF220JT5E` | 22 Ω | R2, R3 |
| 1 | `CL10B104KB8NNNC` | 100 nF | C3 |
| 1 | `CL10A105KB8NNNC` | 1 µF | C4 |
| 1 | `ESP32-S3-WROOM-1(N8R8)` | | U1 |
| 1 | `MIC5219-3.3YM5` | | U2 |
| 1 | `TP4056` | | U3 |
| 1 | `PZ254V-11-04P` | 4P header | H1 |
| 1 | `PZ254V-11-01P` | 1P header | H3 |
| 1 | `HC-PZ254-11.5L-1X8PZ` | 8P header | H4 |
| 1 | capacitor, 0603 | C7 | value not recorded in the project |

## Notes before ordering

- **Two footprints are marked `DNI`** on the silkscreen. If they are deliberate, the
  assembler must be told to skip them.
- **No DRC result is stored in the project file.** The design rules are present
  (0.127 mm min track, 0.15 mm gap, 0.30 mm board-edge clearance) but a clean pass is
  not asserted — re-run DRC in EasyEDA first.
- **C7 has no recorded value or part number**, and **R5/R6** were generic `电阻`
  symbols in the schematic. The BOM resolves R5/R6 to the same 10 kΩ 0603 family as
  R1/R4 by reading the value attribute, and leaves C7 blank rather than guessing.
- **The BOM is derived from the PCB file**, not from an exported design BOM — the
  project ships no `.csv`. Values were decoded from the manufacturer part numbers
  (e.g. `475` → 4.7 µF) and cross-checked against the board's function.
