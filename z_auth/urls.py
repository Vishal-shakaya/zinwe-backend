from django.urls import path

from . import views

app_name = "z_auth"

urlpatterns = [
    path("early-access/", views.EarlyAccessView.as_view(), name="early-access"),
    path("register/", views.RegisterView.as_view(), name="register"),
    path("login/", views.LoginView.as_view(), name="login"),
    path("me/", views.MeView.as_view(), name="me"),
    path("plans/", views.PlanListView.as_view(), name="plans"),
    path("plans/activate/", views.PlanActivateView.as_view(), name="plan-activate"),
]
