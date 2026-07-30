# Product Signature Footer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Vue admin footer's plain centered text with a branded, responsive product signature bar.

**Architecture:** Keep the change inside the existing Vue shell. Reuse `brandLogoUrl` and the already-loaded `program.version` value from the settings API; use CSS-only layout and visibility rules so no backend or route changes are needed.

**Tech Stack:** Vue 3, Naive UI layout components, existing CSS, Python `unittest` contract tests, Vite.

---

### Task 1: Add the footer contract test

**Files:**
- Modify: `tests/test_frontend.py:52-69`
- Test: `tests/test_frontend.py`

- [ ] **Step 1: Add one focused failing test**

Add this method to `FrontendTests` after `test_vue_admin_wires_media_vault_logo_and_icons`:

```python
    def test_vue_admin_footer_uses_product_signature_layout(self):
        shell = (ROOT / "frontend/src/App.vue").read_text(encoding="utf-8")
        styles = (ROOT / "frontend/src/styles.css").read_text(encoding="utf-8")

        self.assertIn('class="app-footer"', shell)
        self.assertIn('class="footer-signature"', shell)
        self.assertIn('class="footer-product"', shell)
        self.assertIn('115 · CMS · Emby 工作流', shell)
        self.assertIn('class="footer-version"', shell)
        self.assertIn(':src="brandLogoUrl"', shell)
        self.assertIn(".footer-signature", styles)
        self.assertIn(".footer-version", styles)
        self.assertIn("@media (max-width: 520px)", styles)
        self.assertIn(".footer-caption", styles)
        self.assertIn("display: none", styles)
```

- [ ] **Step 2: Run the focused test and verify the intended failure**

Run:

```bash
python3 -m unittest tests.test_frontend.FrontendTests.test_vue_admin_footer_uses_product_signature_layout -v
```

Expected: `FAIL` because the current footer has no `footer-signature`, `footer-product`, `footer-version`, or caption contract.

### Task 2: Update the footer markup

**Files:**
- Modify: `frontend/src/App.vue:44`

- [ ] **Step 1: Replace the plain footer text with the product signature structure**

Replace:

```vue
<n-layout-footer class="app-footer">{{ program.app_name }}<span v-if="program.version"> {{ program.version }}</span>
</n-layout-footer>
```

with:

```vue
<n-layout-footer class="app-footer">
  <div class="footer-signature">
    <div class="footer-identity">
      <img class="footer-logo" :src="brandLogoUrl" alt="" width="24" height="24" />
      <span>
        <span class="footer-product">入库助手</span>
        <span class="footer-caption">115 · CMS · Emby 工作流</span>
      </span>
    </div>
    <span v-if="program.version" class="footer-version">v{{ program.version }}</span>
  </div>
</n-layout-footer>
```

- [ ] **Step 2: Re-run the focused contract test**

Run the command from Task 1. Expected: it may still fail only on the missing CSS selectors; do not change the test to match implementation details beyond the agreed contract.

### Task 3: Add responsive footer styling

**Files:**
- Modify: `frontend/src/styles.css:12-13`

- [ ] **Step 1: Replace the existing `.app-footer` rule and add only the required footer rules**

Use this CSS:

```css
.app-footer { padding: 0 28px; color: #748096; background: #fff; border-top: 1px solid #e7ebf2; }
.footer-signature { display: flex; align-items: center; justify-content: space-between; gap: 16px; min-height: 64px; }
.footer-identity { display: flex; align-items: center; gap: 10px; min-width: 0; }
.footer-logo { display: block; flex: 0 0 auto; width: 24px; height: 24px; }
.footer-product { display: block; color: #26344d; font-size: 13px; font-weight: 700; line-height: 1.15; }
.footer-caption { display: block; margin-top: 3px; color: #748096; font-size: 11px; line-height: 1.15; }
.footer-version { flex: 0 0 auto; padding: 5px 8px; border-radius: 999px; background: #eef3ff; color: #315da8; font: 700 11px/1 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
```

- [ ] **Step 2: Add narrow-screen behavior to the existing 520px media query**

Extend the existing rule to include:

```css
.app-footer { padding: 0 14px; }
.footer-caption { display: none; }
```

The product name and version remain visible at narrow widths, while the caption is removed before it can force a wrap.

- [ ] **Step 3: Run the focused contract test and verify it passes**

Run:

```bash
python3 -m unittest tests.test_frontend.FrontendTests.test_vue_admin_footer_uses_product_signature_layout -v
```

Expected: `OK`.

### Task 4: Validate the frontend and inspect the result

**Files:**
- No additional production files.

- [ ] **Step 1: Run all frontend contract tests**

Run:

```bash
python3 -m unittest tests.test_frontend -v
```

Expected: all `FrontendTests` pass.

- [ ] **Step 2: Run the Node tests and production build**

Run:

```bash
cd frontend
npm test
npm run build
```

Expected: Node tests pass and Vite exits with code 0.

- [ ] **Step 3: Run the full Python test suite and diff checks**

Run:

```bash
cd ..
python3 -m unittest discover -s tests -p 'test*.py' -q
git diff --check
```

Expected: the full suite reports `OK` and `git diff --check` is silent.

- [ ] **Step 4: Perform desktop and mobile visual inspection**

Open `/app/overview` in the current local/Unraid frontend and verify:

1. The footer is visually separated from the content by a thin line.
2. Logo, “入库助手”, caption, and version align on one row at desktop width.
3. At a narrow viewport, the caption disappears without horizontal overflow and the version remains visible.

- [ ] **Step 5: Commit the implementation**

```bash
git add frontend/src/App.vue frontend/src/styles.css tests/test_frontend.py
git commit -m "feat: refine product signature footer"
```
