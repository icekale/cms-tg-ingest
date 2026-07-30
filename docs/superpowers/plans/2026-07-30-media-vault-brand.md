# Media Vault Brand Assets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the temporary `CMS` header badge with the approved media-vault logo and ship matching browser and mobile icons without changing the admin UI layout or application behavior.

**Architecture:** Keep one hand-authored SVG as the visual source of truth under Vite's public assets. Generate checked-in 32 px and 180 px PNG derivatives from that SVG, wire all icon metadata through `frontend/index.html`, and load the same SVG in `App.vue` through `import.meta.env.BASE_URL` so the existing `/app/` deployment base remains correct.

**Tech Stack:** Vue 3, Vite 6, CSS, SVG, PNG, Python `unittest`, headless Google Chrome for deterministic raster generation and visual screenshots.

---

## File Map

- Create: `frontend/public/brand/logo-mark.svg` — canonical media-vault vector mark.
- Create: `frontend/public/brand/favicon-32.png` — 32 × 32 browser fallback icon generated from the SVG.
- Create: `frontend/public/brand/apple-touch-icon.png` — 180 × 180 mobile shortcut icon generated from the SVG.
- Modify: `frontend/index.html` — favicon, touch icon, and theme color metadata.
- Modify: `frontend/src/App.vue` — replace the temporary text badge with the SVG mark.
- Modify: `frontend/src/styles.css` — size the new mark and remove obsolete badge styling.
- Modify: `tests/test_frontend.py` — asset dimensions and UI wiring regression tests.

### Task 1: Add and verify the approved brand assets

**Files:**
- Create: `frontend/public/brand/logo-mark.svg`
- Create: `frontend/public/brand/favicon-32.png`
- Create: `frontend/public/brand/apple-touch-icon.png`
- Modify: `tests/test_frontend.py`

- [ ] **Step 1: Write the failing asset contract test**

Add `import struct` near the imports in `tests/test_frontend.py`, then add this method to `FrontendTests`:

```python
    def test_media_vault_brand_assets_exist_with_expected_dimensions(self):
        brand_dir = ROOT / "frontend/public/brand"
        logo = brand_dir / "logo-mark.svg"
        favicon = brand_dir / "favicon-32.png"
        touch_icon = brand_dir / "apple-touch-icon.png"

        self.assertTrue(logo.is_file())
        self.assertTrue(favicon.is_file())
        self.assertTrue(touch_icon.is_file())

        svg = logo.read_text(encoding="utf-8")
        self.assertIn('viewBox="0 0 64 64"', svg)
        self.assertIn("#1D4ED8", svg)
        self.assertIn('aria-label="媒体仓"', svg)

        def png_dimensions(path):
            payload = path.read_bytes()[:24]
            self.assertEqual(payload[:8], b"\x89PNG\r\n\x1a\n")
            return struct.unpack(">II", payload[16:24])

        self.assertEqual(png_dimensions(favicon), (32, 32))
        self.assertEqual(png_dimensions(touch_icon), (180, 180))
```

- [ ] **Step 2: Run the focused test and verify the expected failure**

Run:

```bash
python3 -m unittest tests.test_frontend.FrontendTests.test_media_vault_brand_assets_exist_with_expected_dimensions -v
```

Expected: FAIL at `self.assertTrue(logo.is_file())` because the brand directory does not exist yet.

- [ ] **Step 3: Create the canonical SVG**

Create `frontend/public/brand/logo-mark.svg` with exactly this content:

```svg
<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%" viewBox="0 0 64 64" role="img" aria-label="媒体仓">
  <rect x="4" y="4" width="56" height="56" rx="16" fill="#1D4ED8"/>
  <rect x="16" y="17" width="32" height="30" rx="7" fill="none" stroke="#FFFFFF" stroke-width="4"/>
  <path d="M16 26h32M25 17v9M39 17v9" fill="none" stroke="#FFFFFF" stroke-width="4" stroke-linecap="round"/>
  <path d="M27 32.5 40 38 27 43.5Z" fill="#FFFFFF"/>
</svg>
```

- [ ] **Step 4: Generate the checked-in PNG derivatives**

Run from the repository root:

```bash
brand_repo_root="$(git rev-parse --show-toplevel)"
brand_source="file://${brand_repo_root}/frontend/public/brand/logo-mark.svg"
chrome_bin="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
"$chrome_bin" --headless=new --hide-scrollbars --disable-gpu --force-device-scale-factor=1 --default-background-color=00000000 --window-size=32,32 --screenshot="frontend/public/brand/favicon-32.png" "$brand_source"
"$chrome_bin" --headless=new --hide-scrollbars --disable-gpu --force-device-scale-factor=1 --default-background-color=00000000 --window-size=180,180 --screenshot="frontend/public/brand/apple-touch-icon.png" "$brand_source"
```

Expected: Chrome reports two screenshots and both PNG files exist at the requested sizes.

- [ ] **Step 5: Run the focused asset test**

Run:

```bash
python3 -m unittest tests.test_frontend.FrontendTests.test_media_vault_brand_assets_exist_with_expected_dimensions -v
```

Expected: PASS.

- [ ] **Step 6: Commit the brand assets**

```bash
git add tests/test_frontend.py frontend/public/brand/logo-mark.svg frontend/public/brand/favicon-32.png frontend/public/brand/apple-touch-icon.png
git commit -m "feat: add media vault brand assets"
```

### Task 2: Wire the logo into the Vue shell and browser metadata

**Files:**
- Modify: `frontend/index.html`
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/styles.css`
- Modify: `tests/test_frontend.py`

- [ ] **Step 1: Write the failing UI wiring test**

Add this method to `FrontendTests`:

```python
    def test_vue_admin_wires_media_vault_logo_and_icons(self):
        index = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
        shell = (ROOT / "frontend/src/App.vue").read_text(encoding="utf-8")
        styles = (ROOT / "frontend/src/styles.css").read_text(encoding="utf-8")

        for expected in (
            "%BASE_URL%brand/logo-mark.svg",
            "%BASE_URL%brand/favicon-32.png",
            "%BASE_URL%brand/apple-touch-icon.png",
            'name="theme-color" content="#1D4ED8"',
        ):
            self.assertIn(expected, index)

        self.assertIn("brandLogoUrl", shell)
        self.assertIn("import.meta.env.BASE_URL", shell)
        self.assertIn('class="brand-logo"', shell)
        self.assertIn(':src="brandLogoUrl"', shell)
        self.assertIn('alt=""', shell)
        self.assertNotIn('<span class="brand-mark">CMS</span>', shell)
        self.assertIn(".brand-logo", styles)
        self.assertNotIn(".brand-mark", styles)
```

- [ ] **Step 2: Run the focused test and verify the expected failure**

Run:

```bash
python3 -m unittest tests.test_frontend.FrontendTests.test_vue_admin_wires_media_vault_logo_and_icons -v
```

Expected: FAIL because `frontend/index.html` does not contain the brand icon links.

- [ ] **Step 3: Add browser icon metadata**

Insert these lines in `frontend/index.html` after the viewport meta tag:

```html
    <meta name="theme-color" content="#1D4ED8" />
    <link rel="icon" type="image/svg+xml" href="%BASE_URL%brand/logo-mark.svg" />
    <link rel="icon" type="image/png" sizes="32x32" href="%BASE_URL%brand/favicon-32.png" />
    <link rel="apple-touch-icon" sizes="180x180" href="%BASE_URL%brand/apple-touch-icon.png" />
```

- [ ] **Step 4: Replace the temporary CMS badge**

Add this constant after the existing refs in `frontend/src/App.vue`:

```javascript
const brandLogoUrl = `${import.meta.env.BASE_URL}brand/logo-mark.svg`
```

Replace the current brand block with:

```vue
<div class="brand"><img class="brand-logo" :src="brandLogoUrl" alt="" width="38" height="38" /><span>入库助手</span></div>
```

- [ ] **Step 5: Replace the obsolete badge CSS**

Replace the `.brand-mark` rule in `frontend/src/styles.css` with:

```css
.brand-logo { display: block; flex: 0 0 auto; width: 38px; height: 38px; }
```

Do not change `.top-header`, `.brand`, `.header-note`, or the responsive breakpoints.

- [ ] **Step 6: Run the focused frontend contract tests**

Run:

```bash
python3 -m unittest tests.test_frontend -v
```

Expected: all `FrontendTests` pass.

- [ ] **Step 7: Commit the UI integration**

```bash
git add frontend/index.html frontend/src/App.vue frontend/src/styles.css tests/test_frontend.py
git commit -m "feat: show media vault brand in web UI"
```

### Task 3: Verify build output and visual behavior

**Files:**
- No source changes expected.

- [ ] **Step 1: Run frontend tests and production build**

Run:

```bash
cd frontend
npm test
npm run build
```

Expected: all Node tests pass and Vite completes successfully.

- [ ] **Step 2: Verify public assets and HTML references in the build**

Run from the repository root:

```bash
test -f frontend/dist/brand/logo-mark.svg
test -f frontend/dist/brand/favicon-32.png
test -f frontend/dist/brand/apple-touch-icon.png
rg -n "brand/logo-mark.svg|brand/favicon-32.png|brand/apple-touch-icon.png|theme-color" frontend/dist/index.html
```

Expected: all three files exist and the built HTML references `/app/brand/` assets.

- [ ] **Step 3: Capture desktop and mobile previews**

Start the Vite preview server in a persistent terminal:

```bash
cd frontend
npm run preview -- --host 127.0.0.1 --port 4174
```

Then run from another terminal:

```bash
chrome_bin="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
"$chrome_bin" --headless=new --hide-scrollbars --disable-gpu --window-size=1440,900 --screenshot="/tmp/cms-brand-desktop.png" "http://127.0.0.1:4174/app/overview"
"$chrome_bin" --headless=new --hide-scrollbars --disable-gpu --window-size=390,844 --screenshot="/tmp/cms-brand-mobile.png" "http://127.0.0.1:4174/app/overview"
```

Inspect both screenshots. Expected: the mark is 38 px, the Header remains 60 px tall, the title is readable, and neither desktop nor mobile layout overflows.

- [ ] **Step 4: Run the complete repository verification gate**

Run:

```bash
python3 -m compileall -q app bridge.py doctor.py
python3 -m unittest discover -s tests -p 'test*.py' -q
git diff --check
git status --short --branch
```

Expected: Python compilation succeeds, the full test suite ends with `OK`, the diff check is clean, and the feature branch has no uncommitted source changes.

- [ ] **Step 5: Keep release operations separate**

Do not bump the version, push GitHub, build Docker Hub, or deploy Unraid as part of this implementation unless the user separately requests release operations.
