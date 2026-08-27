import base64
from unittest.mock import patch

import pytest
from sqlalchemy import inspect, select

from app.database import SessionLocal, engine
from app.models import Address, Contact
from app.schemas import (
    MAX_PHOTO_BYTES,
    MAX_PHOTO_DATA_URI_CHARS,
    _validate_photo_data_uri,
)


BASE = "/api/v1/contacts"


def address(address_type: str, street: str = "1 Market St") -> dict:
    return {
        "type": address_type,
        "address": street,
        "city": "San Francisco",
        "state": "CA",
        "postal_code": "94105",
        "country": "USA",
    }


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "sqlite"


def test_create_contact(client, payload):
    response = client.post(BASE, json=payload)
    assert response.status_code == 201
    body = response.json()
    assert body["id"] > 0
    assert body["email"] == "ada@example.com"
    assert body["full_name"] == "Ada Lovelace"
    assert body["created_at"] and body["updated_at"]


def test_create_requires_valid_email(client, payload):
    response = client.post(BASE, json={**payload, "email": "not-an-email"})
    assert response.status_code == 422


def test_create_requires_names(client, payload):
    response = client.post(BASE, json={**payload, "first_name": ""})
    assert response.status_code == 422


def test_duplicate_email_conflicts(client, payload):
    assert client.post(BASE, json=payload).status_code == 201
    response = client.post(BASE, json={**payload, "email": "ADA@example.com"})
    assert response.status_code == 409


def test_get_contact(client, payload):
    contact_id = client.post(BASE, json=payload).json()["id"]
    response = client.get(f"{BASE}/{contact_id}")
    assert response.status_code == 200
    assert response.json()["id"] == contact_id


def test_create_contact_with_multiple_addresses(client, payload):
    addresses = [address("Home"), address("Work", "88 Market St")]

    response = client.post(BASE, json={**payload, "addresses": addresses})

    assert response.status_code == 201
    body = response.json()
    assert [item["type"] for item in body["addresses"]] == ["Home", "Work"]
    assert all(item["id"] > 0 for item in body["addresses"])

    with SessionLocal() as db:
        rows = db.scalars(select(Address).where(Address.contact_id == body["id"])).all()
        assert len(rows) == 2
        assert {row.type.value for row in rows} == {"Home", "Work"}


@pytest.mark.parametrize("address_type", ["Home", "Work", "Other"])
def test_accepts_each_address_type(client, payload, address_type):
    response = client.post(BASE, json={**payload, "addresses": [address(address_type)]})
    assert response.status_code == 201
    assert response.json()["addresses"][0]["type"] == address_type


def test_rejects_invalid_address_type(client, payload):
    response = client.post(BASE, json={**payload, "addresses": [address("Vacation")]})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][-1] == "type"


def test_rejects_blank_street_address(client, payload):
    response = client.post(BASE, json={**payload, "addresses": [address("Home", "   ")]})
    assert response.status_code == 422
    assert "must not be blank" in response.json()["detail"][0]["msg"]


def test_get_and_list_return_nested_addresses(client, payload):
    addresses = [address("Home"), address("Other", "PO Box 42")]
    contact_id = client.post(BASE, json={**payload, "addresses": addresses}).json()["id"]

    fetched = client.get(f"{BASE}/{contact_id}").json()
    listed = client.get(BASE).json()["items"][0]

    assert [item["address"] for item in fetched["addresses"]] == ["1 Market St", "PO Box 42"]
    assert listed["addresses"] == fetched["addresses"]


def test_contact_with_no_addresses_is_valid(client, payload):
    response = client.post(BASE, json=payload)
    assert response.status_code == 201
    assert response.json()["addresses"] == []


def test_addresses_table_has_contact_foreign_key(client):
    foreign_keys = inspect(engine).get_foreign_keys("addresses")
    assert any(
        key["referred_table"] == "contacts"
        and key["constrained_columns"] == ["contact_id"]
        and key["options"].get("ondelete") == "CASCADE"
        for key in foreign_keys
    )
    relationship = inspect(Contact).relationships["addresses"]
    assert relationship.lazy == "selectin"
    assert relationship.cascade.delete_orphan


def test_photo_is_persisted_and_returned(client, payload, photo_data_uri):
    created = client.post(BASE, json={**payload, "photo": photo_data_uri})
    assert created.status_code == 201
    contact_id = created.json()["id"]
    assert created.json()["photo"] == photo_data_uri

    assert client.get(f"{BASE}/{contact_id}").json()["photo"] == photo_data_uri
    assert client.get(BASE).json()["items"][0]["photo"] == photo_data_uri


@pytest.mark.parametrize(
    "photo",
    [
        "not-a-data-uri",
        "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==",
        "data:image/png;base64,not-valid-base64!",
        "data:image/jpeg;base64,iVBORw0KGgoAAAANSUhEUg==",
    ],
)
def test_rejects_invalid_photos(client, payload, photo):
    response = client.post(BASE, json={**payload, "photo": photo})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][-1] == "photo"


def test_rejects_photo_larger_than_limit(client, payload):
    oversized_png = b"\x89PNG\r\n\x1a\n" + b"x" * (MAX_PHOTO_BYTES - 7)
    photo = f"data:image/png;base64,{base64.b64encode(oversized_png).decode()}"

    response = client.post(BASE, json={**payload, "photo": photo})
    assert response.status_code == 422
    assert "2 MB or smaller" in response.json()["detail"][0]["msg"]


def test_rejects_oversized_data_uri_before_decoding():
    photo = "data:image/png;base64," + "A" * (MAX_PHOTO_DATA_URI_CHARS + 1)

    with patch("app.schemas.base64.b64decode") as decode:
        with pytest.raises(ValueError, match="2 MB or smaller"):
            _validate_photo_data_uri(photo)

    decode.assert_not_called()


def test_get_missing_contact_returns_404(client):
    assert client.get(f"{BASE}/9999").status_code == 404


def test_list_pagination_and_total(client, payload):
    for index in range(5):
        client.post(BASE, json={**payload, "email": f"user{index}@example.com"})

    response = client.get(BASE, params={"limit": 2, "offset": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2
    assert body["limit"] == 2 and body["offset"] == 2


def test_list_search(client, payload):
    client.post(BASE, json=payload)
    client.post(
        BASE,
        json={**payload, "first_name": "Grace", "last_name": "Hopper", "email": "grace@example.com", "company": "US Navy"},
    )

    hits = client.get(BASE, params={"search": "hopper"}).json()
    assert hits["total"] == 1
    assert hits["items"][0]["last_name"] == "Hopper"

    by_company = client.get(BASE, params={"search": "navy"}).json()
    assert by_company["total"] == 1

    misses = client.get(BASE, params={"search": "nobody"}).json()
    assert misses["total"] == 0


def test_list_sorting(client, payload):
    client.post(BASE, json={**payload, "last_name": "Zhang", "email": "z@example.com"})
    client.post(BASE, json={**payload, "last_name": "Adams", "email": "a@example.com"})

    names = [
        item["last_name"]
        for item in client.get(BASE, params={"sort_by": "last_name", "order": "asc"}).json()["items"]
    ]
    assert names == ["Adams", "Zhang"]


def test_list_rejects_bad_sort_field(client):
    assert client.get(BASE, params={"sort_by": "; DROP TABLE contacts"}).status_code == 422


def test_patch_updates_only_sent_fields(client, payload, photo_data_uri):
    original_addresses = [address("Home"), address("Work", "88 Market St")]
    created = client.post(
        BASE,
        json={**payload, "photo": photo_data_uri, "addresses": original_addresses},
    ).json()
    contact_id = created["id"]
    response = client.patch(f"{BASE}/{contact_id}", json={"phone": "+1-000-000-0000"})
    assert response.status_code == 200
    body = response.json()
    assert body["phone"] == "+1-000-000-0000"
    assert body["first_name"] == "Ada"
    assert body["company"] == "Analytical Engines"
    assert body["photo"] == photo_data_uri
    assert [item["type"] for item in body["addresses"]] == ["Home", "Work"]
    assert [item["id"] for item in body["addresses"]] == [
        item["id"] for item in created["addresses"]
    ]


def test_patch_with_addresses_replaces_collection(client, payload):
    created = client.post(
        BASE,
        json={**payload, "addresses": [address("Home"), address("Work", "88 Market St")]},
    ).json()
    old_ids = {item["id"] for item in created["addresses"]}

    response = client.patch(
        f"{BASE}/{created['id']}",
        json={"addresses": [address("Other", "PO Box 42")]},
    )

    assert response.status_code == 200
    assert [item["type"] for item in response.json()["addresses"]] == ["Other"]
    assert response.json()["updated_at"] != created["updated_at"]
    with SessionLocal() as db:
        assert db.scalars(select(Address).where(Address.id.in_(old_ids))).all() == []
        rows = db.scalars(select(Address).where(Address.contact_id == created["id"])).all()
        assert len(rows) == 1


def test_patch_duplicate_email_conflicts(client, payload):
    first = client.post(BASE, json=payload).json()["id"]
    client.post(BASE, json={**payload, "email": "grace@example.com"})
    response = client.patch(f"{BASE}/{first}", json={"email": "grace@example.com"})
    assert response.status_code == 409


def test_patch_same_email_is_allowed(client, payload):
    contact_id = client.post(BASE, json=payload).json()["id"]
    response = client.patch(f"{BASE}/{contact_id}", json={"email": payload["email"]})
    assert response.status_code == 200


def test_put_replaces_contact(client, payload):
    contact_id = client.post(BASE, json=payload).json()["id"]
    response = client.put(
        f"{BASE}/{contact_id}",
        json={"first_name": "Grace", "last_name": "Hopper", "email": "grace@example.com"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["full_name"] == "Grace Hopper"
    assert body["company"] is None  # omitted fields are cleared by PUT
    assert body["addresses"] == []


def test_put_replaces_address_collection_without_orphans(client, payload):
    created = client.post(
        BASE,
        json={**payload, "addresses": [address("Home"), address("Work", "88 Market St")]},
    ).json()
    old_ids = {item["id"] for item in created["addresses"]}

    response = client.put(
        f"{BASE}/{created['id']}",
        json={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.com",
            "addresses": [address("Home", "2 Main St"), address("Other", "PO Box 42")],
        },
    )

    assert response.status_code == 200
    assert [item["type"] for item in response.json()["addresses"]] == ["Home", "Other"]
    with SessionLocal() as db:
        assert db.scalars(select(Address).where(Address.id.in_(old_ids))).all() == []
        rows = db.scalars(select(Address).where(Address.contact_id == created["id"])).all()
        assert len(rows) == 2


def test_put_persists_photo_when_included(client, payload, photo_data_uri):
    contact_id = client.post(BASE, json={**payload, "photo": photo_data_uri}).json()["id"]
    response = client.put(
        f"{BASE}/{contact_id}",
        json={
            "first_name": "Grace",
            "last_name": "Hopper",
            "email": "grace@example.com",
            "photo": photo_data_uri,
        },
    )

    assert response.status_code == 200
    assert response.json()["photo"] == photo_data_uri


def test_put_missing_contact_returns_404(client):
    response = client.put(
        f"{BASE}/9999",
        json={"first_name": "A", "last_name": "B", "email": "ab@example.com"},
    )
    assert response.status_code == 404


def test_delete_contact(client, payload):
    contact_id = client.post(
        BASE,
        json={**payload, "addresses": [address("Home"), address("Work", "88 Market St")]},
    ).json()["id"]
    assert client.delete(f"{BASE}/{contact_id}").status_code == 204
    assert client.get(f"{BASE}/{contact_id}").status_code == 404
    assert client.delete(f"{BASE}/{contact_id}").status_code == 404
    with SessionLocal() as db:
        assert db.scalars(select(Address).where(Address.contact_id == contact_id)).all() == []


def test_root_lists_entrypoints(client):
    body = client.get("/").json()
    assert body["contacts"] == BASE
