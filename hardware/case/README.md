# Case

3D-printed enclosure for the InkTime frame.

| File | What it is |
|---|---|
| `Inktime.stl` | Mesh export, ready to slice |
| `Inktime.f3d` | Autodesk Fusion 360 source document |
| `case_features.png` | Rendered features sheet (iso, cutaway, two sections) |

## Dimensions

**152.5 × 202.5 × 17.0 mm.** The tall dimension is the panel length: a 7.3"
display is 163 mm on the diagonal, so the case carries it with a margin for the
bezel lip.

**2,838 triangles, watertight** (0 open boundary edges), enclosing **131.9 cm³**
of material. Roughly 165 g of PLA at 1.24 g/cm³ before slicing adds walls and infill.

## Structure

The part is an **open tray**, not a closed box:

- **3 mm base** at z ≈ 3, with the outer floor at z ≈ 0.2
- **End walls** rising to z ≈ 10.4
- **Internal ribs and bosses** along the length, at a repeating pitch — visible in
  the X-section, these are the standoffs that hold the PCB and stop the base flexing
- **Bezel lip** at z = 14.2 → 17.2, the raised rim that frames the panel
- Two small slots in the top face near the midpoint

Height bands in the model: 3.2 mm (base top), 6.0 mm, 10.4 mm (wall top),
13.2 mm, 14.2 mm (bezel start), 17.2 mm (bezel top).

## Source

The Fusion document carries a `Toolpath` asset, so there is a CAM setup saved
alongside the model. `Inktime.f3d` is the authoritative source — edit that and
re-export the STL rather than editing the mesh.
