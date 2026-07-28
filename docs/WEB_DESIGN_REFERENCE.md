# Design reference — color and shape language

A neutral-first design concept: warm paper and ink in light mode, dim stage light in dark mode. Neutrals carry almost the entire interface; a single brass accent is used sparingly to mark what matters. No gradients, no glassmorphism, minimal elevation.

---

## Concept

The palette should read as archival, not digital-native — closer to a well-kept paper catalog under reading-room light than to a SaaS dashboard. Neutrals are not gray-scale-from-black; they're warmed slightly toward paper (light mode) and toward a dim wood/stage tone (dark mode), so the interface never feels cold or clinical.

Rules to live by:
- **Neutrals do the work.** At least 90% of any screen's surface, text, and border color should come from the neutral scale. Color is the exception, not the rule.
- **One accent, used rarely.** The brass accent marks the single most important actionable or active element on a screen — never more than one or two uses per view.
- **No pure black, no pure white.** Every neutral, in both modes, is warmed off true black/white.
- **Flat fills only.** No gradients anywhere except as a last-resort, single continuous-property use (and even then, avoid by default).
- **Color is never the only signal.** Status/state differences (verified, warning, error) pair color with a label or icon, never color alone.

---

## Color system

### Light mode — "paper"

| Token | Hex | Use |
|---|---|---|
| Background | `#F6F1E7` | Page background — warm paper, not white |
| Surface | `#FCFAF4` | Cards, panels, rows |
| Surface (raised) | `#FFFFFF` | Modals, popovers, top-most layer |
| Border / hairline | `#DCD3BE` | Rules, outlines, dividers |
| Border (strong) | `#C4B89C` | Emphasized dividers, focus outlines |
| Text primary | `#231F1A` | Headings, primary body text |
| Text secondary | `#6B6152` | Captions, labels, secondary text |
| Text muted | `#9C9280` | Placeholder text, disabled state |
| Accent (brass) | `#9C6B14` | Primary actions, active state, current selection |
| Accent, on pale fill | `#7A5510` | Text sitting on the pale accent fill |
| Accent fill (pale) | `#F1E3C4` | Selected background, active chip |
| Success (neutral-leaning) | `#4B6B4C` | Confirmed / positive state |
| Warning | `#9C6B14` | Needs attention (shares the accent — deliberate) |
| Error | `#8C3B31` | Missing data, failed state, destructive actions |

### Dark mode — "stage light"

Not an inversion of light mode — a separately considered palette, warmed toward dim wood and low lamp light rather than blue-black.

| Token | Hex | Use |
|---|---|---|
| Background | `#171511` | Page background — warm near-black |
| Surface | `#1F1C17` | Cards, panels, rows |
| Surface (raised) | `#26221C` | Modals, popovers, top-most layer |
| Border / hairline | `#3A352B` | Rules, outlines, dividers |
| Border (strong) | `#524A3A` | Emphasized dividers, focus outlines |
| Text primary | `#EDE6D6` | Headings, primary body text |
| Text secondary | `#A69A82` | Captions, labels, secondary text |
| Text muted | `#746A56` | Placeholder text, disabled state |
| Accent (brass) | `#D9AE4E` | Primary actions, active state, current selection |
| Accent, on saturated fill | `#3A2F17` | Text sitting on a strong accent fill |
| Accent fill (pale) | `#3A2F17` | Selected background, active chip |
| Success (neutral-leaning) | `#84AE86` | Confirmed / positive state |
| Warning | `#D9AE4E` | Needs attention (shares the accent — deliberate) |
| Error | `#C77468` | Missing data, failed state, destructive actions |

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
- **Straight edges are allowed and encouraged for structural elements** — tables, dividers, rules — to keep the archival, catalog feel. Rounding is reserved for interactive surfaces (buttons, inputs, chips), not for static content containers.
- **No skeuomorphism.** No embossing, inner shadows, gloss, or texture-as-decoration.

## What to avoid
- Gradients between any two palette colors
- Glassmorphism / frosted translucent panels
- Pure black or pure white anywhere
- Accent color applied to more than one or two elements per screen
- Pill-shaped primary buttons
- Soft glowing box-shadows on cards