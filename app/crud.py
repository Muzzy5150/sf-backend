from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Address, Contact, CryptoWallet
from app.schemas import AddressInput, ContactCreate, ContactReplace, ContactUpdate, CryptoWalletInput

SORTABLE_FIELDS = ("id", "first_name", "last_name", "email", "company", "created_at", "updated_at")


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _address_models(addresses: list[AddressInput]) -> list[Address]:
    return [Address(**address.model_dump()) for address in addresses]


def _wallet_models(wallets: list[CryptoWalletInput]) -> list[CryptoWallet]:
    return [CryptoWallet(**wallet.model_dump()) for wallet in wallets]


def get_contact(db: Session, contact_id: int) -> Contact | None:
    return db.get(Contact, contact_id)


def get_contact_by_email(db: Session, email: str) -> Contact | None:
    stmt = select(Contact).where(func.lower(Contact.email) == _normalize_email(email))
    return db.execute(stmt).scalar_one_or_none()


def count_contacts(db: Session) -> int:
    return db.execute(select(func.count()).select_from(Contact)).scalar_one()


def list_contacts(
    db: Session,
    *,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "id",
    order: str = "asc",
) -> tuple[list[Contact], int]:
    """Return (page of contacts, total matching count)."""
    stmt = select(Contact)

    if search:
        pattern = f"%{search.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Contact.first_name).like(pattern),
                func.lower(Contact.last_name).like(pattern),
                func.lower(Contact.email).like(pattern),
                func.lower(func.coalesce(Contact.company, "")).like(pattern),
                func.lower(func.coalesce(Contact.phone, "")).like(pattern),
            )
        )

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()

    if sort_by not in SORTABLE_FIELDS:
        sort_by = "id"
    column = getattr(Contact, sort_by)
    stmt = stmt.order_by(column.desc() if order == "desc" else column.asc())

    items = db.execute(stmt.limit(limit).offset(offset)).scalars().all()
    return list(items), total


def create_contact(db: Session, payload: ContactCreate) -> Contact:
    data = payload.model_dump(exclude={"addresses", "crypto_wallets"})
    data["email"] = _normalize_email(data["email"])
    contact = Contact(
        **data,
        addresses=_address_models(payload.addresses),
        crypto_wallets=_wallet_models(payload.crypto_wallets),
    )
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return contact


def replace_contact(db: Session, contact: Contact, payload: ContactReplace) -> Contact:
    for field, value in payload.model_dump(exclude={"addresses", "crypto_wallets"}).items():
        setattr(contact, field, _normalize_email(value) if field == "email" else value)
    contact.addresses = _address_models(payload.addresses)
    contact.crypto_wallets = _wallet_models(payload.crypto_wallets)
    # Relationship-only changes do not automatically update a column on the
    # parent row, so explicitly mark the contact's modification time.
    contact.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(contact)
    return contact


def update_contact(db: Session, contact: Contact, payload: ContactUpdate) -> Contact:
    changes = payload.model_dump(exclude_unset=True, exclude={"addresses", "crypto_wallets"})
    for field, value in changes.items():
        setattr(contact, field, _normalize_email(value) if field == "email" else value)
    if "addresses" in payload.model_fields_set:
        contact.addresses = _address_models(payload.addresses)
        contact.updated_at = datetime.now(timezone.utc)
    if "crypto_wallets" in payload.model_fields_set:
        contact.crypto_wallets = _wallet_models(payload.crypto_wallets)
        contact.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(contact)
    return contact


def delete_contact(db: Session, contact: Contact) -> None:
    db.delete(contact)
    db.commit()
