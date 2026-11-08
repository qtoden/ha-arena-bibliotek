"""Client for Axiell Arena library portals (e.g. bibliotek.varberg.se).

Arena has no public patron API, so this logs in through the normal web form
(card number or personal ID number + PIN) and reads "Mina sidor"
(/protected/my-account/overview), where loans, reservations and fees are shown.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
import logging
import re

import aiohttp
from bs4 import BeautifulSoup, Tag

_LOGGER = logging.getLogger(__name__)

OVERVIEW_PATH = "/protected/my-account/overview"
COVER_PATH = "/o/arena-portlet/portletResources"
START_PATH = "/"
USER_AGENT = "Mozilla/5.0 (Home Assistant arena_bibliotek)"
TIMEOUT = aiohttp.ClientTimeout(total=60)  # Arena logins can be slow

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_AGENCY_RE = re.compile(r"arena_agency_name=([A-Za-z0-9_]+)")
# Arena answers a record without a cover with a small placeholder GIF
MIN_COVER_BYTES = 1000


class ArenaError(Exception):
    """Communication or parsing problem."""


class ArenaAuthError(ArenaError):
    """Login rejected (wrong card number/PIN)."""


@dataclass
class Loan:
    """One borrowed item."""

    title: str
    author: str | None
    year: str | None
    media_type: str | None
    branch: str | None
    loan_date: date | None
    due_date: date | None
    renewable: bool
    overdue: bool
    renewals_left: int | None
    status: str | None
    record_id: str | None
    checkbox_value: str | None = field(default=None, repr=False)

    def as_dict(self) -> dict:
        """Attribute-friendly dict (dates as ISO strings, no form values)."""
        d = asdict(self)
        d.pop("checkbox_value", None)
        for key in ("loan_date", "due_date"):
            d[key] = d[key].isoformat() if d[key] else None
        return d


@dataclass
class Reservation:
    """One reservation (hold)."""

    title: str
    author: str | None
    status: str | None
    pickup_branch: str | None
    ready: bool
    pickup_by: date | None
    record_id: str | None = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["pickup_by"] = d["pickup_by"].isoformat() if d["pickup_by"] else None
        return d


@dataclass
class AccountData:
    """Everything read from the overview page."""

    loans: list[Loan]
    reservations: list[Reservation]
    fees: float | None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    agency: str | None = None  # the library's agency name, needed for cover images

    def to_storage(self) -> dict:
        """JSON-safe dict for the on-disk cache (keeps renewal checkbox values out)."""
        return {
            "loans": [l.as_dict() for l in self.loans],
            "reservations": [r.as_dict() for r in self.reservations],
            "fees": self.fees,
            "fetched_at": self.fetched_at.isoformat(),
            "agency": self.agency,
        }

    @classmethod
    def from_storage(cls, data: dict) -> "AccountData":
        def _d(v):
            return date.fromisoformat(v) if v else None

        loans = [
            Loan(**{**l, "loan_date": _d(l.get("loan_date")), "due_date": _d(l.get("due_date")),
                    "record_id": l.get("record_id"), "checkbox_value": None})
            for l in data.get("loans", [])
        ]
        reservations = [
            Reservation(**{**r, "pickup_by": _d(r.get("pickup_by"))}) for r in data.get("reservations", [])
        ]
        return cls(
            loans=loans,
            reservations=reservations,
            fees=data.get("fees"),
            fetched_at=datetime.fromisoformat(data["fetched_at"]),
            agency=data.get("agency"),
        )


def _text(el: Tag | None) -> str | None:
    if el is None:
        return None
    t = " ".join(el.get_text(" ", strip=True).split())
    return t or None


def _value(container: Tag, cls: str) -> str | None:
    """Text of `.arena-value` inside `.{cls}` (Arena's "field: value" layout)."""
    box = container.select_one(f".{cls}")
    if box is None:
        return None
    return _text(box.select_one(".arena-value")) or _text(box)


def _date(text: str | None) -> date | None:
    if not text:
        return None
    m = _DATE_RE.search(text)
    return date.fromisoformat(m.group(0)) if m else None


def parse_loans(soup: BeautifulSoup) -> list[Loan]:
    """Loans are table rows classed `arena-renewal-{true|false} arena-loan-overdue-{true|false}`."""
    loans: list[Loan] = []
    for row in soup.find_all("tr", class_=re.compile(r"\barena-renewal-(true|false)\b")):
        classes = row.get("class", [])
        item = row.select_one(".arena-renewal-item") or row
        title = _text(item.select_one(".arena-record-title"))
        if not title:
            continue
        branch_text = _value(item, "arena-renewal-branch")
        loan_date = _date(branch_text)
        branch = _DATE_RE.sub("", branch_text).strip() if branch_text else None
        status = _text(row.select_one(".arena-renewal-status-message"))
        renewals = re.search(r"(\d+)", status) if status and "omlån" in status.lower() else None
        checkbox = row.select_one("input[name=loansCheckboxGroup]")
        loans.append(
            Loan(
                title=title,
                author=_value(item, "arena-record-author"),
                year=_value(item, "arena-record-year"),
                media_type=_value(item, "arena-record-media"),
                branch=branch or None,
                loan_date=loan_date,
                due_date=_date(_text(row.select_one(".arena-renewal-date-value"))),
                renewable="arena-renewal-true" in classes,
                overdue="arena-loan-overdue-true" in classes,
                renewals_left=int(renewals.group(1)) if renewals else None,
                status=status,
                record_id=_text(item.select_one(".arena-record-id")),
                checkbox_value=checkbox.get("value") if checkbox else None,
            )
        )
    loans.sort(key=lambda l: (l.due_date or date.max, l.title))
    return loans


def parse_reservations(soup: BeautifulSoup) -> list[Reservation]:
    """Reservations portlet. Layout is less standardised than loans, so parse defensively:
    every element that carries a record title inside the reservations portlet is one hold."""
    portlet = soup.select_one(".portlet-myReservations") or soup.select_one(
        "[id^=p_p_id_reservationsWicket]"
    )
    if portlet is None:
        return []
    result: list[Reservation] = []
    for title_el in portlet.select(".arena-record-title"):
        item = title_el.find_parent(
            lambda t: t.name in ("tr", "li") or "arena-reservation-item" in (t.get("class") or [])
        ) or title_el.parent
        text = _text(item) or ""
        status = _text(item.select_one("[class*=status]"))
        ready = bool(re.search(r"hämta|avhämtning|klar|ready|available", (status or text), re.I))
        result.append(
            Reservation(
                title=_text(title_el) or "?",
                author=_value(item, "arena-record-author"),
                status=status,
                pickup_branch=_value(item, "arena-reservation-branch")
                or _value(item, "arena-record-branch"),
                ready=ready,
                pickup_by=_date(_text(item.select_one("[class*=expire], [class*=pickup], [class*=date]"))),
                record_id=_text(item.select_one(".arena-record-id")),
            )
        )
    return result


def parse_fees(soup: BeautifulSoup) -> float | None:
    box = soup.select_one(".arena-patron-debts")
    text = _text(box)
    if not text:
        return None
    m = re.search(r"(\d+(?:[  ]\d{3})*(?:,\d+)?)\s*kr", text)
    return float(m.group(1).replace(" ", "").replace(" ", "").replace(",", ".")) if m else None


def parse_overview(html_text: str) -> AccountData:
    soup = BeautifulSoup(html_text, "html.parser")
    # The PIN field is only on "Mina sidor" when you are NOT logged in (a rejected login shows the
    # form again, while the portlet frames are still rendered empty)
    if soup.select_one("input.arena-login-password"):
        raise ArenaAuthError("Not logged in (wrong card number/PIN?)")
    agency = _AGENCY_RE.search(html_text)
    return AccountData(
        loans=parse_loans(soup),
        reservations=parse_reservations(soup),
        fees=parse_fees(soup),
        agency=agency.group(1) if agency else None,
    )


@dataclass
class RenewOutcome:
    """Result for one loan we tried to renew."""

    title: str
    renewed: bool
    old_due_date: date | None
    new_due_date: date | None
    message: str | None

    def as_dict(self) -> dict:
        d = asdict(self)
        for key in ("old_due_date", "new_due_date"):
            d[key] = d[key].isoformat() if d[key] else None
        return d


def _renew_form(soup: BeautifulSoup) -> tuple[str, list[tuple[str, str]], str]:
    """Action URL, the form's own fields (hidden + selects) and the submit button name."""
    checkbox = soup.select_one("input[name=loansCheckboxGroup]")
    form = checkbox.find_parent("form") if checkbox else None
    if form is None or not form.get("action"):
        raise ArenaError("Renewal form not found")
    fields: list[tuple[str, str]] = []
    for inp in form.find_all("input", type="hidden"):
        if inp.get("name"):
            fields.append((inp["name"], inp.get("value", "")))
    for sel in form.find_all("select"):
        if not sel.get("name"):
            continue
        opt = sel.find("option", selected=True) or sel.find("option")
        fields.append((sel["name"], opt.get("value", "") if opt else ""))
    submit = form.find("input", attrs={"type": "submit", "name": re.compile(r"renew", re.I)})
    if submit is None:
        raise ArenaError("Renewal button not found")
    fields.append((submit["name"], submit.get("value", "")))
    return form["action"], fields, submit["name"]




class ArenaClient:
    """Logs in and fetches the account overview. One fresh session per fetch,
    so expired sessions never matter."""

    def __init__(self, base_url: str, username: str, pin: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._pin = pin

    async def _login(self, session: aiohttp.ClientSession) -> None:
        async with session.get(self.base_url + START_PATH) as resp:
            if resp.status != 200:
                raise ArenaError(f"Start page returned HTTP {resp.status}")
            soup = BeautifulSoup(await resp.text(), "html.parser")
        form = soup.find("form", id=re.compile(r"patronLogin"))
        if form is None or not form.get("action"):
            raise ArenaError("Login form not found (is this an Axiell Arena site?)")
        data: dict[str, str] = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if name and inp.get("type") != "submit":
                data[name] = inp.get("value", "")
        user_field = form.find("input", class_="arena-login-username")
        pin_field = form.find("input", class_="arena-login-password")
        if user_field is None or pin_field is None:
            raise ArenaError("Login fields not found")
        data[user_field["name"]] = self._username
        data[pin_field["name"]] = self._pin
        async with session.post(form["action"], data=data) as resp:
            if resp.status >= 400:
                raise ArenaError(f"Login returned HTTP {resp.status}")
            await resp.read()

    async def fetch(self) -> AccountData:
        jar = aiohttp.CookieJar()
        async with aiohttp.ClientSession(
            cookie_jar=jar, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
        ) as session:
            try:
                await self._login(session)
                async with session.get(self.base_url + OVERVIEW_PATH) as resp:
                    if resp.status != 200:
                        raise ArenaError(f"Account page returned HTTP {resp.status}")
                    html_text = await resp.text()
            except asyncio.TimeoutError as err:
                raise ArenaError("The library site did not answer in time") from err
            except aiohttp.ClientError as err:
                raise ArenaError(f"Connection error: {err}") from err
        data = parse_overview(html_text)
        _LOGGER.debug(
            "Arena: %s loans, %s reservations, fees %s",
            len(data.loans), len(data.reservations), data.fees,
        )
        return data

    async def fetch_cover(self, record_id: str, agency: str) -> bytes | None:
        """A record's cover image, or None when the library has none. Needs no login."""
        params = {"resource": "resourceCover", "recordId": record_id, "agencyName": agency}
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT) as session:
                async with session.get(self.base_url + COVER_PATH, params=params) as resp:
                    if resp.status != 200 or not resp.content_type.startswith("image/"):
                        return None
                    data = await resp.read()
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            raise ArenaError(f"Cover request failed: {err}") from err
        return data if len(data) >= MIN_COVER_BYTES else None

    async def renew(
        self,
        choose,
        dry_run: bool = False,
    ) -> tuple[list[RenewOutcome], AccountData | None]:
        """Log in, tick the chosen renewable loans and submit "Låna om" in the same session.

        `choose(loan) -> bool` picks loans. Non-renewable loans are reported but never submitted.
        With dry_run=True everything except the final POST runs (nothing is renewed).
        """
        jar = aiohttp.CookieJar()
        async with aiohttp.ClientSession(
            cookie_jar=jar, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
        ) as session:
            try:
                await self._login(session)
                async with session.get(self.base_url + OVERVIEW_PATH) as resp:
                    before_html = await resp.text()
                before = parse_overview(before_html)
                soup = BeautifulSoup(before_html, "html.parser")
                chosen = [l for l in before.loans if choose(l)]
                outcomes: list[RenewOutcome] = [
                    RenewOutcome(l.title, False, l.due_date, l.due_date, "Kan inte lånas om (" + (l.status or "spärrad") + ")")
                    for l in chosen if not l.renewable or not l.checkbox_value
                ]
                to_renew = [l for l in chosen if l.renewable and l.checkbox_value]
                if not to_renew:
                    return outcomes, before
                action, fields, _ = _renew_form(soup)
                fields += [("loansCheckboxGroup", l.checkbox_value) for l in to_renew]
                if dry_run:
                    _LOGGER.debug("Dry run: would POST %s fields to %s", len(fields), action.split("?")[0])
                    return outcomes + [
                        RenewOutcome(l.title, False, l.due_date, l.due_date, "Testkörning – inget skickades")
                        for l in to_renew
                    ], before
                async with session.post(action, data=fields) as resp:
                    if resp.status >= 400:
                        raise ArenaError(f"Renewal returned HTTP {resp.status}")
                    await resp.read()
                # Read the account again for the authoritative result
                async with session.get(self.base_url + OVERVIEW_PATH) as resp:
                    after = parse_overview(await resp.text())
            except asyncio.TimeoutError as err:
                raise ArenaError("The library site did not answer in time") from err
            except aiohttp.ClientError as err:
                raise ArenaError(f"Connection error: {err}") from err
        by_key = {(l.record_id or l.title): l for l in after.loans}
        for l in to_renew:
            new = by_key.get(l.record_id or l.title)
            new_due = new.due_date if new else None
            renewed = bool(new_due and l.due_date and new_due > l.due_date)
            outcomes.append(
                RenewOutcome(l.title, renewed, l.due_date, new_due, None if renewed else (new.status if new else "Hittades inte efter omlån"))
            )
        return outcomes, after
