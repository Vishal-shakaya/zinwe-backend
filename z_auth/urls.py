from django.urls import path

from . import portal_views, views

app_name = "z_auth"

urlpatterns = [
    path("early-access/", views.EarlyAccessView.as_view(), name="early-access"),
    # Sign-up + email codes
    path("signup/", views.SignupView.as_view(), name="signup"),
    path("signup/verify/", views.SignupVerifyView.as_view(), name="signup-verify"),
    path("otp/resend/", views.ResendCodeView.as_view(), name="otp-resend"),
    # Sign-in / out
    path("register/", views.RegisterView.as_view(), name="register"),  # deprecated: use signup/
    path("login/", views.LoginView.as_view(), name="login"),
    path("login/otp/request/", views.LoginCodeRequestView.as_view(), name="login-otp-request"),
    path("login/otp/verify/", views.LoginCodeVerifyView.as_view(), name="login-otp-verify"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("me/", views.MeView.as_view(), name="me"),
    # Password reset
    path("password/forgot/", views.PasswordForgotView.as_view(), name="password-forgot"),
    path("password/reset/", views.PasswordResetView.as_view(), name="password-reset"),
    # Owner portal
    path("company/", portal_views.CompanyView.as_view(), name="company"),
    path("plans/", views.PlanListView.as_view(), name="plans"),
    path("plans/activate/", views.PlanActivateView.as_view(), name="plan-activate"),
    path("subscription/", portal_views.SubscriptionView.as_view(), name="subscription"),
    path(
        "subscription/payment/",
        portal_views.PaymentSubmitView.as_view(),
        name="subscription-payment",
    ),
    path(
        "subscription/payment/<int:payment_id>/proof/",
        portal_views.PaymentProofView.as_view(),
        name="subscription-payment-proof",
    ),
]
