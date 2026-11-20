## Overview & Architecture

The kerning pipeline splits into two distinct phases so that expensive geometric passes run once per font/tool configuration, while pairwise layout stays fast at text-generation time:

1. **Font-Load / Tool-Configuration Preparation (Once per font + cutter diameter):** Transforms raw centerline profile envelopes into physical ink/cut envelopes, applies optical overhang bleed, bridges internal vertical gaps, and assigns character spacing metadata.
2. **Pairwise Advance Solver (On demand, memoized per pair):** Computes the horizontal advance between adjacent characters using a two-pointer overlap sweep and binary search, falling back to bounding-box rules for non-overlapping marks or symbol fonts.

---

## Phase 1: Font Metadata & Parameter Calibration

### 1.1 Font Classification (`standard` vs. `symbol`)

Add or infer a font-level flag (`is_symbol_font: boolean`):

* **Standard Text Fonts:** Enable character/punctuation spacing classes and asymmetric tightening rules.
* **Symbol Fonts (e.g., Wingdings, Dingbats):** Bypass all punctuation/class overrides. Treat every glyph as a `STANDARD` class with uniform target spacing so decorative icons, arrows, and boxes in slots like `.` or `'` aren't crushed against adjacent symbols.

### 1.2 Global Font Metrics & Constants

Derive baseline tuning constants proportionally from the font's reference geometry (using `H` or `I` for `cap_height`, and `n` or `x` for `x_height`) and the active cutter diameter $w_{\text{tool}}$ ($r = w_{\text{tool}} / 2$):

* **Target Optical Gap ($\bar{g}_{\text{target}}$):** Base desired average channel width between parallel vertical stems. Typically $0.16 \times \text{cap\_height}$ to $0.22 \times \text{cap\_height}$ (adjustable via a user "tracking/looseness" multiplier).
* **Channel Saturation Cap ($g_{\text{cap}}$):** Maximum gap width that contributes full perceptual weight before open counters saturate. Set to $2.2 \times \bar{g}_{\text{target}}$.
* **Hard Collision Floor ($g_{\text{hard}}$):** Absolute minimum physical whitespace permitted at the closest pinch point after tool offset. Set to $0.35 \times \bar{g}_{\text{target}}$ (with a floor of at least $0.5 \times w_{\text{tool}}$ so adjacent cuts never merge).
* **Optical Bleed Radius ($r_{\text{bleed}}$):** Vertical perceptual influence distance for overhangs (like `T` over `a`). Set to $0.12 \times \text{cap\_height}$.

### 1.3 Character Spacing Class Assignment

For non-symbol fonts, classify each character key into one of five spacing classes at load time:

| Class | Characters | Target Multiplier ($\mu$) | Special Rules |
| --- | --- | --- | --- |
| `STANDARD` | `A–Z`, `a–z`, `0–9`, `/`, `\`, `&`, etc. | $1.00$ | Full fluid solver + bleed + hard floor. |
| `PUNCT_BASE` | `.` `,` `;` `:` | $0.40$ | Tightens baseline marks under overhangs (`T.`, `P,`). |
| `PUNCT_HIGH` | `'` `"` `’` `“` `”` | $0.55$ | Uses zero-overlap fallback when paired with short glyphs (`a'`). |
| `DASH` | `-` `–` `—` | $0.80$ | Keeps hyphenated compounds visually bound. |
| `DELIMITER` | `(` `)` `[` `]` `{` `}` | $1.10$ | Elevates $g_{\text{hard}}$ by $1.25\times$ to prevent ascender/descender pinches. |

*(For symbol fonts, assign every glyph `STANDARD` with $\mu = 1.00$.)*

---

## Phase 2: Envelope Preparation Pipeline (Per Glyph)

Run the following four-stage pipeline on every glyph's raw `(x, y)` interval list `[y_low, y_high, x_left, x_right]` whenever a font is loaded or the cutter radius $r$ changes:

### Step 2.1: Filter & Sort Active Intervals

Discard any empty intervals from the raw 32-bucket catalog and ensure remaining intervals are sorted ascending by `y_low`.

### Step 2.2: Bridge Internal Vertical Gaps

Multi-stroke glyphs (`i`, `j`, `:`, `=`, `!`) contain empty vertical gaps between tool-up jumps that would otherwise leak artificial "fluid" into the channel:

* Inspect consecutive active intervals $k$ and $k+1$.
* If the vertical gap $y_{\text{low}, k+1} - y_{\text{high}, k}$ is less than $0.25 \times \text{cap\_height}$, insert a synthetic bridge interval spanning $[y_{\text{high}, k},\; y_{\text{low}, k+1}]$ whose `x_left` and `x_right` linearly interpolate (or take the conservative outer min/max) of the two bounding intervals.

### Step 2.3: 2D Circular Cutter Offset (Minkowski Sum)

Expand the centerline envelope to reflect the physical width and height of the round cutter of radius $r$:

1. **Construct New Y-Boundaries:** Collect all existing `y_low` and `y_high` boundaries. Prepend 3 subdivisions below the lowest stroke (`y_min - r`, `y_min - 0.75r`, `y_min - 0.4r`) and append 3 subdivisions above the highest stroke (`y_max + 0.4r`, `y_max + 0.75r`, `y_max + r`) to model the semicircular tool caps.
2. **Dilate Each New Interval $[y_a, y_b]$:** For every source interval $j$ with span $[y_{\text{low}, j}, y_{\text{high}, j}]$, compute the minimum vertical distance from $[y_a, y_b]$ to $[y_{\text{low}, j}, y_{\text{high}, j}]$:

$$d = \max\big(0,\; y_{\text{low}, j} - y_b,\; y_a - y_{\text{high}, j}\big)$$



For all source intervals where $d \le r$, compute the horizontal reach $\Delta x = \sqrt{r^2 - d^2}$ and set:

$$E_R^{\text{tool}} = \max(x_{\text{right}, j} + \Delta x), \qquad E_L^{\text{tool}} = \min(x_{\text{left}, j} - \Delta x)$$



### Step 2.4: Optical Vertical Bleed (Overhang Shadow)

Generate a secondary **optical envelope** $E^{\text{opt}}$ from $E^{\text{tool}}$ so horizontal overhangs (`T`, `P`, `7`) cast a soft shadow downward/upward into non-overlapping rows:

* Expand the interval grid by $r_{\text{bleed}}$ above and below (2–3 subdivisions).
* For each interval, attenuate neighboring intervals within vertical distance $d \le r_{\text{bleed}}$ using a linear taper:

$$E_R^{\text{opt}} = \max_{d \le r_{\text{bleed}}} \left( E_R^{\text{tool}}[j] - d \cdot \tan\theta \right)$$



*(where $\tan\theta \approx 0.5$ controls how steeply an overhang's visual influence slopes inward).*
* Keep **both** $E^{\text{tool}}$ (exact physical boundary) and $E^{\text{opt}}$ (perceptual boundary) cached on the prepared glyph object.

---

## Phase 3: Pairwise Kerning & Advance Solver

Maintain a hash map `pair_cache[(char_L, char_R)] -> advance` cleared only when the font or tool width changes. When layout encounters an adjacent pair `(L, R)`:

### Step 3.1: Space Character Handling

If `L` or `R` is the space character (`' '`):

* **Simple Mode:** Return a fixed advance of $2.5 \times \bar{g}_{\text{target}}$.
* **Optical Space Mode (Recommended):** Treat `' '` as a virtual glyph spanning `[baseline, x_height]` with zero width (`x_left = 0, x_right = 0`), and solve `(L, ' ')` and `(' ', R)` using a target gap of $1.25 \times \bar{g}_{\text{target}}$ each. Between two vertical stems, this sums to $2.5 \times \bar{g}_{\text{target}}$, while naturally tightening spaces next to open letters like `L` or `T`.

### Step 3.2: Determine Pair Target & Floor

Select the active target multiplier $\mu_{\text{pair}} = \min(\mu_L, \mu_R)$ (for symbol fonts, $\mu_L = \mu_R = 1.0$, so $\mu_{\text{pair}} = 1.0$ always):

* $\bar{g}_{\text{pair}} = \mu_{\text{pair}} \cdot \bar{g}_{\text{target}}$
* $g_{\text{cap, pair}} = 2.2 \cdot \bar{g}_{\text{pair}}$
* $g_{\text{floor, pair}} = g_{\text{hard}}$ (multiplied by $1.25$ if either glyph is `DELIMITER`)

### Step 3.3: Two-Pointer Overlap Sweep

Walk the sorted intervals of $L$ (right profile) and $R$ (left profile) simultaneously using two pointers across their overlapping vertical range $[y_{\text{start}}, y_{\text{end}}]$:

1. **Physical Penetration ($s_{\text{phys}}$):** Sweep $E_R^{\text{tool}}(L)$ against $E_L^{\text{tool}}(R)$ to record the maximum horizontal overlap $s_{\max} = \max_k (x_{R, L}[k] - x_{L, R}[k])$ across all physical overlapping sub-intervals.
2. **Optical Channel Profile:** Sweep $E_R^{\text{opt}}(L)$ against $E_L^{\text{opt}}(R)$ to build a compact list of overlapping sub-intervals, each storing `(height_delta, s_opt)` where $s_{\text{opt}} = x_{R, L}^{\text{opt}} - x_{L, R}^{\text{opt}}$.

### Step 3.4: Zero-Overlap Fallback (`a'`, `L"`, Disjoint Symbols)

Check total optical overlap height $H_{\text{overlap}} = \sum \text{height\_delta}$. If $H_{\text{overlap}} \le 0$:

* Do not run the fluid integral.
* Compute advance directly from the glyphs' tool-offset bounding boxes:

$$A = \text{bbox\_right}_{\text{tool}}(L) - \text{bbox\_left}_{\text{tool}}(R) + (0.25 \cdot \bar{g}_{\text{pair}})$$


* *(Optional refinement for `PUNCT_HIGH` following `STANDARD` in non-symbol fonts: allow a slight negative margin of $-0.05 \cdot \bar{g}_{\text{target}}$ if $L$'s upper-right quadrant is open, such as `L'` or `A'`.)*

### Step 3.5: Binary Search for Fluid Equilibrium

When $H_{\text{overlap}} > 0$, solve for the horizontal advance $A$:

1. **Set Search Bounds:**
* Lower bound (collision guardrail): $A_{\min} = s_{\max} + g_{\text{floor, pair}}$
* Upper bound: $A_{\max} = s_{\max} + g_{\text{cap, pair}}$


2. **Evaluate Fluid Function $F(A)$:** For any candidate advance $A$, compute the saturated average channel width across the optical sub-intervals:

$$F(A) = \frac{1}{H_{\text{overlap}}} \sum_k \Delta y_k \cdot g_{\text{cap, pair}} \left( 1 - \exp\left( -\frac{\max(0,\; A - s_{\text{opt}, k})}{g_{\text{cap, pair}}} \right) \right)$$


3. **Iterate:**
* First check $F(A_{\min})$. If $F(A_{\min}) \ge \bar{g}_{\text{pair}}$ (as in pinch pairs like `L4`), immediately accept $A = A_{\min}$.
* Otherwise, run **8 iterations of binary search** on $[A_{\min}, A_{\max}]$ until $F(A) \approx \bar{g}_{\text{pair}}$.


4. **Cache & Return:** Store $A$ in `pair_cache[(L, R)]` and advance the cursor by $A$.

---

## Phase 4: Verification & Calibration Checklist

Before deploying across all fonts, validate the solver against a short diagnostic test string containing known stress pairs:

* **Stem Rhythm:** `HHnnHn` (verify identical visual spacing between all stems)
* **Diagonal Pinch vs. Air:** `AV VA AA VV L4 T7` (verify `AV` tightens without touching, and `L4` stops cleanly at $g_{\text{hard}}$)
* **Overhangs & Open Counters:** `Ta To Pe Va co ce` (verify `a` does not collide vertically with `T`'s crossbar, and `c` doesn't swallow the next letter)
* **Punctuation & Non-Overlap:** `T. P, a' L" "A" (f) 3.14` (verify baseline dots tuck in and high quotes don't trigger division-by-zero)
* **Symbol Font Isolation:** Render the same characters in a Wingdings/symbol font and verify uniform spacing without punctuation tightening.