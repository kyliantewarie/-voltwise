# Attribution and intellectual property

## Our code

The VoltWise source code, the redesigned web UI (CSS, inline SVG chart, donut
ring), and all documentation were written specifically for this project and are
licensed under Apache-2.0 (see `LICENSE`).

## Design references (no source code copied)

The web UI follows conventions established by several Home Assistant projects.
No source code was copied from them; only colour values, spacing conventions and
layout patterns were used. These are either unprotectable facts (a colour hex
value, a radius measurement) or covered by permissive licences:

| Project | Licence | What informed our UI |
|---|---|---|
| [Home Assistant frontend](https://github.com/home-assistant/frontend) | Apache-2.0 | Dark theme colour tokens (`#111`, `#1c1c1c`, `#e1e1e1`, `#9b9b9b`) and energy semantics colours (import blue, export purple, solar amber, charge pink, discharge teal), sourced from `src/resources/theme/color/color.globals.ts` |
| [Mushroom](https://github.com/piitaya/lovelace-mushroom) | Apache-2.0 | Tile anatomy: icon chip → label → large number → secondary text; 12px radii and compact spacing |
| [Bubble Card](https://github.com/Clooos/Bubble-Card) | MIT | Mobile-first layout, sticky bottom navigation, generous tap targets |
| [apexcharts-card](https://github.com/RomRider/apexcharts-card) | MIT | radialBar-style SOC donut and sparkline/area chart concepts (implemented here with plain inline SVG) |
| [Home Assistant Sections dashboard](https://www.home-assistant.io/dashboards/sections/) | documentation | Section headings and responsive grid structure |

Full licence texts:
- Apache-2.0: https://www.apache.org/licenses/LICENSE-2.0
- MIT: https://opensource.org/licenses/MIT

## Code of Conduct

`CODE_OF_CONDUCT.md` is adapted from the [Contributor Covenant](https://www.contributor-covenant.org/),
version 2.1, available under CC BY 4.0. Attribution is also included at the
bottom of that file.

## Trademarks

"Home Assistant" and "HACS" are trademarks of their respective owners. They are
used here only descriptively to state which platform VoltWise integrates with.
VoltWise is not affiliated with or endorsed by the Home Assistant project or the
Open Home Foundation.

## Icons and images

The UI uses only Unicode symbols (⚡, 📈) and inline SVG shapes — no third-party
icon fonts, images, or graphical assets are bundled.
