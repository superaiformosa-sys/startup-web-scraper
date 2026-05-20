import json
import logging
from google.cloud import firestore
from google.oauth2.service_account import Credentials
from config import GOOGLE_CREDENTIALS_JSON

logger = logging.getLogger(__name__)

_db = None


def get_db() -> firestore.Client:
    global _db
    if _db is None:
        creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(
            creds_info,
            scopes=["https://www.googleapis.com/auth/datastore"],
        )
        _db = firestore.Client(
            project=creds_info["project_id"],
            credentials=creds,
        )
    return _db


def firestore_write(collection: str, data: dict, doc_id: str | None = None) -> str:
    db = get_db()
    col_ref = db.collection(collection)
    if doc_id:
        doc_ref = col_ref.document(doc_id)
        doc_ref.set(data)
        logger.info("✅ Firestore set: %s/%s", collection, doc_id)
        return doc_id
    else:
        _, doc_ref = col_ref.add(data)
        logger.info("✅ Firestore add: %s/%s", collection, doc_ref.id)
        return doc_ref.id


def firestore_query(collection: str, filters: list[tuple] | None = None, order_by: str | None = None, limit: int = 50) -> list[dict]:
    db = get_db()
    q = db.collection(collection)
    if filters:
        for field, op, val in filters:
            q = q.where(field, op, val)
    if order_by:
        q = q.order_by(order_by, direction=firestore.Query.DESCENDING)
    if limit:
        q = q.limit(limit)
    return [doc.to_dict() for doc in q.stream()]
