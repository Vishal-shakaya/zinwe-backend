from __future__ import annotations

from rest_framework import serializers

from .kinds import label_for
from .models import FormSubmission


class SubmissionSerializer(serializers.ModelSerializer):
    kind_label = serializers.SerializerMethodField()
    handled_by = serializers.SerializerMethodField()

    class Meta:
        model = FormSubmission
        fields = [
            "id", "kind", "kind_label", "name", "email", "company", "phone", "subject", "message", "details",
            "status", "note", "handled_by", "handled_at", "created_at",
        ]

    def get_kind_label(self, obj: FormSubmission) -> str:
        return label_for(obj.kind)

    def get_handled_by(self, obj: FormSubmission) -> str:
        user = obj.handled_by
        return (user.get_full_name() or user.email) if user else ""


class UpdateSubmissionSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=FormSubmission.Status.choices, required=False)
    note = serializers.CharField(max_length=4000, required=False, allow_blank=True)
