"""
Web Push subscriptions — ek user ke ek ya zyada device/browser endpoints.
Additive: naya table, create_all se auto ban jaata hai.
"""
from sqlalchemy import Column, Integer, String, Text, DateTime, func, Index
from database import Base


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"
    id         = Column(Integer, primary_key=True)
    user_id    = Column(Integer, index=True)
    endpoint   = Column(Text)                 # push service endpoint (unique-ish per device)
    p256dh     = Column(String(255))          # client public key
    auth       = Column(String(255))          # client auth secret
    ua         = Column(String(255), default="")
    created_at = Column(DateTime, default=func.now())

    __table_args__ = (Index("ix_push_user", "user_id"),)
