# dimsheet (for Codex and other agents)


Measures a component on a rendered page (Chrome or Edge, headless, own profile) and draws one sheet: notes, plan view with spacing/height chains and HUG/FLEX/FILL words, radius callouts, exploded layers, variable tables (spacing, size, radius, container, shadow) and a title block. Every value comes from the browser; values the CSS does not give are `[TBD]`.

## Run
From the repo root:

```bash
python dimsheet.py <file.html | http(s)://url> --select "<css selector>" -o <out-dir>
python dimsheet.py <file.html> --select ".card" --png -o out      # also sheet.png + component.png
python dimsheet.py --json out/measure.json -o out                 # redraw without a browser
```

Outputs in `<out-dir>`: `sheet.svg`, `sheet-motion.svg` (draws itself, loops), `measure.json`, and with `--png` `sheet.png` and `component.png`.

## How to use it well
1. Find the component's selector first (read the source or the page). For a dev server, start it, then pass the URL.
2. Use `--width 375` for a mobile layout; note 2 on the sheet records the width.
3. Show the user `sheet.png` next to `component.png` and check that the slots match.
4. Do not edit numbers on the sheet by hand. If a value is wrong, the page renders it that way; fix the CSS and run again.
5. It draws architecture only: copy and colours are left out on purpose.
