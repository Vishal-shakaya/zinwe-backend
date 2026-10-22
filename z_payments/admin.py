from django.contrib import admin

from .models import RazorpayOrder


@admin.register(RazorpayOrder)
class RazorpayOrderAdmin(admin.ModelAdmin):
    list_display = ("order_id", "owner", "amount_paise", "status", "confirmed_via", "created_at", "paid_at")
    list_filter = ("status", "confirmed_via")
    search_fields = ("order_id", "razorpay_payment_id", "owner__email")
    readonly_fields = [f.name for f in RazorpayOrder._meta.fields]
