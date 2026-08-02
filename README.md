# Geomapper-GH
Map builder from different repositories for use in map creation using ubano and other grasshopper visualising techniques

## Tests

Python:

```
.venv\Scripts\python.exe -m pytest
```

The map picker's front end (`src/mapgen/web/static/app.js`) is plain
JavaScript with no build step and no bundler, so it is tested separately
with a small, dependency-free Node script rather than a JS framework:

```
node tests/js/test_app.js
```

Requires Node (developed against v22). It runs the real, committed
`app.js` inside a minimal DOM and `fetch` stub and checks its behaviour
directly; it does not open a browser and cannot check map rendering or
tile loading, which still need a human looking at the page.
