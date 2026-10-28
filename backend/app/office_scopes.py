"""GĐ11.55 office scopes (design GD11B 5.3): which OAuth scopes the Google / Microsoft office tools ask for, per workspace mode, and which one each tool needs.
- Modes: "read" | "write" | "trigger" (mail trigger only: gmail.readonly / Mail.ReadBasic). Only the mode that is switched on is requested; "off" asks for nothing.
- Gmail write = gmail.modify (read + drafts + send + labels, NO permanent delete). `https://mail.google.com/` is never requested (FORBIDDEN, asserted by tests). No Microsoft `.All` scope.
- `required(provider, mode)` = what the provider is registered with / what a person is asked to grant. `need(provider, family, write)` = the minimal scopes ONE tool call needs
  (passed to connections.get_token). `satisfied/missing` compare with what a connection really holds: a stronger scope counts (gmail.modify covers gmail.readonly, Mail.ReadWrite covers
  Mail.Read ...), so a read tool still works with a token from the write mode or from before 11.55. A connection whose scope string is unknown (None) is never reported as missing.
- `provider` is the OAuth provider name ("google" | "microsoft"); `family` is the part of the product a tool belongs to (google: gmail | calendar | sheets | docs | drive;
  microsoft: mail | calendar | files)."""
G = "https://www.googleapis.com/auth/"
FORBIDDEN = ("https://mail.google.com/",)
READONLY_ALL = ("Sites.Read.All",)   # the only `.All` scope ever requested: read-only SharePoint document libraries (12.82a)
PROVIDERS = ("google", "microsoft")
MODES = ("off", "read", "write", "trigger")
BASE = {"google": ["openid", "email"], "microsoft": ["openid", "email", "offline_access", "User.Read"]}

# family -> {"read": scopes requested in read mode, "write": scopes requested in write mode, "trigger": ..., "need_read": ..., "need_write": ...}
FAMILIES = {
    "google": {
        "gmail": {"read": [G + "gmail.readonly"], "write": [G + "gmail.modify"], "trigger": [G + "gmail.readonly"],
                  "need_read": [G + "gmail.readonly"], "need_write": [G + "gmail.modify"]},
        "calendar": {"read": [G + "calendar.events.readonly", G + "calendar.calendarlist.readonly", G + "calendar.freebusy"],
                     "write": [G + "calendar.events", G + "calendar.calendarlist.readonly", G + "calendar.freebusy"],
                     "need_read": [G + "calendar.events.readonly"], "need_write": [G + "calendar.events"]},
        "sheets": {"read": [G + "spreadsheets.readonly"], "write": [G + "spreadsheets"], "need_read": [G + "spreadsheets.readonly"], "need_write": [G + "spreadsheets"]},
        "docs": {"read": [G + "documents.readonly"], "write": [G + "documents"], "need_read": [G + "documents.readonly"], "need_write": [G + "documents"]},
        "drive": {"read": [G + "drive.readonly"], "write": [G + "drive.readonly", G + "drive.file"], "need_read": [G + "drive.readonly"], "need_write": [G + "drive.file"]},   # GĐ12.82: drive.file = create files only (never the full "drive" scope)
    },
    "microsoft": {
        "mail": {"read": ["Mail.Read"], "write": ["Mail.ReadWrite", "Mail.Send"], "trigger": ["Mail.ReadBasic"],
                 "need_read": ["Mail.Read"], "need_write": ["Mail.ReadWrite", "Mail.Send"]},
        "calendar": {"read": ["Calendars.Read", "Calendars.Read.Shared"], "write": ["Calendars.ReadWrite", "Calendars.Read.Shared"], "need_read": ["Calendars.Read"], "need_write": ["Calendars.ReadWrite"]},   # .Shared = other people's calendars the tool asks about (findMeetingTimes, F-9); a tool names it in `scopes=`
        "files": {"read": ["Files.Read", "Sites.Read.All"], "write": ["Files.ReadWrite", "Sites.Read.All"], "need_read": ["Files.Read"], "need_write": ["Files.ReadWrite"]},   # GĐ12.82a: Sites.Read.All = READ-only SharePoint document libraries (only onedrive_sites_search names it in `scopes=`); never Sites.ReadWrite.All
    },
}

# scope -> the stronger scopes that include it
IMPLIED = {
    G + "gmail.readonly": (G + "gmail.modify",), G + "gmail.modify": (),
    G + "calendar.events.readonly": (G + "calendar.events", G + "calendar.readonly", G + "calendar"),
    G + "calendar.calendarlist.readonly": (G + "calendar.readonly", G + "calendar"),
    G + "calendar.freebusy": (G + "calendar.readonly", G + "calendar"),
    G + "calendar.events": (G + "calendar",),
    G + "spreadsheets.readonly": (G + "spreadsheets",), G + "drive.readonly": (G + "drive",),
    G + "documents.readonly": (G + "documents",), G + "drive.file": (G + "drive",),
    "Mail.ReadBasic": ("Mail.Read", "Mail.ReadWrite"), "Mail.Read": ("Mail.ReadWrite",), "Calendars.Read": ("Calendars.ReadWrite",), "Calendars.Read.Shared": ("Calendars.ReadWrite.Shared",), "Files.Read": ("Files.ReadWrite",),
}

LABELS = {   # short human names for the "missing permission" banner (Vietnamese in i18n_dict.json)
    G + "gmail.readonly": "read Gmail", G + "gmail.modify": "write Gmail (drafts, send, labels)", G + "calendar.events.readonly": "read calendar events",
    G + "calendar.events": "write calendar events", G + "calendar.calendarlist.readonly": "list calendars", G + "calendar.freebusy": "see free/busy times",
    G + "spreadsheets.readonly": "read Google Sheets", G + "spreadsheets": "write Google Sheets", G + "documents.readonly": "read Google Docs", G + "documents": "write Google Docs", G + "drive.readonly": "read Google Drive", G + "drive.file": "create files in Google Drive",
    "Mail.Read": "read Outlook mail", "Mail.ReadWrite": "write Outlook mail (drafts, move)", "Mail.Send": "send Outlook mail", "Mail.ReadBasic": "see mail headers",
    "Files.ReadWrite": "write OneDrive files (create, upload)", "Sites.Read.All": "find SharePoint sites and their document libraries (read only)", "Calendars.Read": "read Outlook calendar", "Calendars.Read.Shared": "read calendars shared with you (meeting-time suggestions)", "Calendars.ReadWrite": "write Outlook calendar", "Files.Read": "read OneDrive files",
}

def _prov(provider):
    if provider not in FAMILIES: raise ValueError(f"unknown provider {provider!r} (google | microsoft)")
    return provider

def _fam(provider, family):
    f = FAMILIES[_prov(provider)].get(family)
    if f is None: raise ValueError(f"unknown family {family!r} for {provider}")
    return f

def _dedup(xs):
    out = []
    for x in xs:
        if x not in out: out.append(x)
    return out

def required(provider, mode) -> list:
    """Scopes the provider is registered with for a workspace mode ('off' -> none). Never a forbidden scope, never an `.All` scope."""
    _prov(provider)
    if mode not in MODES: raise ValueError(f"unknown mode {mode!r}")
    if mode == "off": return []
    out = list(BASE[provider])
    for fam in FAMILIES[provider].values(): out += fam.get(mode, [])
    out = _dedup(out)
    assert not set(out) & set(FORBIDDEN) and not any(s.endswith(".All") and s not in READONLY_ALL for s in out)
    return out

def scope_string(provider, mode) -> str: return " ".join(required(provider, mode))

def need(provider, family, write=False, extra=()) -> list:
    """Minimal scopes ONE tool call of this family needs (read or write) + optional extra ones the tool names itself (e.g. calendar.freebusy)."""
    return _dedup(list(_fam(provider, family)["need_write" if write else "need_read"]) + list(extra))

def _norm(s) -> str:
    s = (s or "").strip()
    return s[len("https://graph.microsoft.com/"):] if s.startswith("https://graph.microsoft.com/") else s   # Graph may echo scopes with the resource prefix

def _granted(granted) -> set | None:
    if granted is None: return None
    if not isinstance(granted, (set, frozenset, list, tuple)): granted = str(granted).replace(",", " ").split()
    g = {_norm(x) for x in granted if x}
    return g or None

def missing_from(granted, scopes) -> list:
    """Of `scopes`, the ones `granted` (scope string / list) does not cover, a stronger scope counting as covering. Unknown grant (None / empty) -> [] (nothing provable)."""
    g = _granted(granted)
    if g is None: return []
    return [s for s in _dedup(scopes) if s not in g and not any(i in g for i in IMPLIED.get(s, ()))]

satisfied = lambda granted, scopes: not missing_from(granted, scopes)   # noqa: E731

def missing(connection, mode) -> list:
    """Scopes a connection {provider, scope} lacks for a workspace mode (the 'Missing permission' banner of Settings > Connections). Base scopes (openid, email ...) are not reported."""
    p = _prov((connection or {}).get("provider"))
    return [s for s in missing_from((connection or {}).get("scope"), required(p, mode)) if s not in BASE[p]]

def label(scope) -> str: return LABELS.get(_norm(scope), _norm(scope))
