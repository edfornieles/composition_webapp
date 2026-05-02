from django.urls import path

from djangoscrap.sim_artwork import views

app_name = "sim_artwork"

urlpatterns = [
    path("api/bootstrap/", views.social_sim_bootstrap, name="social_sim_bootstrap"),
    path("api/<slug:slug>/tick/", views.social_sim_tick, name="social_sim_tick"),
    path("api/<slug:slug>/agents/<slug:agent_slug>/snapshot/", views.social_sim_agent_snapshot, name="social_sim_agent_snapshot"),
]
