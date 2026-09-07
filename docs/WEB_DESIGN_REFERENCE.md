# Design reference — color and shape language

A neutral-first design concept: crisp white and grey in light mode, deep midnight blue in dark mode. Neutrals carry almost the entire interface; a single blue accent is used sparingly to mark what matters. No gradients, no glassmorphism, minimal elevation.

---

## Concept

The palette should read as modern and clean — closer to a well-built SaaS dashboard than an archival paper catalog. Neutrals are true cool greys (not warmed toward paper or wood), so the interface reads crisp and digital-native rather than vintage.

Rules to live by:
- **Neutrals do the work.** At least 90% of any screen's surface, text, and border color should come from the neutral scale. Color is the exception, not the rule.
- **One accent, used rarely.** The blue accent marks the single most important actionable or active element on a screen — never more than one or two uses per view.
- **No pure black, no pure white background.** Every neutral, in both modes, is a step off true black/white (though light-mode surfaces may use pure white for cards against a light grey page background).
- **Flat fills only.** No gradients anywhere except as a last-resort, single continuous-property use (and even then, avoid by default).
- **Color is never the only signal.** Status/state differences (verified, warning, error) pair color with a label or icon, never color alone.

---

## Color system

### Light mode — "crisp"

| Token | Hex | Use |
|---|---|---|
| Background | `#F4F6F9` | Page background — cool light grey |
| Surface | `#FFFFFF` | Cards, panels, rows |
| Surface (raised) | `#FFFFFF` | Modals, popovers, top-most layer |
| Border / hairline | `#E1E7EF` | Rules, outlines, dividers |
| Border (strong) | `#C3CDDA` | Emphasized dividers, focus outlines |
| Text primary | `#131B2C` | Headings, primary body text |
| Text secondary | `#56637A` | Captions, labels, secondary text |
| Text muted | `#8B96A8` | Placeholder text, disabled state |
| Accent (blue) | `#2359D6` | Primary actions, active state, current selection |
| Accent, on pale fill | `#1A45AC` | Text sitting on the pale accent fill |
| Accent fill (pale) | `#DEE9FC` | Selected background, active chip |
| Success | `#1E8A5B` | Confirmed / positive state |
| Warning | `#B4790E` | Needs attention |
| Error | `#C4362A` | Missing data, failed state, destructive actions |

### Dark mode — "midnight"

Not an inversion of light mode — a separately considered palette, deep navy rather than warm near-black.

| Token | Hex | Use |
|---|---|---|
| Background | `#0B1220` | Page background — deep navy near-black |
| Surface | `#111A2C` | Cards, panels, rows |
| Surface (raised) | `#17223A` | Modals, popovers, top-most layer |
| Border / hairline | `#263351` | Rules, outlines, dividers |
| Border (strong) | `#38477A` | Emphasized dividers, focus outlines |
| Text primary | `#E7ECF6` | Headings, primary body text |
| Text secondary | `#9CA9C2` | Captions, labels, secondary text |
| Text muted | `#6B7997` | Placeholder text, disabled state |
| Accent (blue) | `#6C9BFF` | Primary actions, active state, current selection |
| Accent, on saturated fill | `#0B1220` | Text sitting on a strong accent fill |
| Accent fill (pale) | `#1B2C52` | Selected background, active chip |
| Success | `#4FCE97` | Confirmed / positive state |
| Warning | `#E9B24E` | Needs attention |
| Error | `#F17A6D` | Missing data, failed state, destructive actions |

### Usage ratios (rough guide, per screen)
- ~70% background/surface neutrals
- ~20% text and border neutrals
- ~5% accent
- ~5% success/warning/error, combined, only where a real state exists

---

## Shape language

- **Corner radius:** 2–4px on cards, panels, inputs. 4–6px on buttons. Nothing pill-shaped except small tags/status chips — reserve fully rounded shapes for those alone.
- **Borders over shadows:** structure comes from a single 1px hairline (`border` token), not box-shadow. Elevation for true overlays (modals, dropdowns) gets one subtle shadow only — 2–4px blur, low opacity, never a glow.
- **No nested rounding stacks.** A rounded card shouldn't contain another independently-rounded card with a different radius — pick one radius per compositional level and hold it.
- **Straight edges are allowed and encouraged for structural elements** — tables, dividers, rules — to keep a clean, structured feel. Rounding is reserved for interactive surfaces (buttons, inputs, chips), not for static content containers.
- **No skeuomorphism.** No embossing, inner shadows, gloss, or texture-as-decoration.

## What to avoid
- Gradients between any two palette colors
- Glassmorphism / frosted translucent panels
- Pure black anywhere, pure white only for card surfaces against a grey page background
- Accent color applied to more than one or two elements per screen
- Pill-shaped primary buttons
- Soft glowing box-shadows on cards