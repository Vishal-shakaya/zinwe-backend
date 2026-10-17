from __future__ import annotations

from typing import Any

from rest_framework import serializers

from .models import SupportMessage, SupportTicket

REQUIRED = {"required": "This field is required.", "blank": "This field is required."}


class OpenTicketSerializer(serializers.Serializer):
    # Optional: the dashboard chat has no subject box; the service titles the ticket from the message.
    subject = serializers.CharField(max_length=160, required=False, allow_blank=True, default="")
    category = serializers.ChoiceField(
        choices=SupportTicket.Category.choices, default=SupportTicket.Category.QUESTION
    )
    message = serializers.CharField(max_length=4000, error_messages=REQUIRED)
    page = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")


class ReplySerializer(serializers.Serializer):
    body = serializers.CharField(max_length=4000, error_messages=REQUIRED)
    # Staff only: answer and close in one go.
    close = serializers.BooleanField(required=False, default=False)


class StatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=SupportTicket.Status.choices)


class MessageSerializer(serializers.ModelSerializer):
    author_name = serializers.SerializerMethodField()

    class Meta:
        model = SupportMessage
        fields = ["id", "sender", "author_name", "body", "created_at"]

    def get_author_name(self, obj: SupportMessage) -> str:
        if obj.sender == SupportMessage.Sender.STAFF:
            return "Zinwe team"
        return (obj.author.full_name or obj.author.email) if obj.author_id else "You"


class TicketSerializer(serializers.ModelSerializer):
    unread = serializers.IntegerField(read_only=True, default=0)
    last_message = serializers.SerializerMethodField()

    class Meta:
        model = SupportTicket
        fields = ["id", "subject", "category", "status", "page", "created_at", "updated_at", "unread", "last_message"]

    def get_last_message(self, obj: SupportTicket) -> dict[str, Any] | None:
        last = obj.messages.order_by("-created_at", "-id").first()
        return {"sender": last.sender, "body": last.body[:160], "created_at": last.created_at} if last else None


class TicketDetailSerializer(TicketSerializer):
    messages = MessageSerializer(many=True, read_only=True)

    class Meta(TicketSerializer.Meta):
        fields = [*TicketSerializer.Meta.fields, "messages"]


class StaffTicketSerializer(TicketSerializer):
    message_count = serializers.IntegerField(read_only=True, default=0)
    customer = serializers.SerializerMethodField()

    class Meta(TicketSerializer.Meta):
        fields = [*TicketSerializer.Meta.fields, "message_count", "customer"]

    def get_customer(self, obj: SupportTicket) -> dict[str, Any]:
        owner = obj.owner
        company = getattr(owner, "owned_company", None)
        return {
            "id": owner.pk,
            "email": owner.email,
            "full_name": owner.full_name,
            "company": getattr(company, "name", "") or owner.company or "",
        }


class StaffTicketDetailSerializer(StaffTicketSerializer):
    messages = MessageSerializer(many=True, read_only=True)

    class Meta(StaffTicketSerializer.Meta):
        fields = [*StaffTicketSerializer.Meta.fields, "messages"]


class ContactSerializer(serializers.Serializer):
    """The public website contact form: POST /api/contact/."""

    name = serializers.CharField(max_length=120, error_messages=REQUIRED)
    email = serializers.EmailField(
        max_length=254, error_messages={**REQUIRED, "invalid": "Enter a valid email address."},
    )
    company = serializers.CharField(max_length=160, required=False, allow_blank=True, default="")
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")
    topic = serializers.CharField(max_length=80, required=False, allow_blank=True, default="")
    message = serializers.CharField(max_length=4000, error_messages=REQUIRED)

    def validate_email(self, value: str) -> str:
        value = value.strip().lower()
        domain = value.rsplit("@", 1)[-1]
        if "." not in domain or domain.endswith(".") or domain.startswith("."):
            # "rohit@kulkarni" passes the generic email check, but our reply could never arrive (J1-05).
            raise serializers.ValidationError("Check your email address: it needs a domain like .com or .in.")
        return value
