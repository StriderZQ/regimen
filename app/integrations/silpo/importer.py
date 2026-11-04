"""The receipt importer: from the shop into the pantry, with no typing.

This is what the whole thing was for: a purchase must reach the pantry by itself. Manual
entry loses not because it is slow but because it gets skipped — and then the system
confidently advises buying what is already in the cupboard.

Three things that matter more here than speed:

  * **Deduplication on shop plus receipt id.** A rerun after a timeout must never enter
    the same purchases twice.
  * **Non-food never enters the pantry.** Detergent and bags are on the receipt, not in
    the fridge.
  * **An unknown line is never guessed.** A brand's spelling of cottage cheese is cottage
    cheese, but only the owner can know that. The line waits for his answer, and the

A batch is created only for lines where both the product and the weight are known. The
rest sits honestly in `pending` — visible in the report rather than lost.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain import pantry as pantry_domain
from app.models.base import utcnow
from app.models.catalog import ItemAlias, NutritionSource, StoreItem
from app.models.pantry import BatchSource, ExpirySource, StoragePlace
from app.models.policies import Policy, PolicyKind
from app.models.receipts import LineStatus, Receipt, ReceiptLine

log = logging.getLogger(__name__)

# Words that mark a receipt line as definitely not food. The list is deliberately short:
# better to ask once than to silently drop a product from the pantry. Values are matched
NON_FOOD_WORDS = (
    "пакет", "губк", "засіб", "плямовивідник", "фільтр", "заварник", "серветк",
    "туалетн", "мило", "шампун", "паста зубна", "батарейк", "лампа", "тютюнов",
    "сигарет", "скарбничк", "пральн", "миюч", "освіжувач", "рушник", "щітк",
)

# Storage place by name; the fridge is the default, because that is where things spoil
# fastest and putting cupboard goods in the fridge costs less than the reverse.
FREEZER_WORDS = ("заморож", "морожен")
PANTRY_WORDS = (
    "олія", "масло оливков", "крупа", "рис", "паста", "борошно", "цукор", "сіль",
    "консерв", "оцет", "соус", "спеці", "чай", "кава",
)


class SilpoContextError(Exception):
    """No shop parameters — history is unavailable, and that is not a network error."""


@dataclass
class ImportReport:
    receipts_new: int = 0
    receipts_seen: int = 0
    lines: int = 0
    not_food: int = 0
    batches: int = 0
    pending: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"new receipts: {self.receipts_new} (already had: {self.receipts_seen}), "
            f"lines: {self.lines}, batches created: {self.batches}, "
            f"non-food: {self.not_food}, awaiting an answer: {len(self.pending)}"
        )


# --- parsing a receipt line ----------------------------------------------------------

def grams_of(unit: str | None, quantity: float, *, pack_grams: float | None = None) -> float | None:
    """Kilograms and grams convert directly; pieces only when a piece weight is known."""
    unit = (unit or "").strip().lower().replace(",", ".")
    if not unit:
        return None
    if unit in ("кг", "kg"):
        return round(quantity * 1000, 1)
    if unit in ("г", "g", "гр"):
        return round(quantity, 1)
    if unit in ("л", "l"):
        return round(quantity * 1000, 1)  # for water and milk 1 l is about 1000 g
    if unit in ("шт", "pc", "уп"):
        return round(quantity * pack_grams, 1) if pack_grams else None

    if match := re.fullmatch(r"([\d.]+)\s*(кг|г|мл|л)", unit):
        value, suffix = float(match.group(1)), match.group(2)
        factor = {"кг": 1000.0, "г": 1.0, "мл": 1.0, "л": 1000.0}[suffix]
        return round(value * factor * quantity, 1)
    return None


def receipt_key(purchased) -> str:
    """The receipt's identity — the purchase time, to the second.

    The shop gives no receipt id at all, and the branch arrives sometimes as a number and
    sometimes as a name: the archive and the live API disagree, and one trip to the shop
    used to land in the database twice. A person cannot shop in two places in the same
    second, so the time is enough, and it is identical across sources.
    """
    moment = _exact_moment(purchased)
    # With no time there is nothing to identify a receipt by. Substituting "now" would
    # give every rerun a new id and create a duplicate on each pass.
    return moment.strftime("%Y-%m-%dT%H:%M:%S") if moment else ""


def looks_like_food(name: str) -> bool:
    lowered = name.lower()
    return not any(word in lowered for word in NON_FOOD_WORDS)


def place_for(name: str) -> StoragePlace:
    lowered = name.lower()
    if any(word in lowered for word in FREEZER_WORDS):
        return StoragePlace.FREEZER
    if any(word in lowered for word in PANTRY_WORDS):
        return StoragePlace.PANTRY
    return StoragePlace.FRIDGE


# --- matching against the catalogue ---------------------------------------------------

def resolve_item(
    session: Session, owner_id: str, *, raw_name: str, external_id: str | None
) -> StoreItem | None:
    """The article number beats the name: it survives the shop renaming a product."""
    if external_id:
        by_sku = session.scalar(
            select(StoreItem).where(
                StoreItem.owner_id == owner_id,
                StoreItem.store == "silpo",
                StoreItem.external_id == external_id,
            )
        )
        if by_sku:
            return by_sku

    alias = session.scalar(
        select(ItemAlias).where(
            ItemAlias.owner_id == owner_id,
            ItemAlias.store == "silpo",
            func.ru_lower(ItemAlias.raw_name) == raw_name.strip().lower(),
        )
    )
    if alias:
        alias.hits += 1
        return session.get(StoreItem, alias.store_item_id)
    return None


def remember_alias(
    session: Session, owner_id: str, *, raw_name: str, store_item: StoreItem
) -> ItemAlias:
    """The owner's answer to "what is this line" is remembered forever."""
    existing = session.scalar(
        select(ItemAlias).where(
            ItemAlias.owner_id == owner_id,
            ItemAlias.store == "silpo",
            func.ru_lower(ItemAlias.raw_name) == raw_name.strip().lower(),
        )
    )
    if existing:
        existing.store_item_id = store_item.id
        return existing
    alias = ItemAlias(
        owner_id=owner_id, store="silpo", raw_name=raw_name.strip(),
        store_item_id=store_item.id, hits=1,
    )
    session.add(alias)
    session.flush()
    return alias


def last_reconciled(session: Session, owner_id: str) -> dt.date | None:
    """The date of the last full stocktake, if there was one."""
    policy = session.scalar(
        select(Policy)
        .where(Policy.owner_id == owner_id, Policy.kind == PolicyKind.REVISION)
        .order_by(Policy.active_from.desc())
    )
    if policy is None:
        return None
    raw = (policy.value or {}).get("last_reconciled")
    try:
        return dt.date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def already_counted(session: Session, owner_id: str, purchased_at: dt.datetime) -> bool:
    """A purchase made before the stocktake is already in stock — counted by eye.

    Without this check, importing old receipts doubles the balance: a stocktake counted
    the chicken and the cottage cheese, and a receipt from the same day entered them again.
    """
    boundary = last_reconciled(session, owner_id)
    return boundary is not None and purchased_at.date() <= boundary


def stock_from_line(
    session: Session, owner_id: str, line: ReceiptLine, store_item: StoreItem
) -> str | None:
    """Create a batch from a receipt line. With no weight, no batch is created."""
    if already_counted(session, owner_id, line.receipt.purchased_at):
        line.store_item_id = store_item.id
        line.status = LineStatus.HISTORICAL
        line.note = "Bought before the stocktake — the balance already includes it"
        return None

    grams = line.grams or grams_of(line.unit, line.quantity, pack_grams=store_item.pack_grams)
    if not grams:
        return None

    batch = pantry_domain.add_batch(
        session,
        owner_id,
        canonical=store_item.canonical,
        store_item=store_item,
        label=store_item.name,
        grams=grams,
        place=place_for(store_item.name or line.raw_name),
        source=BatchSource.SILPO,
        acquired_at=line.receipt.purchased_at,
        expiry_source=ExpirySource.CALCULATED,
        price_per_kg=round(line.price / (grams / 1000), 2) if line.price and grams else None,
        receipt_line_id=line.id,
        weight_source="receipt",
    )
    line.batch_id = batch.id
    line.store_item_id = store_item.id
    line.status = LineStatus.MATCHED
    return batch.id


# --- import ---------------------------------------------------------------------------

def import_receipt(
    session: Session, owner_id: str, raw: dict, report: ImportReport
) -> Receipt | None:
    """One receipt. A repeat on shop plus external id creates no second record."""
    purchased = raw.get("createdAt") or raw.get("date")
    external_id = receipt_key(purchased)
    if not external_id:
        report.errors.append(
            f"Receipt with no purchase time — skipped (the shop sent: {purchased!r})"
        )
        return None

    existing = session.scalar(
        select(Receipt).where(
            Receipt.owner_id == owner_id,
            Receipt.store == "silpo",
            Receipt.external_id == external_id,
        )
    )
    if existing is not None:
        report.receipts_seen += 1
        return existing

    receipt = Receipt(
        owner_id=owner_id,
        store="silpo",
        external_id=external_id,
        branch=raw.get("filialName") or raw.get("branch") or raw.get("store"),
        purchased_at=_parse_moment(purchased),
        total=raw.get("sumReg") or raw.get("total") or raw.get("sum"),
        discount=raw.get("sumDiscount") or raw.get("discount"),
        bonuses=raw.get("accruedBalaBonusesSum") or raw.get("bonuses"),
        imported_at=utcnow(),
    )
    session.add(receipt)
    session.flush()
    report.receipts_new += 1

    # The receipt URL is deliberately not read: it is a public link anyone holding it
    # can open, and accounting does not need it.
    for entry in raw.get("products") or raw.get("items") or []:
        name = (entry.get("name") or entry.get("productName") or "").strip()
        sku = entry.get("lagerId") or entry.get("lager_id") or entry.get("id")
        unit = entry.get("unit") or entry.get("measure") or ""
        quantity = float(entry.get("qty") or entry.get("quantity") or 0)
        is_food = bool(entry.get("food", looks_like_food(name)))

        line = ReceiptLine(
            owner_id=owner_id,
            receipt_id=receipt.id,
            external_item_id=str(sku) if sku else None,
            raw_name=name,
            unit=unit,
            quantity=quantity,
            grams=entry.get("grams") or grams_of(unit, quantity),
            price=entry.get("price"),
            is_food=is_food,
            status=LineStatus.PENDING if is_food else LineStatus.NOT_FOOD,
        )
        session.add(line)
        session.flush()
        report.lines += 1

        if not is_food:
            report.not_food += 1
            continue

        if already_counted(session, owner_id, receipt.purchased_at):
            # The purchase predates the stocktake: the pantry already counted it. Asking
            # "what product is this?" about a melon from July is noise that buries the
            # real questions about recent receipts.
            line.status = LineStatus.HISTORICAL
            line.note = "Куплено до ревизии склада — остаток уже учтён"
            continue

        store_item = resolve_item(
            session, owner_id, raw_name=name, external_id=line.external_item_id
        )
        if store_item is None:
            report.pending.append(name)
            continue
        if stock_from_line(session, owner_id, line, store_item):
            report.batches += 1
        elif line.status is LineStatus.PENDING:
            # No batch and the line still waits: the reason is an unknown weight.
            # A pre-stocktake purchase never lands here; it has its own status and reason.
            line.note = "Weight cannot be derived from the receipt — the owner must answer"
            report.pending.append(name)

    session.flush()
    return receipt


# How the shop labels macros on a product card. Keys are theirs, in their language
NUTRITION_KEYS = {
    "Білки (г)": "protein_100",
    "Жири (г)": "fat_100",
    "Вуглеводи (г)": "carbs_100",
    "Харчові волокна (г)": "fiber_100",
}
ENERGY_KEY = "Енергетична цінність (кКал/кДЖ)"


def nutrition_from(attributes: dict) -> dict:
    """Macros from the shop's product card.

    This is the manufacturer's declaration for EXACTLY the product in the fridge, not an
    average from a reference table. The difference is real: one branded chicken fillet is
    166 kcal and 9.2 g of fat, while a generic skinless breast is 110 and 1.8.
    """
    if not isinstance(attributes, dict):
        return {}
    result = {}
    for key, column in NUTRITION_KEYS.items():
        value = attributes.get(key)
        if isinstance(value, int | float):
            result[column] = float(value)
    energy = attributes.get(ENERGY_KEY)
    if isinstance(energy, str) and energy.strip():
        # "166/697" — kilocalories and kilojoules separated by a slash
        head = energy.split("/")[0].strip().replace(",", ".")
        with contextlib.suppress(ValueError):
            result["kcal_100"] = float(head)
    elif isinstance(energy, int | float):
        result["kcal_100"] = float(energy)
    return result


def fresh_timeslot(client, branch_id: str) -> dict | None:
    """A delivery slot. Without one, product search silently returns nothing.

    Verified twice: with a stale slot the shop answers "no such product" to every query,
    including an exact article number. Receipt history still arrives normally — which is
    why the slot is needed only for the catalogue.
    """
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    payload = client.call(
        "silpo_get_time_slots",
        {"branchId": branch_id, "start": f"{dt.date.today().isoformat()}T00:00:00+00:00",
         "end": f"{tomorrow.isoformat()}T23:59:59+00:00", "limit": 25},
    )
    slots = (payload or {}).get("slots") or []
    for slot in slots:
        if slot.get("available") and slot.get("start") and slot.get("end"):
            return {"timeslotStart": slot["start"], "timeslotEnd": slot["end"]}
    return None


def enrich_nutrition(
    session: Session, owner_id: str, client, *, only_missing: bool = True
) -> dict:
    """Fill in catalogue macros from the shop's product cards.

    `only_missing=False` overwrites reference values too: a product declaration beats an
    average from a table. Values the owner entered from a label are never touched — he
    held the package in his hands and we did not.
    """
    report = {"updated": 0, "skipped": 0, "no_data": 0, "errors": []}
    try:
        context = cart_context(client)
    except Exception as exc:  # noqa: BLE001
        report["errors"].append(f"No shop parameters: {exc}")
        return report

    slot = fresh_timeslot(client, context["branchId"])
    if slot is None:
        report["errors"].append(
            "No delivery slot — the shop's catalogue is answering with nothing"
        )
        return report
    context.update(slot)

    items = session.scalars(
        select(StoreItem).where(
            StoreItem.owner_id == owner_id,
            StoreItem.store == "silpo",
            StoreItem.external_id.is_not(None),
        )
    ).all()

    for item in items:
        product = item.canonical
        if product is None:
            continue
        # Skipped only when we already hold this product's declaration:
        # it describes the goods, not the food, and does not depend on the card's state
        if only_missing and item.kcal_100 is not None:
            report["skipped"] += 1
            continue

        try:
            found = client.call(
                "silpo_find_products_batch",
                {**context, "products": [str(item.external_id)], "limit": 1},
            )
            queries = (found or {}).get("queries") or []
            products = queries[0].get("products") if queries else []
            if not products:
                report["no_data"] += 1
                continue
            details = client.call(
                "silpo_get_product_details", {**context, "slug": products[0]["slug"]}
            )
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"{item.name}: {exc}")
            continue

        macros = nutrition_from(((details or {}).get("product") or {}).get("attributes", {}))
        if not macros.get("protein_100") and not macros.get("kcal_100"):
            report["no_data"] += 1
            continue

        # The shop's declaration attaches to the GOODS: it describes what was sold.
        for column, value in macros.items():
            setattr(item, column, value)
        report["updated"] += 1

        # It reaches the food only when nothing at all is known about it. Chicken is
        # sold with the skin and the skin is thrown away: 166 kcal on the package and
        # 110 on the plate are both honest, but they are different numbers.
        if product.protein_100 is None and product.kcal_100 is None:
            for column, value in macros.items():
                setattr(product, column, value)
            product.nutrition_source = NutritionSource.LABEL
            continue

        differences = [
            f"{column.replace('_100', '')}: ours is {getattr(product, column)}, "
            f"the shop says {value}"
            for column, value in macros.items()
            if getattr(product, column) is not None
            and abs(getattr(product, column) - value)
            > max(2.0, getattr(product, column) * 0.25)
        ]
        if differences:
            report.setdefault("conflicts", []).append(
                {
                    "product": product.name,
                    "store_item": item.name,
                    "differences": differences,
                    "prep_note": product.prep_note,
                }
            )

    session.flush()
    return report


def cart_context(client) -> dict:
    """Branch, fulfilment method and slot. Without them the shop returns no history.

    The slot is taken from the basket as it is, even a stale one: it has no effect on
    receipt delivery and only gates catalogue availability. Chasing a fresh slot for the
    sake of importing history is pointless.
    """
    cart_id = (client.call("silpo_get_my_shopping_cart") or {}).get("shoppingCartId")
    if not cart_id:
        raise SilpoContextError(
            "The account has no basket — the shop will not return history without it"
        )
    cart = (client.call("silpo_get_shopping_cart_by_id", {"shoppingCartId": cart_id}) or {})
    cart = cart.get("cart") or {}
    shipments = cart.get("shipments") or [{}]
    timeslot = cart.get("timeslot") or {}
    context = {
        "branchId": shipments[0].get("branchId"),
        "deliveryType": cart.get("deliveryType") or "DeliveryHome",
        "timeslotStart": timeslot.get("start"),
        "timeslotEnd": timeslot.get("end"),
    }
    missing = [k for k, v in context.items() if not v]
    if missing:
        raise SilpoContextError(f"The basket is missing data: {', '.join(missing)}")
    return context


def sync(
    session: Session,
    owner_id: str,
    client,
    *,
    months_back: int = 1,
    page_size: int = 10,
) -> ImportReport:
    """Fetch receipts for the recent months.

    The shop returns at most ten receipts per request and keeps a limited window of
    history — so we page by `offset`, and everything collected stays with us: on purchase
    history the truth lives here, not there.
    """
    report = ImportReport()
    today = dt.date.today()
    start = today - dt.timedelta(days=31 * months_back)

    try:
        context = cart_context(client)
    except Exception as exc:  # noqa: BLE001 — show the reason, not a traceback
        report.errors.append(f"Could not fetch the shop parameters: {exc}")
        return report

    offset = 0
    while True:
        try:
            payload = client.call(
                "silpo_get_my_offline_orders",
                {
                    **context,
                    "dateStart": f"{start.isoformat()}T00:00:00+00:00",
                    "dateEnd": f"{today.isoformat()}T23:59:59+00:00",
                    "offset": offset,
                    "limit": page_size,
                },
            )
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"The receipt request failed: {exc}")
            break

        orders = _orders_of(payload)
        if not orders:
            break
        for raw in orders:
            import_receipt(session, owner_id, raw, report)
        if len(orders) < page_size:
            break
        offset += len(orders)

    return report


def _orders_of(payload) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("orders", "items", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def _exact_moment(value) -> dt.datetime | None:
    """The purchase time, or None. There is no guessing here."""
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    return None


def _parse_moment(value) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return utcnow()
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    return utcnow()
