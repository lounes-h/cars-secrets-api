"""The alert emails, in the dealer's language, with every time in the dealer's own zone and named.
Plain text, like the account emails (app/mail.py). The words follow the web's (src/messages)."""

from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode
from zoneinfo import ZoneInfo

WORDS: dict[str, dict[str, Any]] = {
    "en": {
        "days": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
        "months": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
                   "Dec"),
        "moment": "{weekday}, {month} {day}, {clock} {half} {zone}",  # Thu, Oct 8, 3:00 PM EDT
        "calendar_day": "{month} {day}, {year}",
        "halves": ("AM", "PM"),
        "lot": "Lot {number}",
        "sale": "Sale: {when}",
        "future_sale": "Future sale",
        "bid": "Current bid: {amount}",
        "buy_now": "Buy now: {amount}",
        "not_offered": "not offered",
        "matches_subject": ("1 new lot matches your saved searches",
                            "{n} new lots match your saved searches"),
        "matches_intro": "Lots listed since your last summary that match your saved searches:",
        "search": ("{name}: 1 new lot", "{name}: {n} new lots"),
        "also": "Also matches: {names}",
        "see_all": "See all {n}: {url}",
        "reminder_subject": "Auction reminder: {title}",
        "reminders_subject": "Auction reminders: {n} saved lots",
        "change_subject": "A saved lot changed: {title}",
        "changes_subject": "{n} saved lots changed",
        "reminder": {"hour": "Auction reminder, 1 hour before",
                     "day": "Auction reminder, 1 day before"},
        "starts": "The auction starts {when}. That is when the auction opens, not when this lot "
                  "comes up.",
        "changed": "Saved lot changed",
        "sale_change": "Auction: {old} → {new}",
        "buy_now_change": "Buy now: {old} → {new}",
        "footer": "Your alerts: {alerts}\nChoose what we email you: {settings}",
    },
    "es": {
        "days": ("lun", "mar", "mié", "jue", "vie", "sáb", "dom"),
        "months": ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sept", "oct", "nov",
                   "dic"),
        "moment": "{weekday} {day} {month}, {clock} {half} {zone}",  # jue 8 oct, 9:00 a. m. CST
        "calendar_day": "{day} {month} {year}",
        "halves": ("a. m.", "p. m."),
        "lot": "Lote {number}",
        "sale": "Subasta: {when}",
        "future_sale": "Venta futura",
        "bid": "Oferta actual: {amount}",
        "buy_now": "Compra inmediata: {amount}",
        "not_offered": "no disponible",
        "matches_subject": ("1 lote nuevo coincide con tus búsquedas guardadas",
                            "{n} lotes nuevos coinciden con tus búsquedas guardadas"),
        "matches_intro": "Lotes publicados desde tu último resumen que coinciden con tus "
                         "búsquedas guardadas:",
        "search": ("{name}: 1 lote nuevo", "{name}: {n} lotes nuevos"),
        "also": "También coincide con: {names}",
        "see_all": "Ver los {n}: {url}",
        "reminder_subject": "Recordatorio de subasta: {title}",
        "reminders_subject": "Recordatorios de subasta: {n} lotes guardados",
        "change_subject": "Un lote guardado cambió: {title}",
        "changes_subject": "{n} lotes guardados cambiaron",
        "reminder": {"hour": "Recordatorio de subasta, 1 hora antes",
                     "day": "Recordatorio de subasta, 1 día antes"},
        "starts": "La subasta empieza el {when}. Es la hora en que abre la subasta, no la hora "
                  "en que sale este lote.",
        "changed": "Un lote guardado cambió",
        "sale_change": "Subasta: {old} → {new}",
        "buy_now_change": "Compra inmediata: {old} → {new}",
        "footer": "Tus alertas: {alerts}\nElige qué te enviamos por correo: {settings}",
    },
}
AUCTIONS = {"copart": "Copart"}


def when(value: str | datetime, locale: str, tz: ZoneInfo) -> str:
    """A moment in the dealer's zone, named: "Thu, Oct 8, 3:00 PM EDT"."""
    w = WORDS[locale]
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    moment = moment.astimezone(tz)
    return w["moment"].format(
        weekday=w["days"][moment.weekday()], month=w["months"][moment.month - 1], day=moment.day,
        clock=f"{moment.hour % 12 or 12}:{moment.minute:02d}", half=w["halves"][moment.hour >= 12],
        zone=moment.strftime("%Z"))


def calendar_day(value: str | date, locale: str) -> str:
    """A sale day without a time, as the auction gives it: "Oct 9, 2026"."""
    w = WORDS[locale]
    day = value if isinstance(value, date) else date.fromisoformat(value)
    return w["calendar_day"].format(month=w["months"][day.month - 1], day=day.day, year=day.year)


def sale(at: str | None, day: str | None, locale: str, tz: ZoneInfo) -> str:
    if at:
        return when(at, locale, tz)
    return calendar_day(day, locale) if day else WORDS[locale]["future_sale"]


def money(value: float, currency: str | None) -> str:
    return f"${value:,.0f}" if currency in (None, "USD") else f"{value:,.0f} {currency}"


def offer(value: float | None, currency: str | None, locale: str) -> str:
    return money(value, currency) if value else WORDS[locale]["not_offered"]


def title(lot: dict[str, Any], locale: str) -> str:
    named = " ".join(str(part) for part in (lot["year"], lot["make"], lot["model"], lot["trim"])
                     if part)
    return named or WORDS[locale]["lot"].format(number=lot["lot_number"])


def link(web_url: str, locale: str, path: str) -> str:
    """A page of the web app, in the dealer's language (Spanish pages are under /es)."""
    return f"{web_url}{'' if locale == 'en' else '/' + locale}{path}"


def lot_link(web_url: str, locale: str, lot: dict[str, Any]) -> str:
    return link(web_url, locale, f"/active-lots/{quote(lot['source'])}/{quote(lot['lot_number'])}")


def plural(forms: tuple[str, str], n: int, **values: Any) -> str:
    return forms[n != 1].format(n=f"{n:,}", **values)


def heading(lot: dict[str, Any], locale: str) -> list[str]:
    """The lot's name, then its number, auction and place."""
    place = ", ".join(part for part in (lot["location_city"], lot["location_state"]) if part)
    where = [WORDS[locale]["lot"].format(number=lot["lot_number"]),
             AUCTIONS.get(lot["source"], lot["source"]), place]
    return [title(lot, locale), " · ".join(part for part in where if part)]


def change_lines(lot: dict[str, Any], changes: dict[str, Any] | None, locale: str,
                 tz: ZoneInfo) -> list[str]:
    w, lines = WORDS[locale], []
    if changes and (moved := changes.get("sale")):
        lines.append(w["sale_change"].format(
            old=sale(moved["from"]["at"], moved["from"]["date"], locale, tz),
            new=sale(moved["to"]["at"], moved["to"]["date"], locale, tz)))
    if changes and (price := changes.get("buy_now")):
        lines.append(w["buy_now_change"].format(old=offer(price["from"], lot["currency"], locale),
                                                new=offer(price["to"], lot["currency"], locale)))
    return lines


def footer(web_url: str, locale: str) -> str:
    return WORDS[locale]["footer"].format(alerts=link(web_url, locale, "/alerts"),
                                          settings=link(web_url, locale, "/settings"))


def indent(lines: list[str]) -> list[str]:
    return ["  " + line for line in lines]


def summary_mail(payload: dict[str, Any], locale: str, tz: ZoneInfo,
                 web_url: str) -> tuple[str, str]:
    """New lots by saved search; a lot matching several is listed once, naming the others."""
    w = WORDS[locale]
    parts = [w["matches_intro"]]
    for group in payload["searches"]:
        block = [plural(w["search"], group["total"], name=group["name"])]
        for lot in group["lots"]:
            facts = [w["sale"].format(when=sale(lot["sale_at"], lot["sale_date"], locale, tz))]
            if lot["high_bid"] is not None:
                facts.append(w["bid"].format(amount=money(lot["high_bid"], lot["currency"])))
            if lot["buy_it_now_price"]:
                facts.append(w["buy_now"].format(amount=money(lot["buy_it_now_price"],
                                                              lot["currency"])))
            others = [name for name in lot["searches"] if name != group["name"]]
            block += indent([*heading(lot, locale), " · ".join(facts),
                             *([w["also"].format(names=", ".join(others))] if others else []),
                             lot_link(web_url, locale, lot)])
        if group["total"] > len(group["lots"]):
            query = urlencode(parse_qsl(group["query"]))
            url = link(web_url, locale, "/active-lots" + (f"?{query}" if query else ""))
            block += indent([w["see_all"].format(n=f"{group['total']:,}", url=url)])
        parts.append("\n".join(block))
    parts.append(footer(web_url, locale))
    return plural(w["matches_subject"], payload["total"]), "\n\n".join(parts) + "\n"


def news_mail(reminders: list[dict[str, Any]], changes: list[dict[str, Any]], locale: str,
              tz: ZoneInfo, web_url: str) -> tuple[str, str]:
    """Reminders due now, each with what changed on its lot, then the other saved lots that
    changed."""
    w = WORDS[locale]
    if len(reminders) == 1:
        subject = w["reminder_subject"].format(title=title(reminders[0]["lot"], locale))
    elif reminders:
        subject = w["reminders_subject"].format(n=len(reminders))
    elif len(changes) == 1:
        subject = w["change_subject"].format(title=title(changes[0]["lot"], locale))
    else:
        subject = w["changes_subject"].format(n=len(changes))
    parts = []
    for item in reminders:
        lot = item["lot"]
        parts.append("\n".join([
            w["reminder"]["hour" if "hour" in item["reminders"] else "day"],
            *indent([*heading(lot, locale),
                     w["starts"].format(when=when(lot["sale_at"], locale, tz)),
                     *change_lines(lot, item["changes"], locale, tz),
                     lot_link(web_url, locale, lot)])]))
    for item in changes:
        lot = item["lot"]
        parts.append("\n".join([w["changed"], *indent([
            *heading(lot, locale), *change_lines(lot, item["changes"], locale, tz),
            lot_link(web_url, locale, lot)])]))
    parts.append(footer(web_url, locale))
    return subject, "\n\n".join(parts) + "\n"
