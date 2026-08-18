"""FOMC meeting calendar connector.

Extracts scheduled FOMC meeting dates from the Federal Reserve's published
calendar page. Meeting dates are the single most important structural fact for
Fed-rate markets: "will the Fed cut in September" is unanswerable without
knowing when September's meeting is and whether it falls before the market's
resolution date.

**Scope discipline.** This parser extracts dates and nothing else. It does not
read statement text, does not infer policy direction, and does not attempt
sentiment. Those would be interpretation dressed up as evidence, and the
platform's rule is that external text is data.

The page is HTML rather than a structured feed — the Fed publishes no JSON
calendar. Parsing is restricted to a narrow, anchored regular expression over
the year and month headings, the extraction is size-capped, and a parse that
finds nothing reports DEGRADED rather than inventing a schedule.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from app.core.enums import (
    ComponentHealth,
    EvidenceType,
    MarketCategory,
    MarketSubcategory,
    SourceType,
    VerificationStatus,
)
from app.evidence.base import EvidenceError, EvidenceItem, EvidenceProvider
from app.ingest.http import FetchError

MAX_PAGE_BYTES = 4 * 1024 * 1024

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# The calendar groups meetings under a "<YYYY> FOMC Meetings" heading, then
# renders each meeting as a row containing a month cell and a date cell.
# Anchored to the Fed's own class names, verified against the live page on
# 2026-08-18, so a redesign produces zero matches (reported DEGRADED) rather
# than silently matching noise.
_YEAR_HEADING = re.compile(r">(\d{4})\s+FOMC\s+Meetings", re.I)

# Matches both the plain and the "--shaded" row variants the page alternates.
_MEETING_ROW = re.compile(
    r'class="[^"]*row fomc-meeting"(.*?)(?=class="[^"]*row fomc-meeting"|\Z)', re.I | re.S
)
_MONTH_CELL = re.compile(
    r'class="[^"]*fomc-meeting__month[^"]*"[^>]*>\s*(?:<strong>)?\s*([A-Za-z]+)'
    r'(?:\s*/\s*([A-Za-z]+))?',
    re.I,
)
_DATE_CELL = re.compile(
    r'class="[^"]*fomc-meeting__date[^"]*"[^>]*>\s*(?:<strong>)?\s*'
    r'([0-9]{1,2})\s*(?:-|\u2013|&#8211;)?\s*([0-9]{1,2})?',
    re.I,
)


class FOMCCalendarProvider(EvidenceProvider):
    """Scheduled FOMC meeting dates."""

    async def collect(self, *, now: datetime | None = None) -> list[EvidenceItem]:
        now = now or datetime.now(UTC)
        started = datetime.now(UTC)

        try:
            body = await self.fetcher.fetch_text(
                self.definition.base_url,
                headers={
                    "User-Agent": self.settings.evidence_user_agent,
                    "Accept": "text/html",
                },
            )
        except FetchError as exc:
            self._record_health(ComponentHealth.FAILED, str(exc)[:200], error_code=exc.error_code)
            raise EvidenceError(
                f"FOMC calendar fetch failed: {exc}",
                source_key=self.source_key, error_code=exc.error_code,
            ) from exc

        if len(body) > MAX_PAGE_BYTES:
            raise EvidenceError(
                f"FOMC page exceeded {MAX_PAGE_BYTES} bytes",
                source_key=self.source_key, error_code="oversized",
            )

        meetings = self._parse_meetings(body)
        if not meetings:
            # A structure change is a real, observable failure. Reporting
            # DEGRADED with zero items is honest; guessing a schedule is not.
            self._record_health(
                ComponentHealth.DEGRADED,
                "calendar fetched but no meetings parsed; the page structure may have changed",
                error_code="no_meetings_parsed",
            )
            return []

        items = [
            EvidenceItem(
                source_key=self.source_key,
                source_type=SourceType.OFFICIAL_GOVERNMENT,
                source_tier=1,
                evidence_type=EvidenceType.SCHEDULED_EVENT,
                series_key="FOMC_MEETING",
                title=f"FOMC meeting, {start:%Y-%m-%d}"
                + (f" to {end:%Y-%m-%d}" if end and end != start else ""),
                numeric_value=None,
                unit=None,
                observation_date=start,
                known_at=now,
                reference_url=self.definition.base_url,
                verification_status=VerificationStatus.CONFIRMED_FACT,
                reliability_score=self.definition.reliability_score,
                parser_version=self.definition.parser_version,
                payload={
                    "start_date": start.isoformat(),
                    "end_date": (end or start).isoformat(),
                    "is_future": start > now,
                },
                subject_tags=("fomc", "fed", "federal reserve", "rate decision", "meeting"),
                categories=(MarketCategory.FEDERAL_RESERVE, MarketCategory.MACROECONOMICS),
                subcategories=(MarketSubcategory.FED_RATES,),
            )
            for start, end in meetings
        ]

        latency = int((datetime.now(UTC) - started).total_seconds() * 1000)
        future = sum(1 for s, _ in meetings if s > now)
        self._record_health(
            ComponentHealth.HEALTHY,
            f"{len(items)} meetings parsed, {future} still upcoming",
            items=len(items), latency_ms=latency,
        )
        return items

    # ------------------------------------------------------------------
    def _parse_meetings(self, html: str) -> list[tuple[datetime, datetime | None]]:
        """Extract (start, end) pairs. Returns empty on any structural change."""
        meetings: list[tuple[datetime, datetime | None]] = []

        # Each year's section runs from its heading to the next heading.
        headings = list(_YEAR_HEADING.finditer(html))
        sections: list[tuple[int, str]] = []
        for index, match in enumerate(headings):
            end = headings[index + 1].start() if index + 1 < len(headings) else len(html)
            try:
                year = int(match.group(1))
            except ValueError:
                continue
            if 2000 <= year <= 2100:
                sections.append((year, html[match.end() : end]))

        for year, section in sections:
            for row in _MEETING_ROW.findall(section):
                month_match = _MONTH_CELL.search(row)
                date_match = _DATE_CELL.search(row)
                if not month_match or not date_match:
                    continue

                first_month = _MONTHS.get(month_match.group(1).strip().lower())
                second_month = (
                    _MONTHS.get(month_match.group(2).strip().lower())
                    if month_match.group(2)
                    else None
                )
                if first_month is None:
                    continue

                try:
                    start_day = int(date_match.group(1))
                    end_day = int(date_match.group(2)) if date_match.group(2) else None
                except ValueError:
                    continue

                start = _safe_date(year, first_month, start_day)
                if start is None:
                    continue

                end = None
                if end_day is not None:
                    # A meeting spanning a month boundary (e.g. "Apr/May 29-1")
                    # ends in the second month.
                    end_month = second_month if (second_month and end_day < start_day) else first_month
                    end_year = year + 1 if (end_month or 0) < first_month else year
                    end = _safe_date(end_year, end_month or first_month, end_day)

                meetings.append((start, end))

        return meetings


def _safe_date(year: int, month: int, day: int) -> datetime | None:
    try:
        return datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return None
