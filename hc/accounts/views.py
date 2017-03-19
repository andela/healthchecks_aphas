import uuid
import re
import pprint

from django.contrib import messages
from datetime import timedelta as td
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth import authenticate
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import check_password
from django.contrib.auth.models import User
from django.core import signing
from django.http import HttpResponseForbidden, HttpResponseBadRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt

from django.utils import timezone
from hc.accounts.forms import (EmailPasswordForm, InviteTeamMemberForm,
                               RemoveTeamMemberForm, ReportSettingsForm,
                               SetPasswordForm, TeamNameForm)
from hc.accounts.models import Profile, Member, REPORT_DURATIONS
from hc.api.models import Channel, Check
from hc.lib.badges import get_badge_url


def _make_user(email):
    username = str(uuid.uuid4())[:30]
    user = User(username=username, email=email)
    user.set_unusable_password()
    user.save()

    user_profile = Profile(user=user)
    user_profile.save()

    # Make user a member of their own team
    user_membership = Member(team=user_profile, user=user)
    user_membership.save()

    channel = Channel()
    channel.user = user
    channel.kind = "email"
    channel.value = email
    channel.email_verified = True
    channel.save()

    return user


def _associate_demo_check(request, user):
    if "welcome_code" in request.session:
        check = Check.objects.get(code=request.session["welcome_code"])

        # Only associate demo check if it doesn't have an owner already.
        if check.user is None:
            check.user = user
            check.save()

            check.assign_all_channels()

            del request.session["welcome_code"]


def login(request):
    bad_credentials = False
    if request.method == 'POST':
        form = EmailPasswordForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data["email"]
            password = form.cleaned_data["password"]
            if len(password):
                user = authenticate(username=email, password=password)
                if user is not None and user.is_active:
                    auth_login(request, user)
                    return redirect("hc-checks")
                bad_credentials = True
            else:
                try:
                    user = User.objects.get(email=email)
                except User.DoesNotExist:
                    user = _make_user(email)
                    _associate_demo_check(request, user)

                user.profile.send_instant_login_link()
                return redirect("hc-login-link-sent")

    else:
        form = EmailPasswordForm()

    bad_link = request.session.pop("bad_link", None)
    ctx = {
        "form": form,
        "bad_credentials": bad_credentials,
        "bad_link": bad_link
    }
    return render(request, "accounts/login.html", ctx)


def logout(request):
    auth_logout(request)
    return redirect("hc-index")


def login_link_sent(request):
    return render(request, "accounts/login_link_sent.html")


def set_password_link_sent(request):
    return render(request, "accounts/set_password_link_sent.html")


def check_token(request, username, token):
    if request.user.is_authenticated and request.user.username == username:
        # User is already logged in
        return redirect("hc-checks")

    # Some email servers open links in emails to check for malicious content.
    # To work around this, we sign user in if the method is POST.
    #
    # If the method is GET, we instead serve a HTML form and a piece
    # of Javascript to automatically submit it.

    if request.method == "POST":
        user = authenticate(username=username, token=token)
        if user is not None and user.is_active:
            # This should get rid of "welcome_code" in session
            request.session.flush()

            user.profile.token = ""
            user.profile.save()
            auth_login(request, user)

            return redirect("hc-checks")

        request.session["bad_link"] = True
        return redirect("hc-login")

    return render(request, "accounts/check_token_submit.html")


@login_required
def profile(request):
    user_profile = request.user.profile
    # Switch user back to its default team
    if user_profile.current_team_id != user_profile.id:
        request.team = user_profile
        user_profile.current_team_id = user_profile.id
        user_profile.save()

    show_api_key = False
    if request.method == "POST":
        if "set_password" in request.POST:
            user_profile.send_set_password_link()
            return redirect("hc-set-password-link-sent")
        elif "create_api_key" in request.POST:
            user_profile.set_api_key()
            show_api_key = True
            messages.success(request, "The API key has been created!")
        elif "revoke_api_key" in request.POST:
            user_profile.api_key = ""
            user_profile.save()
            messages.info(request, "The API key has been revoked!")
        elif "show_api_key" in request.POST:
            show_api_key = True
        elif "update_reports_allowed" in request.POST:
            form = ReportSettingsForm(request.POST)
            if form.is_valid():
                user_profile.reports_allowed = True
                if form.cleaned_data['report_duration'] == 'never':
                    user_profile.reports_allowed = False
                elif form.cleaned_data['report_duration'] == 'daily':
                    user_profile.report_duration = REPORT_DURATIONS[0][0]
                elif form.cleaned_data['report_duration'] == 'weekly':
                    user_profile.report_duration = REPORT_DURATIONS[1][0]
                elif form.cleaned_data['report_duration'] == 'monthly':
                    user_profile.report_duration = REPORT_DURATIONS[2][0]
                else:
                    return HttpResponseBadRequest()

                user_profile.save()
                messages.success(request, "Your settings have been updated!")
        elif "invite_team_member" in request.POST:
            if not user_profile.team_access_allowed:
                return HttpResponseForbidden()

            form = InviteTeamMemberForm(request.POST)
            if form.is_valid():

                email = form.cleaned_data["email"]
                try:
                    user = User.objects.get(email=email)
                except User.DoesNotExist:
                    user = _make_user(email)

                user_profile.invite(user)

                messages.success(request, "Invitation to %s sent!" % email)
        elif "remove_team_member" in request.POST:
            form = RemoveTeamMemberForm(request.POST)
            if form.is_valid():

                email = form.cleaned_data["email"]
                farewell_user = User.objects.get(email=email)
                farewell_user.profile.current_team = None
                farewell_user.profile.save()

                # Remove user from channel integrations
                farewell_user_channel = Channel.objects.filter(
                        user=request.user, value=email)
                for channel in farewell_user_channel:
                    channel.delete()

                farewell_member = Member.objects.get(team=user_profile,
                                      user=farewell_user)
                # First, set team members with priorities greater than deleted
                # member to minus one
                lower_priority_members = Member.objects.filter(
                        team=user_profile,
                        priority__gt=farewell_member.priority)
                for member in lower_priority_members:
                    member.priority -= 1
                    member.save()

                farewell_member.delete()


                messages.info(request, "%s removed from team!" % email)
        elif "set_team_name" in request.POST:
            if not user_profile.team_access_allowed:
                return HttpResponseForbidden()

            form = TeamNameForm(request.POST)
            if form.is_valid():
                user_profile.team_name = form.cleaned_data["team_name"]
                user_profile.save()
                messages.success(request, "Team Name updated!")

        elif "save_notification_priorities" in request.POST:
            if not user_profile.team_access_allowed:
                return HttpResponseForbidden()
            data = dict(request.POST.iterlists())

            emails = [str(i) for i in data.get('email')]
            priorities = [int(i) for i in data.get('priority')]
            priority_dict = dict(zip(emails, priorities))

            try:
                user_profile.priority_delay = td(seconds =(int(data.get('priority_delay')[0])))
                checked = data.get('priority_notifications_allowed')
                if checked:
                    user_profile.prioritize_notifications = True
                else:
                    user_profile.prioritize_notifications = False

                user_profile.save()
                for email, priority in priority_dict.iteritems():
                    user = User.objects.get(email=email)
                    members = Member.objects.filter(team=user_profile,
                                      user=user)
                    for member in members:
                        member.priority = priority
                        member.save()
            except KeyError as e:
                raise e
            except Exception as e:
                raise e

    tags = set()
    for check in Check.objects.filter(user=request.team.user):
        tags.update(check.tags_list())

    username = request.team.user.username
    badge_urls = []
    for tag in sorted(tags, key=lambda s: s.lower()):
        if not re.match("^[\w-]+$", tag):
            continue

        badge_urls.append(get_badge_url(username, tag))

    ctx = {
        "page": "profile",
        "badge_urls": badge_urls,
        "profile": user_profile,
        "show_api_key": show_api_key
    }

    return render(request, "accounts/profile.html", ctx)


@login_required
def set_password(request, token):
    profile = request.user.profile
    if not check_password(token, profile.token):
        return HttpResponseBadRequest()

    if request.method == "POST":
        form = SetPasswordForm(request.POST)
        if form.is_valid():
            password = form.cleaned_data["password"]
            request.user.set_password(password)
            request.user.save()

            profile.token = ""
            profile.save()

            # Setting a password logs the user out, so here we
            # log them back in.
            u = authenticate(username=request.user.email, password=password)
            auth_login(request, u)

            messages.success(request, "Your password has been set!")
            return redirect("hc-profile")

    return render(request, "accounts/set_password.html", {})


def unsubscribe_reports(request, username):
    try:
        signing.Signer().unsign(request.GET.get("token"))
    except signing.BadSignature:
        return HttpResponseBadRequest()

    user = User.objects.get(username=username)
    user.profile.reports_allowed = False
    user.profile.save()

    return render(request, "accounts/unsubscribed.html")


def switch_team(request, target_username):
    other_user = User.objects.get(username=target_username)

    # The rules:
    # Superuser can switch to any team.
    access_ok = request.user.is_superuser

    # Users can switch to teams they are members of.
    if not access_ok and other_user.id == request.user.id:
        access_ok = True

    # Users can switch to their own teams.
    if not access_ok:
        for membership in request.user.member_set.all():
            if membership.team.user.id == other_user.id:
                access_ok = True
                break

    if not access_ok:
        return HttpResponseForbidden()

    request.user.profile.current_team = other_user.profile
    request.user.profile.save()

    return redirect("hc-checks")
