from collections import defaultdict

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from pretalx.person.interfaces.forms.profile import SpeakerMergeForm
from pretalx.person.models import SpeakerProfile

from .speakers import (
    PROFILE_FIELDS,
    URL_IDENTIFIER,
    check_speaker_support,
    field_update,
    matching_user,
    person_url,
    profile_values,
    snapshot_state,
    source_values,
    validate_source_url,
)


class SpeakerImportForm(forms.Form):
    def __init__(self, *args, conference, people, mapping, source_url, **kwargs):
        super().__init__(*args, **kwargs)
        check_speaker_support()
        self.conference = conference
        self.people = people
        self.mapping = mapping
        self.source_url = validate_source_url(source_url)
        self.entries = []
        self.groups = []
        self.problems = []
        self.accounts = {}
        self.profiles = (
            list(
                SpeakerProfile.objects.filter(event=mapping.event)
                .select_related("user", "event__cfp", "profile_picture")
                .order_by("pk")
            )
            if mapping and mapping.event_id
            else []
        )
        self.by_profile = {p.pk: p for p in self.profiles}
        self.imports = (
            {m.person_id: m for m in mapping.speakers} if mapping and mapping.pk else {}
        )
        for profile in self.profiles:
            if profile.user:
                self.accounts[profile.user.pk] = profile.user
        self._question_check()
        for person in people:
            self._person(person)
        self._groups()
        self.fingerprint = snapshot_state(
            conference, people, mapping, self.profiles, self.accounts, self.source_url
        )

    def choice(self, key, label, choices, initial="", help_text=""):
        self.fields[key] = forms.ChoiceField(
            label=label,
            choices=[("", _("Select an option")), *choices],
            initial=initial,
            help_text=help_text,
        )
        value = self.data.get(key, "") if self.is_bound else initial
        return value if value in dict(choices) else ""

    def flag(self, key, label, help_text=""):
        self.fields[key] = forms.BooleanField(
            label=label, required=True, help_text=help_text
        )
        return self.is_bound and self.data.get(key) in ("on", "True", "true", "1")

    def _question_check(self):
        if not self.mapping or not self.source_url:
            return
        question = self.mapping.url_question
        if question and (
            question.target != "speaker"
            or question.variant != "url"
            or question.identifier != URL_IDENTIFIER
        ):
            self.problems.append(
                _(
                    "The frab source URL field has a different type or identifier. Restore it before import."
                )
            )
        elif self.mapping.has_url_question and (
            not question
            or question.is_public
            or question.is_visible_to_reviewers
            or not question.active
            or not question.contains_personal_data
            or not question.read_only
        ):
            self.flag(
                "repair_url_question", _("Restore the private frab source URL field")
            )

    def _person(self, person):
        pid = person["id"]
        values = source_values(person, self.source_url)
        entry = {
            "id": pid,
            "values": values,
            "mapping": self.imports.get(pid),
            "profile": None,
            "account": None,
            "merge": None,
            "skip": False,
            "notes": [],
        }
        for field, form_field in (
            ("name", forms.CharField(max_length=120)),
            ("email", forms.EmailField(required=False)),
        ):
            try:
                form_field.clean(values[field])
            except ValidationError:
                key = f"person_{pid}_{field}"
                form_field.label = _("frab person %(id)s: %(field)s") % {
                    "id": pid,
                    "field": field,
                }
                form_field.initial = values[field]
                if field == "email":
                    form_field.help_text = _(
                        "Correct the email or clear it to create a managed speaker."
                    )
                self.fields[key] = form_field
                if self.is_bound:
                    if key not in self.data:
                        self.data = self.data.copy()
                        self.data[key] = values[field]
                    try:
                        values[field] = form_field.clean(self.data.get(key, ""))
                    except ValidationError:
                        # Normal form validation reports the field error.
                        values[field] = ""
        try:
            account, accounts = matching_user(values["name"], values["email"])
        except ValidationError as exc:
            self.problems.extend(exc.messages)
            account, accounts = None, []
        self.accounts.update({u.pk: u for u in accounts})
        entry["account"] = account
        if accounts and not account:
            entry["notes"].append(
                _(
                    "Email matches an account, but the name differs: %(name)s. Keep this speaker managed."
                )
                % {"name": accounts[0].name or _("No name")}
            )
        if account and not account.is_active:
            entry["notes"].append(
                _("The matching account is inactive. Its state will not change.")
            )
        account_profile = next(
            (p for p in self.profiles if account and p.user_id == account.pk), None
        )
        imported = entry["mapping"]
        profile = self.by_profile.get(imported.profile_id) if imported else None
        if (
            profile
            and profile.user_id
            and (not account or account.pk != profile.user_id)
        ):
            entry["skip"] = True
            entry["profile"] = profile
            self.flag(
                f"skip_{pid}",
                _("Keep frab person %(id)s unchanged") % {"id": pid},
                _(
                    "The source email or name no longer matches the linked account. Correct the identity before updating this speaker."
                ),
            )
            entry["key"] = f"skip:{pid}"
        else:
            if imported and not profile:
                candidates = [
                    (f"profile:{p.pk}", f"{p.get_display_name()} ({p.code})")
                    for p in self.profiles
                    if not p.user_id or (account and p.user_id == account.pk)
                ]
                selected = self.choice(
                    f"target_{pid}",
                    _("Repair the target for frab person %(id)s") % {"id": pid},
                    [("new", _("Create a replacement profile")), *candidates],
                    help_text=_(
                        "The previous profile %(code)s was deleted or merged. Select its current profile, or create a replacement."
                    )
                    % {"code": imported.target_code},
                )
                if selected.startswith("profile:"):
                    profile = self.by_profile[int(selected.split(":")[1])]
            elif not profile and not account:
                candidates = [
                    p
                    for p in self.profiles
                    if not p.user_id
                    and values["email"]
                    and (p.email or "").lower() == values["email"].lower()
                ]
                aliases = [
                    e
                    for e in self.entries
                    if not e["account"]
                    and not e["skip"]
                    and values["email"]
                    and e["values"]["email"].lower() == values["email"].lower()
                ]
                # Show shared addresses on the first row as well.
                shared = (
                    values["email"]
                    and sum(
                        (p["email"] or "").strip().lower() == values["email"].lower()
                        for p in self.people
                    )
                    > 1
                )
                if candidates or aliases or shared:
                    selected = self.choice(
                        f"target_{pid}",
                        _("Identity for frab person %(id)s") % {"id": pid},
                        [("new", _("Separate managed speaker"))]
                        + [
                            (f"profile:{p.pk}", f"{p.get_display_name()} ({p.code})")
                            for p in candidates
                        ]
                        + [
                            (
                                f"source:{e['id']}",
                                _("Same person as frab ID %(id)s") % {"id": e["id"]},
                            )
                            for e in aliases
                        ],
                    )
                    if selected.startswith("profile:"):
                        profile = self.by_profile[int(selected.split(":")[1])]
                    elif selected.startswith("source:"):
                        other = next(
                            e for e in aliases if e["id"] == int(selected.split(":")[1])
                        )
                        profile = other["profile"]
                        entry["key"] = other["key"]
            if account_profile:
                if profile and profile.pk != account_profile.pk:
                    if profile.user_id:
                        self.problems.append(
                            _("An import cannot move a profile between user accounts.")
                        )
                    else:
                        entry["merge"] = profile
                profile = account_profile
            entry["profile"] = profile
            entry.setdefault(
                "key",
                f"profile:{profile.pk}"
                if profile
                else f"user:{account.pk}"
                if account
                else f"new:{pid}",
            )
        entry["url"] = person_url(self.source_url, self.conference["acronym"], pid)
        if entry["profile"]:
            entry["action"] = (
                _("Keep unchanged") if entry["skip"] else _("Reuse event profile")
            )
        elif account:
            entry["action"] = _("Link existing user")
        else:
            entry["action"] = _("Create managed speaker")
        self.entries.append(entry)

    def _merge_choices(self, merged, survivor):
        prefix = f"merge_{merged.pk}"
        self.flag(
            prefix,
            _("Merge managed profile %(code)s into %(target)s")
            % {"code": merged.code, "target": survivor.code},
            _(
                "This moves talks, answers, and mail links. Internal notes are combined. The managed profile is then deleted."
            ),
        )
        native = SpeakerMergeForm(merged=merged, survivor=survivor)
        choices = {}
        details = []
        for item in native.items:
            field = item["field"].name
            # Keep global user pictures unchanged. A native picture adoption can alter the account.
            options = [("survivor", _("Keep the account profile value"))]
            if field != "picture":
                options.append(("merged", _("Use the managed profile value")))
            key = f"{prefix}_{field}"
            selected = self.choice(
                key,
                f"{survivor.code}: {item['label']}",
                options,
                initial="survivor"
                if field == "picture" or item["survivor"]
                else "merged",
            )
            choices[field] = selected
            details.append(
                {
                    "field": str(item["label"]),
                    "current": str(item["survivor"] or ""),
                    "source": str(item["merged"] or ""),
                }
            )
        return choices, details

    def _groups(self):
        grouped = defaultdict(list)
        for entry in self.entries:
            if not entry["skip"]:
                grouped[entry["key"]].append(entry)
        for entries in grouped.values():
            primary = next(
                (e for e in entries if e["mapping"] and e["mapping"].is_primary),
                entries[0],
            )
            if len(entries) > 1:
                identical = all(e["values"] == primary["values"] for e in entries)
                initial = (
                    str(primary["id"])
                    if identical
                    or (primary["mapping"] and primary["mapping"].is_primary)
                    else ""
                )
                selected = self.choice(
                    f"primary_{entries[0]['id']}",
                    _("Primary frab person for %(name)s")
                    % {"name": primary["values"]["name"]},
                    [
                        (str(e["id"]), f"{e['id']}: {e['values']['name']}")
                        for e in entries
                    ],
                    initial=initial,
                    help_text=_(
                        "Use this person's frab data for all profile fields and the source URL. Keep the other IDs as aliases."
                    ),
                )
                primary = next(
                    (e for e in entries if str(e["id"]) == selected), primary
                )
            profile = primary["profile"]
            current = (
                profile_values(profile)
                if profile
                else dict.fromkeys(PROFILE_FIELDS, "")
            )
            merges = {}
            details = []
            for entry in entries:
                merged = entry["merge"]
                if merged and merged.pk not in merges:
                    choices, merge_details = self._merge_choices(merged, profile)
                    merges[merged.pk] = (merged, choices)
                    details.extend(merge_details)
                    for field in ("name", "email", "biography"):
                        if choices.get(field) == "merged":
                            current[field] = getattr(merged, field) or ""
                    if merged.internal_notes:
                        current["internal_notes"] = "\n\n".join(
                            v
                            for v in (current["internal_notes"], merged.internal_notes)
                            if v
                        )
            old = primary["mapping"].snapshot if primary["mapping"] else {}
            result = {}
            for field in PROFILE_FIELDS:
                incoming = primary["values"][field]
                value, conflict = field_update(
                    old.get(field, ""),
                    current[field],
                    incoming,
                    has_snapshot=field in old,
                )
                if conflict:
                    choice = self.choice(
                        f"value_{entries[0]['id']}_{field}",
                        f"{primary['values']['name']}: {SpeakerProfile._meta.get_field(field).verbose_name}",
                        [
                            ("target", _("Keep pretalx value")),
                            ("source", _("Use frab value")),
                        ],
                    )
                    details.append(
                        {"field": field, "current": current[field], "source": incoming}
                    )
                    if choice == "source":
                        value = incoming
                result[field] = value
            url_value = primary["url"]
            if (
                profile
                and self.mapping
                and self.mapping.url_question_id
                and self.source_url
            ):
                answers = list(
                    profile.answers.filter(question_id=self.mapping.url_question_id)
                )
                if len(answers) > 1:
                    self.problems.append(
                        _(
                            "A speaker has duplicate frab source URL answers. Remove the duplicate before import."
                        )
                    )
                current_url = answers[0].answer if answers else ""
                url_value, conflict = field_update(
                    old.get("source_url", ""),
                    current_url,
                    url_value,
                    has_snapshot="source_url" in old,
                )
                if conflict:
                    choice = self.choice(
                        f"url_{entries[0]['id']}",
                        _("Source URL for %(name)s")
                        % {"name": primary["values"]["name"]},
                        [
                            ("target", _("Keep pretalx URL")),
                            ("source", _("Use generated frab URL")),
                        ],
                    )
                    details.append(
                        {
                            "field": "frab source URL",
                            "current": current_url,
                            "source": primary["url"],
                        }
                    )
                    if choice == "source":
                        url_value = primary["url"]
            self.groups.append(
                {
                    "entries": entries,
                    "primary": primary,
                    "profile": profile,
                    "values": result,
                    "url": url_value,
                    "merges": merges,
                    "details": details,
                }
            )

    def clean(self):
        cleaned = super().clean()
        if self.problems:
            raise ValidationError(self.problems)
        # Do not let an alias select a profile linked to a different identity.
        for group in self.groups:
            profile = group["profile"]
            for entry in group["entries"]:
                linked_user = (
                    profile.user_id
                    if profile and profile.user_id
                    else group["primary"]["account"].pk
                    if group["primary"]["account"]
                    else None
                )
                if linked_user and (
                    not entry["account"] or entry["account"].pk != linked_user
                ):
                    raise ValidationError(
                        _(
                            "Every source alias must match the linked user's email and name."
                        )
                    )
        return cleaned
