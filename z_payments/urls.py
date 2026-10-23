from django.urls import path

from . import views

app_name = "z_payments"

urlpatterns = [
    path("razorpay/order/", views.RazorpayOrderView.as_view(), name="razorpay-order"),
    path("razorpay/verify/", views.RazorpayVerifyView.as_view(), name="razorpay-verify"),
    path("razorpay/webhook/", views.RazorpayWebhookView.as_view(), name="razorpay-webhook"),
    path("offer/<int:pk>/", views.OfferView.as_view(), name="offer"),
]
