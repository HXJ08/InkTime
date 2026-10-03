# Hardware

PCB source and bill of materials for the InkTime frame.

| File | What it is |
|---|---|
| `ESP32-S3-InkDisplay-v1.0.epro2` | EasyEDA Pro project (opens in EasyEDA Pro) |
| `project2.json` | Project manifest, extracted from the `.epro2` |
| `bom.csv` | Bill of materials, exported from EasyEDA — 17 line items, 24 components |

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

## BOM

Sourced from EasyEDA's own export (`BOM_V1_PCB1_2026-10-03.xlsx`), so the LCSC
supplier part numbers are included and the values are authoritative.

| Designators | Qty | Value | Manufacturer Part | LCSC |
|---|---|---|---|---|
| C1 | 1 | 4.7 µF | `CL10A475KO8NNNC` | C19666 |
| C2 | 1 | 10 µF | `CL10A106KP8NNNC` | C19702 |
| C3 | 1 | 100 nF | `CL10B104KB8NNNC` | C1591 |
| C4 | 1 | 1 µF | `CL10A105KB8NNNC` | C15849 |
| C5, C6 | 2 | 47 pF | `CL10C470JB8NNNC` | C1671 |
| C7 | 1 | 100 nF | — | — |
| R1, R4 | 2 | 10 kΩ | `RC1608F103CS` | C17441167 |
| R2, R3 | 2 | 22 Ω | `0603WAF220JT5E` | C23345 |
| R5, R6 | 2 | 100 kΩ | — | — |
| SW1-SW4 | 4 | — | `KH-6X6X5H-STM` | C2837531 |
| H1 | 1 | — | `PZ254V-11-04P` | C2691448 |
| H2 | 1 | — | `PZ254V-11-02P` | C492401 |
| H3 | 1 | — | `PZ254V-11-01P` | C492400 |
| H4 | 1 | — | `HC-PZ254-11.5L-1x8PZ` | C27985192 |
| U1 | 1 | — | `ESP32-S3-WROOM-1-N8R8` | C2913201 |
| U2 | 1 | — | `MIC5219-3.3YM5-TR` | C29613 |
| U3 | 1 | — | `TP4056` | — |

**24 components total.** C7, R5/R6 and U3 have values but no supplier part number
recorded — they are generic and need picking at order time.

## Notes before ordering

- **Two footprints are marked `DNI`** on the silkscreen. If they are deliberate, the
  assembler must be told to skip them.
- **No DRC result is stored in the project file.** The design rules are present
  (0.127 mm min track, 0.15 mm gap, 0.30 mm board-edge clearance) but a clean pass is
  not asserted — re-run DRC in EasyEDA first.
- **`bom.csv` is EasyEDA's export, not a derivation.** An earlier version of this file
  was reconstructed by parsing the PCB geometry and got three parts wrong: C2 as 4.7 µF
  instead of 10 µF, C7 as blank instead of 100 nF, and R5/R6 as 10 kΩ instead of 100 kΩ.
  The schematic knows values the PCB file does not, which is why the export supersedes it.
