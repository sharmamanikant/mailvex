from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Campaign,
    Complaint,
    Contact,
    Domain,
    EmailAccount,
    Message,
    Reply,
    Thread,
)

POSITIVE_REPLY_CLASSIFICATIONS = {"INTERESTED", "MEETING_REQUEST", "NEEDS_INFORMATION", "SEND_DETAILS", "OTHER"}


class AnalyticsService:
    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    def _count(self, model, *conditions, **filters) -> int:
        stmt = select(func.count()).select_from(model).where(*self._tenant_filters(model, *conditions, **filters))
        return int(self.session.scalar(stmt) or 0)

    def _tenant_filters(self, model, *conditions, **filters):
        clauses = [model.tenant_id == self.tenant_id]
        clauses.extend(conditions)
        clauses.extend(filter for filter in filters.values())
        return clauses

    def metrics(self) -> dict[str, int]:
        campaigns = self._count(Campaign)
        recipients = self._count(Contact)
        queued = self._count(Message, Message.status == "QUEUED")
        sent = self.session.scalar(
            select(func.count()).select_from(Message).where(
                Message.tenant_id == self.tenant_id,
                Message.status.in_(("SENT", "ACCEPTED")),
            )
        ) or 0
        provider_accepted = self._count(Message, Message.status == "ACCEPTED")
        bounced = self.session.scalar(select(func.count()).select_from(Message).where(Message.tenant_id == self.tenant_id, Message.status == "BOUNCED")) or 0
        bounced = int(bounced)
        failed = self._count(Message, Message.status == "FAILED")
        replies = self._count(Reply)
        positive_replies = self.session.scalar(
            select(func.count()).select_from(Reply).where(
                Reply.tenant_id == self.tenant_id,
                Reply.classification.in_(tuple(POSITIVE_REPLY_CLASSIFICATIONS)),
            )
        ) or 0
        unsubscribes = self._count(Reply, Reply.classification == "UNSUBSCRIBE")
        complaints = self._count(Complaint)
        return {
            "campaigns": int(campaigns),
            "recipients": int(recipients),
            "queued": int(queued),
            "sent": int(sent),
            "provider_accepted": int(provider_accepted),
            "bounced": int(bounced),
            "failed": int(failed),
            "replies": int(replies),
            "positive_replies": int(positive_replies),
            "unsubscribes": int(unsubscribes),
            "complaints": int(complaints),
        }

    def campaign_report(self) -> list[dict[str, object]]:
        campaigns = self.session.scalars(select(Campaign).where(Campaign.tenant_id == self.tenant_id).order_by(Campaign.created_at.desc())).all()
        rows: list[dict[str, object]] = []
        for campaign in campaigns:
            sent = self.session.scalar(select(func.count()).select_from(Message).where(Message.tenant_id == self.tenant_id, Message.campaign_id == campaign.id, Message.status.in_(("SENT", "ACCEPTED")))) or 0
            accepted = self.session.scalar(select(func.count()).select_from(Message).where(Message.tenant_id == self.tenant_id, Message.campaign_id == campaign.id, Message.status == "ACCEPTED")) or 0
            queued = self.session.scalar(select(func.count()).select_from(Message).where(Message.tenant_id == self.tenant_id, Message.campaign_id == campaign.id, Message.status == "QUEUED")) or 0
            failed = self.session.scalar(select(func.count()).select_from(Message).where(Message.tenant_id == self.tenant_id, Message.campaign_id == campaign.id, Message.status == "FAILED")) or 0
            replies = self.session.scalar(
                select(func.count()).select_from(Reply).join(Thread, Thread.id == Reply.thread_id).where(
                    Reply.tenant_id == self.tenant_id,
                    Thread.campaign_id == campaign.id,
                )
            ) or 0
            positive = self.session.scalar(
                select(func.count()).select_from(Reply).join(Thread, Thread.id == Reply.thread_id).where(
                    Reply.tenant_id == self.tenant_id,
                    Thread.campaign_id == campaign.id,
                    Reply.classification.in_(tuple(POSITIVE_REPLY_CLASSIFICATIONS)),
                )
            ) or 0
            unsubscribes = self.session.scalar(
                select(func.count()).select_from(Reply).join(Thread, Thread.id == Reply.thread_id).where(
                    Reply.tenant_id == self.tenant_id,
                    Thread.campaign_id == campaign.id,
                    Reply.classification == "UNSUBSCRIBE",
                )
            ) or 0
            complaints = self.session.scalar(
                select(func.count()).select_from(Complaint).join(Message, Message.id == Complaint.message_id).where(
                    Complaint.tenant_id == self.tenant_id,
                    Message.campaign_id == campaign.id,
                )
            ) or 0
            rows.append(
                {
                    "campaign_id": str(campaign.id),
                    "campaign_name": campaign.name,
                    "status": campaign.status,
                    "queued": int(queued),
                    "sent": int(sent),
                    "provider_accepted": int(accepted),
                    "failed": int(failed),
                    "replies": int(replies),
                    "positive_replies": int(positive),
                    "unsubscribes": int(unsubscribes),
                    "complaints": int(complaints),
                }
            )
        return rows

    def sender_report(self) -> list[dict[str, object]]:
        senders = self.session.scalars(select(EmailAccount).where(EmailAccount.tenant_id == self.tenant_id).order_by(EmailAccount.email)).all()
        rows: list[dict[str, object]] = []
        for sender in senders:
            sent = self.session.scalar(select(func.count()).select_from(Message).where(Message.tenant_id == self.tenant_id, Message.sender_id == sender.id, Message.status.in_(("SENT", "ACCEPTED")))) or 0
            accepted = self.session.scalar(select(func.count()).select_from(Message).where(Message.tenant_id == self.tenant_id, Message.sender_id == sender.id, Message.status == "ACCEPTED")) or 0
            queued = self.session.scalar(select(func.count()).select_from(Message).where(Message.tenant_id == self.tenant_id, Message.sender_id == sender.id, Message.status == "QUEUED")) or 0
            failed = self.session.scalar(select(func.count()).select_from(Message).where(Message.tenant_id == self.tenant_id, Message.sender_id == sender.id, Message.status == "FAILED")) or 0
            replies = self.session.scalar(select(func.count()).select_from(Reply).where(Reply.tenant_id == self.tenant_id, Reply.recipient_email == sender.email)) or 0
            positive = self.session.scalar(select(func.count()).select_from(Reply).where(Reply.tenant_id == self.tenant_id, Reply.recipient_email == sender.email, Reply.classification.in_(tuple(POSITIVE_REPLY_CLASSIFICATIONS)))) or 0
            unsubscribes = self.session.scalar(select(func.count()).select_from(Reply).where(Reply.tenant_id == self.tenant_id, Reply.recipient_email == sender.email, Reply.classification == "UNSUBSCRIBE")) or 0
            complaints = self.session.scalar(
                select(func.count()).select_from(Complaint).join(Message, Message.id == Complaint.message_id).where(
                    Complaint.tenant_id == self.tenant_id,
                    Message.sender_id == sender.id,
                )
            ) or 0
            rows.append(
                {
                    "sender_id": str(sender.id),
                    "sender_name": sender.display_name or sender.email,
                    "sender_email": sender.email,
                    "campaigns": self.session.scalar(select(func.count()).select_from(Campaign).where(Campaign.tenant_id == self.tenant_id, Campaign.sender_id == sender.id)) or 0,
                    "queued": int(queued),
                    "sent": int(sent),
                    "provider_accepted": int(accepted),
                    "failed": int(failed),
                    "replies": int(replies),
                    "positive_replies": int(positive),
                    "unsubscribes": int(unsubscribes),
                    "complaints": int(complaints),
                    "health_score": float(sender.health_score) if sender.health_score is not None else 0.0,
                }
            )
        return rows

    def domain_report(self) -> list[dict[str, object]]:
        domains = self.session.scalars(select(Domain).where(Domain.tenant_id == self.tenant_id).order_by(Domain.domain)).all()
        rows: list[dict[str, object]] = []
        for domain in domains:
            domain_contacts = self.session.scalar(select(func.count()).select_from(Contact).where(Contact.tenant_id == self.tenant_id, Contact.email.ilike(f"%@{domain.domain}"))) or 0
            delivered = self.session.scalar(select(func.count()).select_from(Message).where(Message.tenant_id == self.tenant_id, Message.status == "SENT", Message.subject.ilike(f"%{domain.domain}%"))) or 0
            complaints = self.session.scalar(select(func.count()).select_from(Complaint).join(Message, Message.id == Complaint.message_id).where(Complaint.tenant_id == self.tenant_id, Message.sender_id.in_(select(EmailAccount.id).where(EmailAccount.tenant_id == self.tenant_id, EmailAccount.email.ilike(f"%@{domain.domain}"))))) or 0
            rows.append(
                {
                    "domain": domain.domain,
                    "health": domain.health_status,
                    "recipients": int(domain_contacts),
                    "messages_sent": int(delivered),
                    "complaints": int(complaints),
                }
            )
        return rows

    def dashboard(self) -> dict[str, object]:
        return {
            "tenant_id": str(self.tenant_id),
            "metrics": self.metrics(),
            "campaign_performance": self.campaign_report(),
            "sender_performance": self.sender_report(),
            "domain_health": self.domain_report(),
        }
