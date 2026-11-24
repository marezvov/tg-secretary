"""Read-only web agent: fast extractor, hybrid Set-of-Mark VLM navigation, PDFs."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from . import llm_client
from .prompts import som_prompt
from .runtime import HEAVY_SEMAPHORE
from .schemas import WebAction, WatchDecision  # noqa: F401 - public contract

log = logging.getLogger(__name__)

MAX_PAGE_CHARS = 20000
MAX_PDF_CHARS = 20000


class BrowserError(RuntimeError):
    pass


@dataclass
class PageContent:
    url: str
    title: str
    text: str
    links: list[tuple[str, str]] = field(default_factory=list)
    pdf_texts: list[str] = field(default_factory=list)

    def prompt_text(self, limit: int = MAX_PAGE_CHARS) -> str:
        links = "\n".join(f"- {text}: {href}" for text, href in self.links[:40])
        pdfs = "\n\n".join(self.pdf_texts)
        body = (
            f"URL: {self.url}\n"
            f"TITLE: {self.title}\n\n"
            f"CONTENT:\n{self.text[:limit]}"
        )
        if links:
            body += f"\n\nLINKS:\n{links}"
        if pdfs:
            body += f"\n\nPDF CONTENT:\n{pdfs[:MAX_PDF_CHARS]}"
        return f"<untrusted_web_data>\n{body}\n</untrusted_web_data>"


def _validate_url(url: str) -> str:
    candidate = (url or "").strip()
    if not candidate.startswith(("http://", "https://")):
        candidate = "https://" + candidate
    parsed = urlparse(candidate)
    if not parsed.netloc:
        raise BrowserError("Некорректный URL")
    return candidate


async def _block_heavy_assets(route: Any) -> None:
    if route.request.resource_type in {"image", "media", "font"}:
        await route.abort()
    else:
        await route.continue_()


async def _block_media(route: Any) -> None:
    if route.request.resource_type in {"media"}:
        await route.abort()
    else:
        await route.continue_()


async def extract_page(url: str, goal: str = "", timeout: int = 90) -> PageContent:
    """Level 1: Chromium + trafilatura, no VLM, strictly read-only."""
    target = _validate_url(url)
    async with HEAVY_SEMAPHORE:
        try:
            return await asyncio.wait_for(_extract_page(target, goal), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise BrowserError(f"Страница не ответила за {timeout} секунд") from exc


async def _extract_page(url: str, goal: str) -> PageContent:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserError(
            "Playwright не установлен. Выполни: pip install playwright && playwright install chromium"
        ) from exc
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True, args=["--disable-dev-shm-usage"]
            )
            try:
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                    ),
                )
                await context.route("**/*", _block_heavy_assets)
                page = await context.new_page()
                page.set_default_timeout(12000)
                await page.goto(url, wait_until="domcontentloaded", timeout=35000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:  # noqa: BLE001 - many pages never go idle
                    pass
                try:
                    await page.evaluate(
                        """
                        () => {
                            document.querySelectorAll('details').forEach(d => d.open = true);
                            document.querySelectorAll('[aria-expanded="false"]').forEach(el => {
                                try { el.click(); } catch (e) {}
                            });
                        }
                        """
                    )
                    await page.wait_for_timeout(600)
                except Exception:
                    pass
                title = (await page.title()) or url
                html = await page.content()
                links = await _extract_links(page)
                text = _html_to_text(html, url)
                if not text.strip():
                    text = (await page.inner_text("body"))[:MAX_PAGE_CHARS]
                pdf_links = _select_pdf_links(links, goal)
                pdf_texts: list[str] = []
                for pdf_url in pdf_links:
                    pdf_text = await _download_pdf(context, pdf_url)
                    if pdf_text:
                        pdf_texts.append(f"PDF: {pdf_url}\n{pdf_text}")
                return PageContent(
                    url=page.url or url,
                    title=title,
                    text=text[:MAX_PAGE_CHARS],
                    links=links,
                    pdf_texts=pdf_texts,
                )
            finally:
                await browser.close()
    except BrowserError:
        raise
    except Exception as exc:  # noqa: BLE001 - browser launch/network failures
        raise BrowserError(f"Не удалось прочитать страницу: {exc}") from exc


async def _extract_links(page: Any) -> list[tuple[str, str]]:
    try:
        anchors = await page.eval_on_selector_all(
            "a[href]",
            """
            els => els.map(e => ({
                text: (e.innerText || e.getAttribute('aria-label') || '').trim().slice(0, 100),
                href: e.href
            })).filter(x => x.href && x.href.startsWith('http'))
            """,
        )
    except Exception:
        return []
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for anchor in anchors:
        href = str(anchor.get("href") or "")
        if not href or href in seen:
            continue
        seen.add(href)
        text = str(anchor.get("text") or "").strip() or href
        result.append((text[:120], href))
        if len(result) >= 100:
            break
    return result


def _select_pdf_links(links: list[tuple[str, str]], goal: str) -> list[str]:
    pdfs = [(text, href) for text, href in links if ".pdf" in href.lower()]
    if not pdfs:
        return []
    if not goal:
        return [href for _text, href in pdfs[:2]]
    words = {word for word in re.findall(r"[\w]{4,}", goal.lower())}
    scored: list[tuple[int, str]] = []
    for text, href in pdfs:
        haystack = f"{text} {href}".lower()
        score = sum(1 for word in words if word in haystack)
        if score:
            scored.append((score, href))
    scored.sort(key=lambda item: -item[0])
    selected = [href for _score, href in scored[:2]]
    return selected or [href for _text, href in pdfs[:2]]


async def _download_pdf(context: Any, url: str) -> str | None:
    try:
        response = await context.request.get(url, timeout=25000)
        if not response.ok:
            return None
        body = await response.body()
        if not body or len(body) > 20 * 1024 * 1024:
            return None
        return _pdf_to_text(body)
    except Exception as exc:  # noqa: BLE001
        log.debug("PDF download failed for %s: %s", url, exc)
        return None


def _pdf_to_text(data: bytes) -> str | None:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return None
    try:
        document = fitz.open(stream=data, filetype="pdf")
        parts = []
        for index, page in enumerate(document):
            if index >= 40:
                break
            parts.append(page.get_text("text"))
        document.close()
        text = "\n".join(parts).strip()
        return text[:MAX_PDF_CHARS] if text else None
    except Exception:
        return None


def _html_to_text(html: str, url: str) -> str:
    if not html:
        return ""
    try:
        import trafilatura

        extracted = trafilatura.extract(
            html,
            url=url,
            include_links=True,
            include_tables=True,
            include_images=False,
            favor_recall=True,
            output_format="markdown",
        )
        if extracted and extracted.strip():
            return extracted.strip()
    except Exception as exc:  # noqa: BLE001 - trafilatura is optional
        log.debug("trafilatura failed: %s", exc)
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "template"]):
            tag.decompose()
        for table in soup.find_all("table"):
            rows = []
            for row in table.find_all("tr"):
                cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                table.replace_with("\n" + "\n".join(rows) + "\n")
        text = soup.get_text("\n", strip=True)
        return re.sub(r"\n{3,}", "\n\n", text)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)


# --------------------------------------------------------------------------- Level 2
SOM_JS = """
() => {
    document.querySelectorAll('.__som_badge').forEach(el => el.remove());
    document.querySelectorAll('[data-som-id]').forEach(el => el.removeAttribute('data-som-id'));
    const selectors = 'a, button, [role="button"], [role="tab"], summary, input, select, textarea';
    const out = [];
    let id = 0;
    const seen = new Set();
    for (const el of document.querySelectorAll(selectors)) {
        if (seen.has(el)) continue;
        seen.add(el);
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        if (rect.width < 4 || rect.height < 4) continue;
        if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;
        if (rect.bottom < 0 || rect.top > window.innerHeight || rect.right < 0 || rect.left > window.innerWidth) continue;
        id += 1;
        el.setAttribute('data-som-id', String(id));
        const badge = document.createElement('div');
        badge.className = '__som_badge';
        badge.textContent = String(id);
        badge.style.cssText = [
            'position:fixed',
            'left:' + Math.max(0, rect.left) + 'px',
            'top:' + Math.max(0, rect.top) + 'px',
            'min-width:18px',
            'height:18px',
            'padding:1px 4px',
            'background:#ff0055',
            'color:#ffffff',
            'font:bold 12px/16px sans-serif',
            'text-align:center',
            'border-radius:4px',
            'z-index:2147483647',
            'pointer-events:none',
            'box-shadow:0 0 0 2px #ffffff'
        ].join(';');
        document.body.appendChild(badge);
        const text = (el.innerText || el.value || el.getAttribute('aria-label') ||
                      el.getAttribute('title') || el.getAttribute('alt') || '').trim();
        out.push({
            id: id,
            tag: el.tagName.toLowerCase(),
            text: text.slice(0, 100),
            href: el.href || null,
            aria: (el.getAttribute('aria-label') || '').slice(0, 100)
        });
        if (id >= 55) break;
    }
    return out;
}
"""


async def visual_navigate(
    url: str, goal: str, max_steps: int = 7, timeout: int = 90
) -> str:
    """Level 2: SoM VLM loop. Only the current screenshot is sent each step."""
    target = _validate_url(url)
    async with HEAVY_SEMAPHORE:
        try:
            return await asyncio.wait_for(
                _visual_navigate(target, goal, max_steps=max_steps), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            raise BrowserError(f"Визуальная навигация не уложилась в {timeout} секунд") from exc


async def _visual_navigate(url: str, goal: str, max_steps: int = 7) -> str:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserError(
            "Playwright не установлен. Выполни: pip install playwright && playwright install chromium"
        ) from exc
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True, args=["--disable-dev-shm-usage"]
            )
            try:
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                    ),
                )
                await context.route("**/*", _block_media)
                page = await context.new_page()
                page.set_default_timeout(12000)
                await page.goto(url, wait_until="domcontentloaded", timeout=35000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass

                history: list[str] = []
                previous_signature: str | None = None
                idle_steps = 0
                for step in range(1, max_steps + 1):
                    elements = await page.evaluate(SOM_JS)
                    element_map = _format_element_map(elements)
                    screenshot = await page.screenshot(type="jpeg", quality=80, full_page=False)
                    try:
                        screenshot = llm_client.compress_image(screenshot)
                    except Exception:
                        pass
                    prompt = som_prompt(
                        goal, step, max_steps,
                        "\n".join(history[-10:]) or "(пока нет)",
                        element_map or "(интерактивных элементов не видно)",
                    )
                    action_data = await llm_client.vision_json(
                        screenshot,
                        prompt,
                        model_cls=WebAction,
                        max_tokens=900,
                        acquire_lock=False,
                    )
                    action = str(action_data.get("action") or "extract")
                    thought = str(action_data.get("thought") or "")
                    log.info("SoM step %s/%s: %s (%s)", step, max_steps, action, thought[:120])

                    if action == "extract":
                        result = str(action_data.get("result") or "").strip()
                        if not result:
                            result = _html_to_text(await page.content(), page.url)[:6000]
                        return (
                            f"Визуальная навигация завершена за {step} шаг(ов).\n"
                            f"Цель: {goal}\n\n{result}"
                        )
                    if action == "click":
                        element_id = action_data.get("id")
                        if not _element_exists(elements, element_id):
                            history.append(f"{step}. click[{element_id}] -> элемента нет в карте")
                            idle_steps += 1
                        else:
                            try:
                                await page.locator(f'[data-som-id="{int(element_id)}"]').first.click(
                                    timeout=6000
                                )
                                await _settle(page)
                                if len(context.pages) > 1:
                                    page = context.pages[-1]
                                history.append(
                                    f"{step}. click[{element_id}] -> {_element_label(elements, element_id)}"
                                )
                            except Exception as exc:  # noqa: BLE001
                                history.append(f"{step}. click[{element_id}] -> ошибка: {exc}")
                                idle_steps += 1
                    elif action == "goto":
                        new_url = str(action_data.get("url") or "").strip()
                        try:
                            new_url = _validate_url(new_url)
                            await page.goto(new_url, wait_until="domcontentloaded", timeout=30000)
                            await _settle(page)
                            history.append(f"{step}. goto {new_url}")
                        except Exception as exc:  # noqa: BLE001
                            history.append(f"{step}. goto -> ошибка: {exc}")
                            idle_steps += 1
                    elif action == "scroll":
                        direction = str(action_data.get("direction") or "down").lower()
                        delta = 850 if direction != "up" else -850
                        try:
                            await page.evaluate(f"window.scrollBy(0, {delta})")
                            await page.wait_for_timeout(700)
                            history.append(f"{step}. scroll {direction}")
                        except Exception as exc:  # noqa: BLE001
                            history.append(f"{step}. scroll -> ошибка: {exc}")
                    else:
                        history.append(f"{step}. unknown action {action}")

                    try:
                        signature = hashlib.sha256(
                            (page.url + await page.content()).encode("utf-8", errors="ignore")
                        ).hexdigest()
                    except Exception:
                        signature = None
                    if signature is not None and signature == previous_signature:
                        idle_steps += 1
                    else:
                        idle_steps = 0
                    previous_signature = signature
                    if idle_steps >= 2:
                        history.append("Два холостых шага подряд — останавливаюсь.")
                        break

                fallback = _html_to_text(await page.content(), page.url)
                return (
                    "VLM не успел завершить навигацию. Вот очищенный текст текущей страницы:\n\n"
                    + fallback[:8000]
                )
            finally:
                await browser.close()
    except BrowserError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BrowserError(f"Ошибка визуальной навигации: {exc}") from exc


async def _settle(page: Any) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    await page.wait_for_timeout(900)


def _format_element_map(elements: list[dict[str, Any]]) -> str:
    lines = []
    for element in elements:
        text = str(element.get("text") or element.get("aria") or "").strip()
        href = element.get("href")
        line = f"[{element.get('id')}] <{element.get('tag')}> {text[:100]}"
        if href:
            line += f" -> {str(href)[:140]}"
        lines.append(line)
    return "\n".join(lines)


def _element_exists(elements: list[dict[str, Any]], element_id: Any) -> bool:
    try:
        numeric = int(element_id)
    except (TypeError, ValueError):
        return False
    return any(int(item.get("id", -1)) == numeric for item in elements)


def _element_label(elements: list[dict[str, Any]], element_id: Any) -> str:
    try:
        numeric = int(element_id)
    except (TypeError, ValueError):
        return ""
    for item in elements:
        if int(item.get("id", -1)) == numeric:
            return str(item.get("text") or item.get("aria") or item.get("tag") or "")[:100]
    return ""


async def page_text(url: str, goal: str = "") -> str:
    content = await extract_page(url, goal=goal)
    return content.prompt_text()
