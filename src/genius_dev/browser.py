"""Playwright-driven browser testing + deterministic DOM-based visual audit."""
from __future__ import annotations

import base64
import importlib.util
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VIEWPORTS = {"desktop": (1280, 800), "tablet": (768, 1024), "mobile": (390, 844)}


def playwright_installed() -> bool:
    return importlib.util.find_spec("playwright") is not None


async def browser_ready() -> tuple[bool, str]:
    """True only if a Chromium can actually be launched."""
    if not playwright_installed():
        return False, "playwright not installed  (pip install 'genius-dev[browser]' && playwright install chromium)"
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            b = await pw.chromium.launch()
            await b.close()
        return True, "chromium ok"
    except Exception as e:  # noqa: BLE001
        msg = str(e).splitlines()[0][:120]
        return False, f"chromium unavailable: {msg}  (run: playwright install chromium)"


AUDIT_JS = r"""
() => {
  const issues = [];
  const add = (sev, kind, msg, el) => issues.push({sev, kind, msg, sel: el ? sel(el) : ''});
  const sel = el => { let s = el.tagName.toLowerCase(); if (el.id) s += '#'+el.id; else if (el.className && typeof el.className==='string') s += '.'+el.className.trim().split(/\s+/).slice(0,2).join('.'); return s; };
  const vw = Math.min(window.innerWidth, screen.width || window.innerWidth), vh = window.innerHeight;   // mobile emulation may zoom-to-fit, so trust the device width
  if (document.documentElement.scrollWidth > vw + 1) add('fail','overflow',`horizontal scroll: content ${document.documentElement.scrollWidth}px wider than ${vw}px viewport`, document.documentElement);
  const lum = c => { const a=c.map(v=>{v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4)}); return 0.2126*a[0]+0.7152*a[1]+0.0722*a[2]; };
  const parse = s => { const m = s.match(/rgba?\(([^)]+)\)/); if(!m) return null; const p=m[1].split(',').map(x=>parseFloat(x)); return {r:p[0],g:p[1],b:p[2],a:p.length>3?p[3]:1}; };
  const bgOf = el => { while (el) { const c = parse(getComputedStyle(el).backgroundColor); if (c && c.a > 0.5) return c; el = el.parentElement; } return {r:255,g:255,b:255,a:1}; };
  const all = [...document.querySelectorAll('body *')].filter(e => { const r=e.getBoundingClientRect(); const cs=getComputedStyle(e); return r.width>0 && r.height>0 && cs.visibility!=='hidden' && cs.display!=='none'; });
  let contrastReported = 0, smallText = 0, clipped = 0;
  const fams = new Set();
  for (const e of all.slice(0, 1500)) {
    const cs = getComputedStyle(e);
    const own = [...e.childNodes].some(n => n.nodeType===3 && n.textContent.trim().length>1);
    if (own) {
      fams.add(cs.fontFamily.split(',')[0].trim());
      const fg = parse(cs.color), bg = bgOf(e);
      if (fg && bg) {
        const l1 = lum([fg.r,fg.g,fg.b]), l2 = lum([bg.r,bg.g,bg.b]);
        const ratio = (Math.max(l1,l2)+0.05)/(Math.min(l1,l2)+0.05);
        const big = parseFloat(cs.fontSize) >= 24 || (parseFloat(cs.fontSize) >= 18.66 && parseInt(cs.fontWeight) >= 700);
        if (ratio < (big?3:4.5) && contrastReported < 6) { contrastReported++; add('warn','contrast',`low contrast ${ratio.toFixed(1)}:1 on "${e.textContent.trim().slice(0,30)}"`, e); }
      }
      if (parseFloat(cs.fontSize) < 11 && smallText < 3) { smallText++; add('warn','typography',`tiny text (${cs.fontSize})`, e); }
      if ((cs.overflow==='hidden'||cs.overflowX==='hidden') && e.scrollWidth > e.clientWidth+2 && cs.textOverflow!=='ellipsis' && clipped < 5) { clipped++; add('fail','clipped',`text clipped (${e.scrollWidth}px in ${e.clientWidth}px box): "${e.textContent.trim().slice(0,30)}"`, e); }
    }
  }
  if (fams.size > 3) add('warn','typography',`${fams.size} different font families in use (${[...fams].slice(0,4).join(', ')})`, null);
  const interactive = all.filter(e => e.matches('a[href],button,input:not([type=hidden]),select,textarea,[role=button]'));
  for (const e of interactive) {
    const r = e.getBoundingClientRect();
    if (vw < 600 && (r.height < 32 || r.width < 32) && e.matches('a,button,[role=button]') && issues.filter(i=>i.kind==='tap-target').length < 4) add('warn','tap-target',`small tap target ${Math.round(r.width)}x${Math.round(r.height)}`, e);
    if (r.right > vw + 1 || r.left < -1) add('fail','offscreen','interactive element extends beyond viewport', e);
    if (e.matches('button,[role=button]') && !(e.textContent.trim() || e.getAttribute('aria-label') || e.getAttribute('title') || e.querySelector('img[alt],svg[aria-label],svg title'))) add('fail','a11y','button has no accessible name', e);
    if (e.matches('a') && (['','#','javascript:void(0)'].includes((e.getAttribute('href')||'').trim()))) add('warn','dead-link','link with empty or "#" href: '+e.textContent.trim().slice(0,30), e);
    if (e.matches('input,select,textarea') && !(e.labels && e.labels.length) && !e.getAttribute('aria-label') && !e.getAttribute('placeholder') && !e.getAttribute('title')) add('warn','a11y','form control has no label', e);
  }
  for (let i=0;i<Math.min(interactive.length,80);i++) for (let j=i+1;j<Math.min(interactive.length,80);j++) {
    const a=interactive[i], b=interactive[j]; if (a.contains(b)||b.contains(a)) continue;
    const ra=a.getBoundingClientRect(), rb=b.getBoundingClientRect();
    const w=Math.min(ra.right,rb.right)-Math.max(ra.left,rb.left), h=Math.min(ra.bottom,rb.bottom)-Math.max(ra.top,rb.top);
    if (w>4 && h>4 && (w*h)/Math.min(ra.width*ra.height, rb.width*rb.height) > 0.3) { add('fail','overlap',`"${(a.textContent||a.tagName).trim().slice(0,20)}" overlaps "${(b.textContent||b.tagName).trim().slice(0,20)}"`, a); break; }
  }
  if (all.length && vh >= 600) { const bottom = Math.max(...all.map(e => e.getBoundingClientRect().bottom + window.scrollY)); if (bottom < 0.30 * vh) add('warn','empty-space',`content fills only ${Math.round(100*bottom/vh)}% of the viewport height (large empty area)`, null); }
  document.querySelectorAll('img:not([alt])').forEach((im,i)=>{ if(i<3) add('warn','a11y','image without alt text', im); });
  if (!document.title.trim()) add('warn','a11y','page has no <title>', null);
  if (!document.querySelector('meta[name=viewport]')) add('warn','responsive','missing <meta name="viewport">', null);
  const body = document.body; const textLen = body.innerText.trim().length;
  if (textLen < 20 && document.querySelectorAll('img,canvas,svg,video').length === 0) add('fail','blank','page renders essentially no content', body);
  return issues;
}
"""


@dataclass
class FlowResult:
    ok: bool = True
    log: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def summary(self) -> str:
        bad = [i for i in self.issues if i["sev"] == "fail"]
        return (f"{'PASS' if self.ok else 'FAIL'} · {len(self.log)} steps · {len(self.console_errors)} console errors · "
                f"{len(self.failed_requests)} failed requests · {len(bad)} UI failures / {len(self.issues) - len(bad)} warnings")


async def run_flow(url: str, steps: list[dict[str, Any]] | None, shot_dir: Path, headless: bool = True,
                   viewport: str = "desktop", audit: bool = False, tag: str = "page") -> FlowResult:
    """Execute a scripted browser flow. Step actions: goto, click, fill, press, select, check, wait_for, expect_text,
    expect_url, reload, screenshot, wait."""
    res = FlowResult()
    if not playwright_installed():
        res.ok, res.error = False, "playwright is not installed"
        return res
    from playwright.async_api import async_playwright
    shot_dir.mkdir(parents=True, exist_ok=True)
    w, h = VIEWPORTS.get(viewport, VIEWPORTS["desktop"])
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=headless)
            ctx = await browser.new_context(viewport={"width": w, "height": h}, is_mobile=viewport == "mobile")
            page = await ctx.new_page()
            page.set_default_timeout(8000)
            page.on("console", lambda m: res.console_errors.append((m.text.splitlines() or [""])[0][:200]) if m.type == "error" else None)
            page.on("pageerror", lambda e: res.console_errors.append(f"uncaught: {str(e)[:200]}"))
            page.on("requestfailed", lambda r: res.failed_requests.append(f"{r.method} {r.url[:120]} — {r.failure}"))
            page.on("response", lambda r: res.failed_requests.append(f"{r.status} {r.request.method} {r.url[:120]}") if r.status >= 400 else None)
            try:
                await page.goto(url, wait_until="load")
                res.log.append(f"goto {url}")
                for i, st in enumerate(steps or [], 1):
                    a = st.get("action", "")
                    sel, val = st.get("selector", ""), st.get("value", st.get("text", ""))
                    if a == "goto":
                        await page.goto(st.get("url") or url + val, wait_until="load")
                    elif a == "click":
                        await page.click(sel)
                    elif a == "fill":
                        await page.fill(sel, str(val))
                    elif a == "press":
                        await page.press(sel or "body", str(val))
                    elif a == "select":
                        await page.select_option(sel, str(val))
                    elif a == "check":
                        await page.check(sel)
                    elif a == "wait_for":
                        await page.wait_for_selector(sel)
                    elif a == "wait":
                        await page.wait_for_timeout(int(val or 500))
                    elif a == "reload":
                        await page.reload(wait_until="load")
                    elif a == "expect_text":
                        body = await (page.inner_text(sel) if sel else page.inner_text("body"))
                        if str(val) not in body:
                            raise AssertionError(f"expected text {val!r} not found")
                    elif a == "expect_url":
                        if str(val) not in page.url:
                            raise AssertionError(f"expected URL containing {val!r}, at {page.url}")
                    elif a == "screenshot":
                        p = shot_dir / f"{tag}-{viewport}-{int(time.time())}-{i}.png"
                        await page.screenshot(path=str(p), full_page=True)
                        res.screenshots.append(str(p))
                    else:
                        raise ValueError(f"unknown browser action {a!r}")
                    res.log.append(f"{a} {sel or val or ''}".strip())
                await page.wait_for_timeout(200)
                if audit:
                    res.issues = await page.evaluate(AUDIT_JS)
                    res.issues += await _axe_issues(page, {i["kind"] + i["msg"] for i in res.issues})
                p = shot_dir / f"{tag}-{viewport}-{int(time.time())}.png"
                await page.screenshot(path=str(p), full_page=True)
                res.screenshots.append(str(p))
            except Exception as e:  # noqa: BLE001
                res.ok, res.error = False, f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"
                try:
                    p = shot_dir / f"{tag}-{viewport}-failure.png"
                    await page.screenshot(path=str(p))
                    res.screenshots.append(str(p))
                except Exception:  # noqa: BLE001
                    pass
            finally:
                await browser.close()
    except Exception as e:  # noqa: BLE001
        res.ok, res.error = False, f"browser launch failed: {str(e).splitlines()[0][:160]}"
    if res.ok and (res.console_errors or any(i["sev"] == "fail" for i in res.issues)):
        res.ok = False if audit else res.ok
    return res


def axe_installed() -> bool:
    return importlib.util.find_spec("axe_playwright_python") is not None


async def _axe_issues(page, seen: set[str]) -> list[dict[str, Any]]:
    """Optional axe-core pass (pip install 'genius-dev[a11y]'): critical violations fail the audit, the rest are warnings."""
    if not axe_installed():
        return []
    try:
        from axe_playwright_python.async_playwright import Axe
        data = (await Axe().run(page)).response
    except Exception:  # noqa: BLE001 — a11y engine problems must never break the audit itself
        return []
    out: list[dict[str, Any]] = []
    for v in data.get("violations", []):
        sev = "fail" if v.get("impact") == "critical" else "warn"
        node = (v.get("nodes") or [{}])[0]
        out.append({"sev": sev, "kind": f"axe:{v['id']}", "msg": f"{v.get('help', v['id'])} ({v.get('impact')}, {len(v.get('nodes', []))} element(s))",
                    "sel": (node.get("target") or [""])[0] if node.get("target") else ""})
    return out


def encode_png(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode()
