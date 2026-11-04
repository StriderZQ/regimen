"""Receipt importer tests: weights, non-food, duplicates, unknown lines.

Live OAuth is not exercised here — it does not exist before deployment. Everything else
is: parsing a receipt, deduplication, and an unknown line never being guessed.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from app.domain import pantry
from app.integrations.silpo import importer
from app.integrations.silpo.client import SilpoClient, SilpoError, Token, load_token, save_token
from app.models import CanonicalProduct, LineStatus, Principal, Receipt, ReceiptLine, StoreItem
from app.models.pantry import StoragePlace


def _catalogue(session: Session, owner: Principal) -> StoreItem:
    product = CanonicalProduct(
        owner_id=owner.id, slug="tvorog-5", name="Творог 5%", category="творог",
        kcal_100=121, protein_100=16, fat_100=5, carbs_100=3, is_protein_base=True,
    )
    session.add(product)
    session.flush()
    item = StoreItem(
        owner_id=owner.id, canonical_id=product.id, store="silpo", external_id="716286",
        name="Сир кисломолочний Молокія 5%", pack_grams=350,
    )
    session.add(item)
    session.flush()
    return item


def _receipt(**overrides) -> dict:
    base = {
        "id": "R-1",
        "date": "2026-09-05T16:33:27",
        "store": "вул. Прикладна, 1",
        "total": 91.0,
        "items": [
            {"lager_id": 716286, "name": "Сир кисломолочний Молокія 5%", "unit": "350г",
             "qty": 2, "price": 60.0},
        ],
    }
    base.update(overrides)
    return base


# --- weight parsing ---------------------------------------------------------------------

@pytest.mark.parametrize(
    ("unit", "qty", "expected"),
    [
        ("кг", 0.608, 608.0),
        ("350г", 2, 700.0),
        ("1,5л", 1, 1500.0),
        ("160г", 4, 640.0),
        ("л", 1, 1000.0),
        ("шт", 3, None),       # вес штуки неизвестен — не выдумываем
        ("", 1, None),
    ],
)
def test_grams_from_unit(unit, qty, expected):
    assert importer.grams_of(unit, qty) == expected


def test_pieces_need_pack_weight():
    assert importer.grams_of("шт", 2, pack_grams=90) == 180.0


def test_non_food_is_recognised():
    assert not importer.looks_like_food("Пакет біорозкладний 3кг")
    assert not importer.looks_like_food("Засіб для прання Perwoll")
    assert importer.looks_like_food("Сир кисломолочний Молокія 5%")


def test_place_follows_the_product():
    assert importer.place_for("Філе куряче охолоджене") is StoragePlace.FRIDGE
    assert importer.place_for("Овочі заморожені") is StoragePlace.FREEZER
    assert importer.place_for("Олія оливкова") is StoragePlace.PANTRY


# --- import -------------------------------------------------------------------------------

def test_known_item_becomes_stock(session, owner):
    item = _catalogue(session, owner)
    report = importer.ImportReport()

    importer.import_receipt(session, owner.id, _receipt(), report)

    assert report.receipts_new == 1
    assert report.batches == 1
    line = session.query(ReceiptLine).one()
    assert line.status is LineStatus.MATCHED
    assert pantry.batch_balance(session, line.batch_id) == 700.0
    assert session.get(type(item), item.id) is item


def test_second_import_creates_nothing(session, owner):
    """A retry after a timeout has no right to enter the purchases twice."""
    _catalogue(session, owner)
    first = importer.ImportReport()
    importer.import_receipt(session, owner.id, _receipt(), first)

    second = importer.ImportReport()
    importer.import_receipt(session, owner.id, _receipt(), second)

    assert second.receipts_new == 0
    assert second.receipts_seen == 1
    assert session.query(Receipt).count() == 1
    assert session.query(ReceiptLine).count() == 1


def test_household_chemicals_never_reach_the_pantry(session, owner):
    report = importer.ImportReport()
    importer.import_receipt(
        session, owner.id,
        _receipt(items=[{"name": "Засіб для прання Perwoll", "unit": "шт", "qty": 1,
                         "price": 200.0}]),
        report,
    )
    line = session.query(ReceiptLine).one()
    assert line.is_food is False
    assert line.status is LineStatus.NOT_FOOD
    assert report.batches == 0
    assert pantry.list_stock(session, owner.id) == []


def test_unknown_line_waits_for_an_answer(session, owner):
    """An unknown line is not guessed — it waits for the owner."""
    report = importer.ImportReport()
    importer.import_receipt(
        session, owner.id,
        _receipt(items=[{"lager_id": 999, "name": "Кавун", "unit": "кг", "qty": 3.322,
                         "price": 16.99}]),
        report,
    )
    line = session.query(ReceiptLine).one()
    assert line.status is LineStatus.PENDING
    assert report.batches == 0
    assert "Кавун" in report.pending


def test_answer_is_remembered_forever(session, owner):
    """Said once that this is cottage cheese — never asked again."""
    item = _catalogue(session, owner)
    raw = "Сир кисломолочний 5% домашній"
    importer.remember_alias(session, owner.id, raw_name=raw, store_item=item)

    resolved = importer.resolve_item(session, owner.id, raw_name=raw, external_id=None)
    assert resolved is not None and resolved.id == item.id

    # Case and spacing must not break the matching
    again = importer.resolve_item(
        session, owner.id, raw_name="  СИР КИСЛОМОЛОЧНИЙ 5% ДОМАШНІЙ ", external_id=None
    )
    assert again is not None and again.id == item.id


def test_sku_beats_name(session, owner):
    """The shop renamed a product — the article number still recognises it."""
    item = _catalogue(session, owner)
    resolved = importer.resolve_item(
        session, owner.id, raw_name="Совершенно другое название", external_id="716286"
    )
    assert resolved.id == item.id


def test_weightless_line_is_not_invented(session, owner):
    """Pieces with no package weight: no batch is created, the line waits for an answer."""
    product = CanonicalProduct(owner_id=owner.id, slug="x", name="Что-то", category="прочее")
    session.add(product)
    session.flush()
    session.add(StoreItem(owner_id=owner.id, canonical_id=product.id, store="silpo",
                          external_id="555", name="Что-то штучное"))
    session.flush()

    report = importer.ImportReport()
    importer.import_receipt(
        session, owner.id,
        _receipt(items=[{"lager_id": 555, "name": "Что-то штучное", "unit": "шт", "qty": 2}]),
        report,
    )
    line = session.query(ReceiptLine).one()
    assert line.status is LineStatus.PENDING
    assert "the owner must answer" in (line.note or "")


def test_receipt_without_id_is_reported_not_guessed(session, owner):
    report = importer.ImportReport()
    result = importer.import_receipt(session, owner.id, {"items": []}, report)
    assert result is None
    assert report.errors


# --- syncing through a stub client ---------------------------------------------------------

class _FakeClient:
    """A stub with the real shape of the shop's responses, taken from the live API."""

    def __init__(self, pages, *, cart=True) -> None:
        self.pages = list(pages)
        self.cart = cart
        self.calls: list[dict] = []

    def call(self, tool, arguments=None):
        self.calls.append({"tool": tool, "args": arguments})
        if tool == "silpo_get_my_shopping_cart":
            return {"success": True, "shoppingCartId": "cart-1", "exists": self.cart} \
                if self.cart else {"success": True, "shoppingCartId": None, "exists": False}
        if tool == "silpo_get_shopping_cart_by_id":
            return {
                "cart": {
                    "deliveryType": "DeliveryHome",
                    "timeslot": {"start": "2026-09-12T06:00:00+00:00",
                                 "end": "2026-09-12T07:30:00+00:00"},
                    "shipments": [{"branchId": "branch-1"}],
                }
            }
        return self.pages.pop(0) if self.pages else {"orders": []}


def _order(**overrides) -> dict:
    """A receipt as the shop returns it: no id, with a branch id and a creation time."""
    base = {
        "filId": 3362,
        "filialName": "вул. Прикладна, буд. 1",
        "createdAt": "2026-09-11T13:45:47",
        "sumReg": 259.0,
        "sumDiscount": 32.4,
        "accruedBalaBonusesSum": 1.91,
        "receiptUrl": "https://receipt.example/secret-link",
        "products": [
            {"lagerId": 838497, "name": "Сир кисломолочний Молокія 5%", "unit": "350г",
             "quantity": 1, "price": 67.99},
        ],
    }
    base.update(overrides)
    return base


def test_sync_asks_the_store_for_its_context_first(session, owner):
    """Without a branch and a slot the shop returns no history — both come from the basket."""
    _catalogue(session, owner)
    client = _FakeClient([{"orders": [_order()]}])

    report = importer.sync(session, owner.id, client)

    assert [c["tool"] for c in client.calls][:2] == [
        "silpo_get_my_shopping_cart", "silpo_get_shopping_cart_by_id"
    ]
    receipts_call = client.calls[-1]
    assert receipts_call["args"]["branchId"] == "branch-1"
    assert receipts_call["args"]["limit"] <= 10  # магазин больше десяти не отдаёт
    assert report.receipts_new == 1


def test_missing_cart_is_explained_not_crashed(session, owner):
    report = importer.sync(session, owner.id, _FakeClient([], cart=False))
    assert report.receipts_new == 0
    assert any("basket" in e.lower() for e in report.errors)


def test_receipt_url_is_never_stored(session, owner):
    """A public receipt link opens for anyone who holds it."""
    _catalogue(session, owner)
    importer.sync(session, owner.id, _FakeClient([{"orders": [_order()]}]))

    receipt = session.query(Receipt).one()
    assert "secret-link" not in repr(receipt.__dict__)


def test_repeat_receipt_is_recognised_without_an_id(session, owner):
    """The shop gives no receipt id — we identify it by branch and time."""
    _catalogue(session, owner)
    importer.sync(session, owner.id, _FakeClient([{"orders": [_order()]}]))
    second = importer.sync(session, owner.id, _FakeClient([{"orders": [_order()]}]))

    assert second.receipts_new == 0
    assert second.receipts_seen == 1
    assert session.query(Receipt).count() == 1


def test_sync_walks_pages_until_short_page(session, owner):
    _catalogue(session, owner)
    full = {"orders": [_order(createdAt=f"2026-09-0{i}T10:00:00") for i in range(1, 10)]
            + [_order(createdAt="2026-09-10T10:00:00")]}
    client = _FakeClient([full, {"orders": [_order(createdAt="2026-08-30T10:00:00")]}])

    report = importer.sync(session, owner.id, client)

    assert report.receipts_new == 11
    receipt_calls = [c for c in client.calls if c["tool"] == "silpo_get_my_offline_orders"]
    assert len(receipt_calls) == 2
    assert receipt_calls[1]["args"]["offset"] == 10


def test_sync_reports_failure_instead_of_crashing(session, owner):
    class _Broken:
        def call(self, *_a, **_kw):
            raise SilpoError("сеть недоступна")

    report = importer.sync(session, owner.id, _Broken())
    assert report.errors and "сеть недоступна" in report.errors[0]


# --- the token ------------------------------------------------------------------------------

def test_token_roundtrip_is_atomic_and_private(tmp_path):
    path = tmp_path / "silpo-token.json"
    token = Token(
        access_token="a", refresh_token="r",
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
        token_url="https://example/token", client_id="cid",
    )
    save_token(token, path)

    assert oct(path.stat().st_mode)[-3:] == "600"
    assert not (tmp_path / "silpo-token.tmp").exists()
    loaded = load_token(path)
    assert loaded.access_token == "a"
    assert loaded.refresh_token == "r"
    assert not loaded.expired


def test_expired_token_is_seen_as_expired():
    token = Token(access_token="a", expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5))
    assert token.expired


def test_missing_token_says_a_human_is_needed(tmp_path):
    from app.integrations.silpo.client import SilpoAuthError

    with pytest.raises(SilpoAuthError, match="A human passes the grocery authorisation"):
        load_token(tmp_path / "нет-такого.json")


def test_refresh_without_refresh_token_is_an_auth_error(tmp_path):
    from app.integrations.silpo.client import SilpoAuthError

    path = tmp_path / "t.json"
    save_token(Token(access_token="a"), path)
    client = SilpoClient(token=load_token(path), token_file=path, http=object())
    with pytest.raises(SilpoAuthError, match="refresh_token"):
        client._refresh()


# --- macros from the shop's product cards -----------------------------------------------------

CHICKEN_ATTRS = {
    "Склад": "куряче філе 100%",
    "Енергетична цінність (кКал/кДЖ)": "166/697",
    "Білки (г)": 20.8,
    "Жири (г)": 9.2,
}


def test_nutrition_is_parsed_from_store_attributes():
    """The shape is taken from a live response."""
    macros = importer.nutrition_from(CHICKEN_ATTRS)
    assert macros == {"kcal_100": 166.0, "protein_100": 20.8, "fat_100": 9.2}


def test_energy_is_taken_in_kilocalories_not_kilojoules():
    """"166/697" — kilocalories first. Taking the second number overstates it fourfold."""
    assert importer.nutrition_from(
        {"Енергетична цінність (кКал/кДЖ)": "116/485"}
    )["kcal_100"] == 116.0


def test_attributes_without_nutrition_give_nothing():
    assert importer.nutrition_from({"Країна": "Україна"}) == {}
    assert importer.nutrition_from(None) == {}


class _CatalogueClient:
    """A shop with a product card and a free delivery slot."""

    def __init__(self, attributes=CHICKEN_ATTRS, slot_available=True) -> None:
        self.attributes = attributes
        self.slot_available = slot_available

    def call(self, tool, arguments=None):
        if tool == "silpo_get_my_shopping_cart":
            return {"shoppingCartId": "cart-1", "exists": True}
        if tool == "silpo_get_shopping_cart_by_id":
            return {"cart": {"deliveryType": "DeliveryHome",
                             "timeslot": {"start": "2026-09-12T06:00:00+00:00",
                                          "end": "2026-09-12T07:30:00+00:00"},
                             "shipments": [{"branchId": "branch-1"}]}}
        if tool == "silpo_get_time_slots":
            return {"slots": [{"start": "2026-09-13T07:30:00+00:00",
                               "end": "2026-09-13T09:00:00+00:00",
                               "available": self.slot_available}]}
        if tool == "silpo_find_products_batch":
            return {"queries": [{"products": [{"slug": "kuriache-file-549339"}]}]}
        if tool == "silpo_get_product_details":
            return {"product": {"attributes": self.attributes}}
        return {}


def test_store_declaration_lands_on_the_item_not_the_product(session, owner):
    """Chicken is sold with the skin and eaten without: two different honest numbers."""
    item = _catalogue(session, owner)
    item.canonical.name = "Куриное филе"
    item.canonical.kcal_100 = 110
    item.canonical.protein_100 = 23
    item.canonical.fat_100 = 1.8
    item.canonical.prep_note = "покупается с кожей, кожа снимается"
    session.flush()

    report = importer.enrich_nutrition(session, owner.id, _CatalogueClient())

    assert report["updated"] == 1
    assert item.kcal_100 == 166.0          # как продано
    assert item.canonical.kcal_100 == 110  # как съедено — не тронуто
    assert report["conflicts"], "расхождение обязано быть названо"
    assert "кожей" in report["conflicts"][0]["prep_note"]


def test_product_without_any_figures_takes_the_store_declaration(session, owner):
    item = _catalogue(session, owner)
    item.canonical.kcal_100 = None
    item.canonical.protein_100 = None
    item.canonical.fat_100 = None
    session.flush()

    importer.enrich_nutrition(session, owner.id, _CatalogueClient())

    assert item.canonical.protein_100 == 20.8
    assert item.canonical.nutrition_source is not None


def test_without_a_free_timeslot_enrichment_says_so(session, owner):
    """With a stale slot the shop answers with nothing, whatever is asked."""
    _catalogue(session, owner)
    report = importer.enrich_nutrition(
        session, owner.id, _CatalogueClient(slot_available=False)
    )
    assert report["updated"] == 0
    assert any("slot" in e for e in report["errors"])
