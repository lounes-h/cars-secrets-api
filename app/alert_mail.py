"""The alert emails, in the dealer's language, with every time in the dealer's own zone and named.
Built from the account emails' blocks (app/mail.py), so each is plain text and HTML. The words
follow the web's (src/messages)."""

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode
from zoneinfo import ZoneInfo

from . import mail

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
        "see_all": "See all {n}",
        "reminder_subject": "Auction reminder: {title}",
        "reminders_subject": "Auction reminders: {n} saved lots",
        "change_subject": "A saved lot changed: {title} · {what}",
        "changes_subject": "{n} saved lots changed · {what}",
        "changed": "Saved lot changed · {what}",
        "reminder": {"hour": "Auction reminder, 1 hour before",
                     "day": "Auction reminder, 1 day before"},
        "starts": "The auction starts {when}. That is when the auction opens, not when this lot "
                  "comes up.",
        "what": {"sale": "auction date changed", "sale_set": "auction date set",
                 "buy_now": "Buy now price changed"},
        "and": " and ",
        "sale_change": "Auction: {old} → {new}",
        "sale_set": "Auction date set: {new}",
        "buy_now_change": "Buy now: {old} → {new}",
        "alerts": "Your alerts",
        "settings": "Choose what we email you",
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
        "see_all": "Ver los {n}",
        "reminder_subject": "Recordatorio de subasta: {title}",
        "reminders_subject": "Recordatorios de subasta: {n} lotes guardados",
        "change_subject": "Un lote guardado cambió: {title} · {what}",
        "changes_subject": "{n} lotes guardados cambiaron · {what}",
        "changed": "Un lote guardado cambió · {what}",
        "reminder": {"hour": "Recordatorio de subasta, 1 hora antes",
                     "day": "Recordatorio de subasta, 1 día antes"},
        "starts": "La subasta empieza el {when}. Es la hora en que abre la subasta, no la hora "
                  "en que sale este lote.",
        "what": {"sale": "cambió la fecha de la subasta", "sale_set": "fecha de subasta fijada",
                 "buy_now": "cambió el precio de compra inmediata"},
        "and": " y ",
        "sale_change": "Subasta: {old} → {new}",
        "sale_set": "Fecha de subasta fijada: {new}",
        "buy_now_change": "Compra inmediata: {old} → {new}",
        "alerts": "Tus alertas",
        "settings": "Elige qué te enviamos por correo",
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


def lot_link(web_url: str, locale: str, lot: dict[str, Any]) -> str:
    return mail.page(web_url, locale,
                     f"/active-lots/{quote(lot['source'])}/{quote(lot['lot_number'])}")


def plural(forms: tuple[str, str], n: int, **values: Any) -> str:
    return forms[n != 1].format(n=f"{n:,}", **values)


def dated(time: dict[str, Any]) -> bool:
    return bool(time["at"] or time["date"])


def what(changes: list[dict[str, Any]], locale: str) -> str:
    """What changed on these lots: "Auction date set and Buy now price changed". A future sale
    given its date reads as set, not as changed."""
    w, parts = WORDS[locale], []
    if sales := [item["sale"] for item in changes if "sale" in item]:
        moved = any(dated(one["from"]) for one in sales)
        parts.append(w["what"]["sale" if moved else "sale_set"])
    if any("buy_now" in item for item in changes):
        parts.append(w["what"]["buy_now"])
    words = w["and"].join(parts)
    return words[:1].upper() + words[1:]


def change(template: str, old: str, new: str) -> mail.Block:
    """A change's line, its new value in bold in the HTML."""
    return mail.Block(template.format(old=old, new=new), mail.esc(template).format(
        old=mail.esc(old), new=f"<strong>{mail.esc(new)}</strong>"))


def change_lines(lot: dict[str, Any], changes: dict[str, Any] | None, locale: str,
                 tz: ZoneInfo) -> list[mail.Block]:
    w, lines = WORDS[locale], []
    if changes and (moved := changes.get("sale")):
        old = moved["from"]
        # A future sale given its date reads as set, not as "Future sale → …".
        lines.append(change(w["sale_change" if dated(old) else "sale_set"],
                            sale(old["at"], old["date"], locale, tz),
                            sale(moved["to"]["at"], moved["to"]["date"], locale, tz)))
    if changes and (price := changes.get("buy_now")):
        lines.append(change(w["buy_now_change"], offer(price["from"], lot["currency"], locale),
                            offer(price["to"], lot["currency"], locale)))
    return lines


def lot_block(lot: dict[str, Any], lines: list[str], locale: str, web_url: str,
              changes: Sequence[mail.Block] = ()) -> mail.Block:
    """The lot's name, linking to it, then its number, auction and place, then `lines`, then
    what changed, set apart."""
    place = ", ".join(part for part in (lot["location_city"], lot["location_state"]) if part)
    where = [WORDS[locale]["lot"].format(number=lot["lot_number"]),
             AUCTIONS.get(lot["source"], lot["source"]), place]
    return mail.lot(title(lot, locale), lot_link(web_url, locale, lot),
                    [" · ".join(part for part in where if part), *lines], changes)


def finish(subject: str, blocks: list[mail.Block], locale: str, web_url: str) -> mail.Mail:
    """The email, with the way to the inbox and to what is emailed under it."""
    w = WORDS[locale]
    links = [(w["alerts"], mail.page(web_url, locale, "/alerts")),
             (w["settings"], mail.page(web_url, locale, "/settings"))]
    return mail.compose(subject, [*blocks, mail.footer(links)], locale, web_url)


def summary_mail(payload: dict[str, Any], locale: str, tz: ZoneInfo,
                 web_url: str) -> mail.Mail:
    """New lots by saved search; a lot matching several is listed once, naming the others."""
    w = WORDS[locale]
    blocks = [mail.paragraph(w["matches_intro"])]
    for group in payload["searches"]:
        lots = []
        for lot in group["lots"]:
            facts = [w["sale"].format(when=sale(lot["sale_at"], lot["sale_date"], locale, tz))]
            if lot["high_bid"] is not None:
                facts.append(w["bid"].format(amount=money(lot["high_bid"], lot["currency"])))
            if lot["buy_it_now_price"]:
                facts.append(w["buy_now"].format(amount=money(lot["buy_it_now_price"],
                                                              lot["currency"])))
            others = [name for name in lot["searches"] if name != group["name"]]
            lots.append(lot_block(lot, [" · ".join(facts), *(
                [w["also"].format(names=", ".join(others))] if others else [])], locale, web_url))
        if group["total"] > len(group["lots"]):
            query = urlencode(parse_qsl(group["query"]))
            url = mail.page(web_url, locale, "/active-lots" + (f"?{query}" if query else ""))
            lots.append(mail.link(w["see_all"].format(n=f"{group['total']:,}"), url))
        blocks.append(mail.section(plural(w["search"], group["total"], name=group["name"]), lots))
    return finish(plural(w["matches_subject"], payload["total"]), blocks, locale, web_url)


def news_mail(reminders: list[dict[str, Any]], changes: list[dict[str, Any]], locale: str,
              tz: ZoneInfo, web_url: str) -> mail.Mail:
    """Reminders due now, each with what changed on its lot, then the other saved lots that
    changed."""
    w, changed = WORDS[locale], what([item["changes"] for item in changes], locale)
    if len(reminders) == 1:
        subject = w["reminder_subject"].format(title=title(reminders[0]["lot"], locale))
    elif reminders:
        subject = w["reminders_subject"].format(n=len(reminders))
    elif len(changes) == 1:
        subject = w["change_subject"].format(what=changed, title=title(changes[0]["lot"], locale))
    else:
        subject = w["changes_subject"].format(what=changed, n=len(changes))
    blocks = []
    for item in reminders:
        lot, which = item["lot"], "hour" if "hour" in item["reminders"] else "day"
        blocks.append(mail.section(w["reminder"][which], [
            lot_block(lot, [w["starts"].format(when=when(lot["sale_at"], locale, tz))], locale,
                      web_url, change_lines(lot, item["changes"], locale, tz))]))
    for item in changes:
        lot = item["lot"]
        blocks.append(mail.section(w["changed"].format(what=what([item["changes"]], locale)), [
            lot_block(lot, [], locale, web_url, change_lines(lot, item["changes"], locale, tz))]))
    return finish(subject, blocks, locale, web_url)
