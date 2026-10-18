import asyncio
import json
import logging
import random
import re
from datetime import datetime
from typing import List, Literal, Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)


def _clean_date(value: str | None) -> Optional[str]:
    """Drop IPOJi's far-future TBA sentinel instead of treating it as a real date."""
    if not value:
        return None
    value = value.strip()
    if not value or "tba" in value.casefold():
        return None

    parsed = None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        for fmt in ("%b %d, %Y", "%d %b %Y"):
            try:
                parsed = datetime.strptime(value, fmt).date()
                break
            except ValueError:
                continue
    if parsed is None or parsed.year >= 2030:
        return None
    return parsed.isoformat()


class IPOSnapshot(BaseModel):
    company_name: str
    category: Literal["Mainboard"] = "Mainboard"
    status: str = "Upcoming"
    price_band_high: float = 0.0
    lot_size: int = 0
    gmp_price: float = 0.0
    qib_sub: float = 0.0
    nii_sub: float = 0.0
    retail_sub: float = 0.0
    retail_sub_is_proxy: bool = False
    subscription_total: float = 0.0
    category_split_available: bool = False
    total_subscription_available: bool = False
    open_date: Optional[str] = None
    close_date: Optional[str] = None
    listing_date: Optional[str] = None
    listing_price: Optional[float] = None
    listing_gain_pct: Optional[float] = None
    ipoji_url: Optional[str] = None
    registrar_name: Optional[str] = None
    registrar_url: Optional[str] = None
    gmp_observations: list[dict[str, str | float]] = Field(default_factory=list)
    scraped_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def estimated_listing_price(self) -> float:
        return self.price_band_high + self.gmp_price

    @property
    def expected_listing_gain_pct(self) -> float:
        if self.price_band_high <= 0:
            return 0.0
        return round((self.gmp_price / self.price_band_high) * 100, 2)


class IPOJiDetailData(BaseModel):
    registrar_name: Optional[str] = None
    registrar_url: Optional[str] = None
    gmp_observations: list[dict[str, str | float]] = Field(default_factory=list)


class IPODataAggregator:
    def __init__(self):
        self.headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        self.user_agents = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
        )
        self.urls = [
            "https://www.ipoji.com/ipo",
            "https://www.ipoji.com/ipo/upcoming-ipo",
            "https://www.ipoji.com/ipo/listed-ipo",
            "https://www.ipoji.com/ipo-subscription-status-live-bidding-data-bse-nse",
        ]

    def _request_headers(self) -> dict[str, str]:
        return {**self.headers, "User-Agent": random.choice(self.user_agents)}

    async def _get_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        attempts: int = 3,
    ) -> httpx.Response:
        """Retry transient upstream failures with a rotating browser identity."""
        for attempt in range(attempts):
            try:
                response = await client.get(url, headers=self._request_headers())
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                retryable = status_code in {408, 429} or status_code >= 500
                if not retryable or attempt + 1 >= attempts:
                    raise
                delay = 0.5 * (2 ** attempt)
                logger.warning(
                    "IPOJi returned HTTP %s for %s; retrying in %.1fs (%s/%s)",
                    status_code,
                    url,
                    delay,
                    attempt + 2,
                    attempts,
                )
            except httpx.TransportError as exc:
                if attempt + 1 >= attempts:
                    raise
                delay = 0.5 * (2 ** attempt)
                logger.warning(
                    "IPOJi request to %s failed (%s); retrying in %.1fs (%s/%s)",
                    url,
                    exc,
                    delay,
                    attempt + 2,
                    attempts,
                )

            await asyncio.sleep(delay)

        raise RuntimeError(f"Request to {url} failed after {attempts} attempts")

    @staticmethod
    def _company_key(name: str) -> str:
        """Match IPOJi's lifecycle cards with names on the subscription overview."""
        key = re.sub(r"\b(?:limited|ltd|ipo)\b", " ", name.casefold())
        return re.sub(r"[^a-z0-9]", "", key)

    @staticmethod
    def _parse_subscription_number(value: str) -> Optional[float]:
        match = re.search(r"(-?\d+(?:,\d{3})*(?:\.\d+)?)\s*x?", value, re.IGNORECASE)
        return float(match.group(1).replace(",", "")) if match else None

    @classmethod
    def _parse_subscription_overview(cls, soup: BeautifulSoup) -> dict[str, dict[str, float]]:
        """Read IPOJi's category table, keeping mainboard retail separate from SME Individual."""
        subscriptions: dict[str, dict[str, float]] = {}
        for table in soup.select("table"):
            rows = table.select("tr")
            if not rows:
                continue

            headers = [cell.get_text(" ", strip=True).casefold() for cell in rows[0].find_all(["th", "td"], recursive=False)]
            qib_index = next((i for i, value in enumerate(headers) if "qib" in value), None)
            nii_index = next((i for i, value in enumerate(headers) if "nii" in value), None)
            retail_index = next((i for i, value in enumerate(headers) if "retail" in value), None)
            total_index = next((i for i, value in enumerate(headers) if "total" in value), None)
            if None in (qib_index, nii_index, retail_index, total_index):
                continue

            for row in rows[1:]:
                if (row.get("data-ipo-type") or "").casefold() != "mainboard":
                    continue
                title = row.select_one(".subs-overview-ipo-title")
                cells = row.find_all("td", recursive=False)
                if not title or len(cells) <= max(qib_index, nii_index, retail_index, total_index):
                    continue

                values = {
                    "qib_sub": cls._parse_subscription_number(cells[qib_index].get_text(" ", strip=True)),
                    "nii_sub": cls._parse_subscription_number(cells[nii_index].get_text(" ", strip=True)),
                    "retail_sub": cls._parse_subscription_number(cells[retail_index].get_text(" ", strip=True)),
                    "subscription_total": cls._parse_subscription_number(cells[total_index].get_text(" ", strip=True)),
                }
                if any(value is None for value in values.values()):
                    continue
                subscriptions[cls._company_key(title.get_text(" ", strip=True))] = {
                    name: float(value) for name, value in values.items()
                }
        return subscriptions

    @staticmethod
    def _parse_card(card, url: str) -> Optional[IPOSnapshot]:
        name_tag = card.select_one(".ipo-card-name")
        if not name_tag:
            return None

        name = name_tag.get_text(" ", strip=True)
        text = card.get_text(" ", strip=True)
        detail_path = card.get("data-agent-href")
        if not detail_path:
            detail_link = card.select_one('a[href^="/ipo/"]')
            detail_path = detail_link.get("href") if detail_link else None
        if not detail_path:
            click_match = re.search(r"handleCardClick\(event,\s*['\"]([^'\"]+)", card.get("onclick", ""))
            detail_path = click_match.group(1) if click_match else None
        board = (card.get("data-ipo-board") or "").lower()
        if board == "sme" or "/sme-ipo/" in url:
            return None

        price_match = re.search(
            r"Offer\s*Price\D{0,20}(?:(\d+(?:\.\d+)?)\s*-\s*)?(\d+(?:\.\d+)?)",
            text,
            re.IGNORECASE,
        )
        lot_match = re.search(r"Lot Size\s*(\d+)", text, re.IGNORECASE)
        gmp_match = re.search(
            r"Exp\.\s*Premium\s*([^\d+-]{0,10}[+-]?\s*\d+(?:\.\d+)?)",
            text,
            re.IGNORECASE,
        )
        sub_match = re.search(r"Subscription\s*(\d+(?:\.\d+)?)x", text, re.IGNORECASE)
        status_tag = card.select_one(".ipo-card-status-badge")
        status = status_tag.get_text(" ", strip=True) if status_tag else "Upcoming"
        if "/listed-ipo" in url:
            status = "Listed"

        dates = card.select(".ipo-card-date time[datetime]")
        ipo_dates = [_clean_date(tag.get("datetime")) for tag in dates]
        listing_match = re.search(
            r"Listing\s*Date\D{0,20}([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}|\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4}|\d{4}-\d{2}-\d{2})",
            text,
            re.IGNORECASE,
        )
        listing_price_match = re.search(
            r"(?:List(?:ing)? Price)\D{0,20}([\d,]+(?:\.\d+)?)",
            text,
            re.IGNORECASE,
        )
        performance_match = re.search(
            r"(?:Premium|Discount)\s+of\s+([\d.]+)\s*%", text, re.IGNORECASE
        )
        listing_gain_pct = None
        if performance_match:
            listing_gain_pct = float(performance_match.group(1))
            if "discount" in text[performance_match.start():performance_match.end()].lower():
                listing_gain_pct *= -1
        elif re.search(r"\bat\s+Par\b", text, re.IGNORECASE):
            listing_gain_pct = 0.0
        elif listing_price_match and price_match:
            issue_price = float(price_match.group(2))
            listing_price = float(listing_price_match.group(1).replace(",", ""))
            if issue_price > 0:
                listing_gain_pct = round((listing_price - issue_price) / issue_price * 100, 2)
        total_sub = float(sub_match.group(1).replace(",", "")) if sub_match else 0.0
        retail_match = re.search(
            r"\bRetail(?:\s+Subscription)?\s*([\d,]+(?:\.\d+)?)x", text, re.IGNORECASE
        )
        nii_match = re.search(
            r"\bNII(?:\s+Subscription)?\s*([\d,]+(?:\.\d+)?)x", text, re.IGNORECASE
        )
        qib_match = re.search(
            r"\bQIB(?:\s+Subscription)?\s*([\d,]+(?:\.\d+)?)x", text, re.IGNORECASE
        )
        retail_sub = float(retail_match.group(1).replace(",", "")) if retail_match else 0.0
        gmp_text = re.sub(r"[^\d.-]", "", gmp_match.group(1)) if gmp_match else ""
        return IPOSnapshot(
            company_name=name,
            category="Mainboard",
            status=status,
            price_band_high=float(price_match.group(2)) if price_match else 0.0,
            lot_size=int(lot_match.group(1)) if lot_match else 0,
            gmp_price=float(gmp_text) if gmp_text else 0.0,
            retail_sub=retail_sub,
            retail_sub_is_proxy=False,
            qib_sub=float(qib_match.group(1).replace(",", "")) if qib_match else 0.0,
            nii_sub=float(nii_match.group(1).replace(",", "")) if nii_match else 0.0,
            subscription_total=total_sub,
            category_split_available=bool(qib_match and nii_match and retail_match),
            total_subscription_available=bool(sub_match),
            open_date=ipo_dates[0] if ipo_dates else None,
            close_date=ipo_dates[1] if len(ipo_dates) > 1 else None,
            listing_date=_clean_date(listing_match.group(1)) if listing_match else None,
            listing_price=(
                float(listing_price_match.group(1).replace(",", ""))
                if listing_price_match
                else None
            ),
            listing_gain_pct=listing_gain_pct,
            ipoji_url=urljoin("https://www.ipoji.com", detail_path) if detail_path else None,
        )

    @staticmethod
    def _registrar_portal_url(registrar_name: str | None) -> Optional[str]:
        if not registrar_name:
            return None
        key = re.sub(r"[^a-z0-9]+", " ", registrar_name.casefold()).strip()
        if "bigshare" in key:
            return "https://ipo.bigshareonline.com/IPO_Status.html"
        if "mufg" in key or "link intime" in key or "intime" in key:
            return "https://in.mpms.mufg.com/Initial_Offer/public-issues.html"
        if "kfin" in key:
            return "https://ipostatus.kfintech.com/"
        return None

    @classmethod
    def _parse_ipo_detail_page(cls, html: str, ipoji_url: str) -> IPOJiDetailData:
        soup = BeautifulSoup(html, "lxml")
        registrar_name = None
        for row in soup.select(".detail-list__row"):
            label = row.select_one("dt")
            value = row.select_one("dd")
            if (
                label
                and value
                and label.get_text(" ", strip=True).rstrip(":").casefold() == "registrar"
            ):
                registrar_name = value.get_text(" ", strip=True) or None
                break

        registrar_contact_url = None
        registrar_heading = next((
            heading
            for heading in soup.select(".premium-card .card-header h3")
            if "registrar contact details" in heading.get_text(" ", strip=True).casefold()
        ), None)
        if registrar_heading:
            contact_card = registrar_heading.find_parent("div", class_="premium-card")
            contact_body = contact_card.select_one(".card-body") if contact_card else None
            if contact_body:
                if not registrar_name:
                    name_tag = contact_body.select_one("p strong")
                    registrar_name = name_tag.get_text(" ", strip=True) if name_tag else None
                for link in contact_body.select("a[href]"):
                    candidate = urljoin(ipoji_url, link.get("href", "").strip())
                    parsed_url = urlparse(candidate)
                    if parsed_url.scheme in {"http", "https"} and parsed_url.netloc:
                        registrar_contact_url = candidate
                        break

        data_tag = soup.find("script", id="ipo-gmp-graph-data")
        raw_observations = []
        if data_tag:
            try:
                candidate_data = json.loads(data_tag.string or data_tag.get_text())
                if isinstance(candidate_data, list):
                    raw_observations = candidate_data
            except (json.JSONDecodeError, TypeError):
                pass

        observations: list[dict[str, str | float]] = []
        for item in raw_observations:
            if not isinstance(item, dict):
                continue
            try:
                timestamp = datetime.fromisoformat(
                    str(item["dateTime"]).replace("Z", "+00:00")
                ).isoformat()
                gmp_price = float(item["value"])
            except (KeyError, TypeError, ValueError):
                continue
            observations.append({"timestamp": timestamp, "gmp_price": gmp_price})

        observations.sort(key=lambda observation: str(observation["timestamp"]))
        return IPOJiDetailData(
            registrar_name=registrar_name,
            registrar_url=(
                cls._registrar_portal_url(registrar_name) or registrar_contact_url
            ),
            gmp_observations=observations,
        )

    async def fetch_ipo_detail(
        self,
        ipoji_url: str,
        client: httpx.AsyncClient | None = None,
    ) -> IPOJiDetailData:
        """Fetch registrar details and GMP observations, reusing a caller's pool when available."""
        if client is not None:
            response = await self._get_with_retry(client, ipoji_url)
            return self._parse_ipo_detail_page(response.text, ipoji_url)

        async with httpx.AsyncClient(
            timeout=15.0,
            headers=self.headers,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
        ) as standalone_client:
            response = await self._get_with_retry(standalone_client, ipoji_url)
            html = response.text
        return self._parse_ipo_detail_page(html, ipoji_url)

    async def fetch_gmp_observations(self, ipoji_url: str) -> list[dict[str, str | float]]:
        """Compatibility wrapper returning only IPOJi's timestamped GMP points."""
        detail = await self.fetch_ipo_detail(ipoji_url)
        return detail.gmp_observations

    async def fetch_live_gmp_data(self) -> List[IPOSnapshot]:
        async with httpx.AsyncClient(
            timeout=15.0,
            headers=self.headers,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
        ) as client:
            responses = await asyncio.gather(
                *(self._get_with_retry(client, url) for url in self.urls),
                return_exceptions=True,
            )

        by_name: dict[str, IPOSnapshot] = {}
        subscription_by_name: dict[str, dict[str, float]] = {}
        successful_pages = 0
        for url, response in zip(self.urls, responses):
            if isinstance(response, Exception) or response.status_code != 200:
                print(f"[IPOJi] Failed to fetch {url}: {response}")
                continue
            successful_pages += 1
            soup = BeautifulSoup(response.text, "lxml")
            if "ipo-subscription-status-live-bidding-data" in url:
                subscription_by_name = self._parse_subscription_overview(soup)
                print(f"[IPOJi] Read category subscriptions for {len(subscription_by_name)} mainboard IPOs")
                continue

            for card in soup.select("article.ipo-card"):
                snapshot = self._parse_card(card, url)
                if not snapshot or snapshot.category.casefold() != "mainboard":
                    continue
                key = self._company_key(snapshot.company_name)
                previous = by_name.get(key)
                if previous:
                    # Later dedicated upcoming/listed pages provide the lifecycle status;
                    # keep GMP and subscription from the current IPO card when available.
                    snapshot.gmp_price = previous.gmp_price or snapshot.gmp_price
                    if previous.total_subscription_available and not snapshot.total_subscription_available:
                        snapshot.subscription_total = previous.subscription_total
                        snapshot.total_subscription_available = True
                    if previous.category_split_available and not snapshot.category_split_available:
                        snapshot.qib_sub = previous.qib_sub
                        snapshot.nii_sub = previous.nii_sub
                        snapshot.retail_sub = previous.retail_sub
                        snapshot.category_split_available = True
                    snapshot.price_band_high = (
                        snapshot.price_band_high or previous.price_band_high
                    )
                    snapshot.lot_size = snapshot.lot_size or previous.lot_size
                    snapshot.listing_date = snapshot.listing_date or previous.listing_date
                    snapshot.listing_price = snapshot.listing_price or previous.listing_price
                    snapshot.listing_gain_pct = (
                        snapshot.listing_gain_pct
                        if snapshot.listing_gain_pct is not None
                        else previous.listing_gain_pct
                    )
                    snapshot.ipoji_url = snapshot.ipoji_url or previous.ipoji_url
                by_name[key] = snapshot

        if successful_pages == 0 or not by_name:
            raise RuntimeError("All IPOJi pages failed; refusing to return an empty snapshot")

        for key, snapshot in by_name.items():
            subscription = subscription_by_name.get(key)
            if subscription:
                snapshot.qib_sub = subscription["qib_sub"]
                snapshot.nii_sub = subscription["nii_sub"]
                snapshot.retail_sub = subscription["retail_sub"]
                snapshot.retail_sub_is_proxy = False
                snapshot.subscription_total = subscription["subscription_total"]
                snapshot.category_split_available = True
                snapshot.total_subscription_available = True

        # Reuse one keep-alive pool for all detail pages. Creating a client inside
        # each task repeats DNS/TCP/TLS setup and makes the detail stage much slower.
        detail_limits = httpx.Limits(max_connections=8, max_keepalive_connections=8)
        async with httpx.AsyncClient(
            timeout=15.0,
            headers=self.headers,
            follow_redirects=True,
            limits=detail_limits,
        ) as detail_client:
            semaphore = asyncio.Semaphore(8)

            async def populate_observations(snapshot: IPOSnapshot) -> None:
                if not snapshot.ipoji_url:
                    return
                try:
                    async with semaphore:
                        detail = await self.fetch_ipo_detail(
                            snapshot.ipoji_url,
                            client=detail_client,
                        )
                    snapshot.gmp_observations = detail.gmp_observations
                    snapshot.registrar_name = detail.registrar_name
                    snapshot.registrar_url = detail.registrar_url
                except Exception as exc:
                    print(
                        f"[IPOJi] Could not fetch detail data for "
                        f"{snapshot.company_name}: {exc}"
                    )

            await asyncio.gather(
                *(populate_observations(snapshot) for snapshot in by_name.values())
            )

        snapshots = list(by_name.values())
        print(f"[IPOJi] Scraped {len(snapshots)} IPO cards from {successful_pages} pages")
        return snapshots

async def main():
    aggregator = IPODataAggregator()
    for ipo in await aggregator.fetch_live_gmp_data():
        categories = (
            f"QIB: {ipo.qib_sub}x | NII: {ipo.nii_sub}x | Retail: {ipo.retail_sub}x"
            if ipo.category_split_available
            else "QIB: N/A | NII: N/A | Retail: N/A"
        )
        total = (
            f"{ipo.subscription_total}x"
            if ipo.total_subscription_available
            else "N/A"
        )
        print(
            f"[{ipo.category} | {ipo.status}] {ipo.company_name} | "
            f"Price INR {ipo.price_band_high} | GMP INR {ipo.gmp_price} | "
            f"{ipo.open_date} -> {ipo.close_date} | IPOJi listing date: {ipo.listing_date} | "
            f"Subscriptions: {categories} | Total: {total} | "
            f"Listing price: {ipo.listing_price} | Listing gain: {ipo.listing_gain_pct}%"
        )


if __name__ == "__main__":
    asyncio.run(main())
