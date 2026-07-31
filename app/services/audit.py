from sqlalchemy.orm import Session
from ..models import AuditLog


def record(db: Session, entity_type: str, entity_id: int, action: str,
           actor: str | None = None, details: str | None = None) -> None:
    db.add(AuditLog(entity_type=entity_type, entity_id=entity_id,
                     action=action, actor=actor, details=details))
    db.commit()
