import os
from django.shortcuts import render, redirect
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.models import User
from django.contrib.auth import login, authenticate
from django.contrib import messages
from .models import AddComposition, Profile
from .video_processing import create_final_video
from django.core.paginator import Paginator

def register(request):
    if request.method == "POST":
        email = request.POST["email"]
        username = request.POST["username"]
        password = request.POST["password"]
        confirm_password = request.POST["confirm_password"]

        if password != confirm_password:
            messages.error(request, "Passwords do not match!")
            return redirect("register")

        if User.objects.filter(username=username).exists():
            messages.error(request, "Username already taken!")
            return redirect("register")

        if User.objects.filter(email=email).exists():
            messages.error(request, "Email already registered!")
            return redirect("register")

        user = User.objects.create_user(username=username, email=email, password=password)
        
        # Ensure Profile is created after user registration
        Profile.objects.get_or_create(user=user)

        messages.success(request, "Account created successfully! You can now log in.")
        return redirect("login")

    return render(request, "register.html")

def home(request):
    return render(request, 'home.html')

def service(request):
    return render(request, 'service-detail.html')

def portfolio(request):
    return render(request, 'profile-detail.html')

def admin_login(request):
    if request.method == "POST":
        username = request.POST["username"]
        password = request.POST["password"]
        user = authenticate(request, username=username, password=password)

        if user is not None and user.is_staff:
            login(request, user)

            # Ensure Profile exists for admin user
            Profile.objects.get_or_create(user=user)

            return redirect("admin-dashboard")
        else:
            messages.error(request, "Invalid admin credentials!")
            return redirect("admin-login")

    return render(request, "admin/admin_login.html")

@staff_member_required
def admin_dashboard(request):
    return render(request, "admin/dashboard.html")

@staff_member_required
def add_composition(request):
    if request.method == "POST":
        composition = AddComposition.objects.create(
            background_video=request.FILES["background_video"],
            foreground_video=request.FILES["foreground_video"],
            audio_file=request.FILES.get("audio_file"),
            brightness=request.POST["brightness"],
            saturation=request.POST["saturation"],
            opacity=request.POST["opacity"],
            transition=request.POST["background_transition"]
        )

        # Process the video
        output_path = f"media/videos/final_{composition.id}.mp4"
        create_final_video(composition.background_video.path, composition.foreground_video.path, 
                           composition.audio_file.path if composition.audio_file else None, output_path)

        return redirect("composition-view")

    return render(request, "admin/composition.html")

@staff_member_required
def composition_view(request):
    compositions_list = AddComposition.objects.all().order_by("-id")  # Fetch all compositions
    paginator = Paginator(compositions_list, 10)  # Paginate with 10 items per page

    page_number = request.GET.get("page")
    compositions = paginator.get_page(page_number)

    return render(request, "admin/composition.html", {"compositions": compositions})

@staff_member_required
def new_source(request):
    return render(request, "admin/new-source.html")

@staff_member_required
def source_library(request):
    return render(request, "admin/source-library.html")
