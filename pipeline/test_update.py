#!/usr/bin/env python3
"""Unit tests for the pure helpers in update.py — the name-cleaning and scope
heuristics that decide what a Jade alert contributes to the watchlist.

These never touch the network, IMAP, or the Anthropic API. Run directly:

    python pipeline/test_update.py        # zero-dependency runner (needs bs4 only,
                                          # which update.py imports at module load)

or under pytest if available:

    pytest pipeline/test_update.py
"""

import contextlib
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update as u  # noqa: E402


# ---------------------------------------------------------------------------
# clean_case_name — extract a clean "A v B" (or non-adversarial) party name from
# the messy text fragment that sits just before a citation in an alert.
# ---------------------------------------------------------------------------
def _name(raw, cite="[2026] WASC 1"):
    return u.clean_case_name(raw, cite)


def test_clean_name_plain_pair():
    assert _name("Stefanski v Western Australia") == "Stefanski v Western Australia"
    assert _name("Garlett v Western Australia & Anor") == "Garlett v Western Australia & Anor"


def test_clean_name_keeps_no_suffix():
    # the "[No 2]" / "(No 3)" hearing-number suffix is part of the name
    assert _name("The State of Western Australia v Raven [No 2]") == \
        "The State of Western Australia v Raven [No 2]"
    assert _name("Smith v Jones (No 3)") == "Smith v Jones (No 3)"


def test_clean_name_strips_leading_junk():
    # a leading court / preamble fragment must not be swallowed into the party name
    assert _name("Court of Appeal. The State of Western Australia v Mead [No 2]") == \
        "The State of Western Australia v Mead [No 2]"
    assert _name("Court of Appeal. The State of Western Australia v Mead") == \
        "The State of Western Australia v Mead"
    assert _name("New decision: Western Australia v Montani") == "Western Australia v Montani"
    assert _name("In re something. Smith v Jones") == "Smith v Jones"


def test_clean_name_strips_parallel_citations():
    assert _name("Smith v Jones (2020) 270 CLR 1", "[2020] HCA 5") == "Smith v Jones"
    assert _name("Smith v Jones (2020) 270 CLR 1;", "[2020] HCA 5") == "Smith v Jones"
    assert _name("Foo v Bar (2019) 55 WAR 12; [2019] WASCA 7", "[2019] WASCA 7") == "Foo v Bar"


def test_clean_name_keeps_abbreviated_state():
    # "W.A." must not be mistaken for a sentence boundary and truncate the plaintiff
    assert _name("The State of W.A. v Smith") == "The State of W.A. v Smith"


def test_clean_name_pseudonym():
    assert _name("RCB v The State of Western Australia (a pseudonym)") == \
        "RCB v The State of Western Australia (a pseudonym)"
    assert _name("Smith (a pseudonym) v The Queen") == "Smith (a pseudonym) v The Queen"


def test_clean_name_non_adversarial():
    # no "A v B" pair — fall back to the trailing fragment, intact
    assert _name("Re Jones; Ex parte Smith") == "Re Jones; Ex parte Smith"
    assert _name("Ex parte Coward") == "Ex parte Coward"


def test_clean_name_empty():
    assert _name("") == "(case name pending)"
    assert _name("   ", "[2026] WASC 1") == "(case name pending)"


# ---------------------------------------------------------------------------
# in_scope — the WASC/WASCA name-only civil filter. A criminal case must never be
# dropped; obvious civil matters (with no criminal-party signal) should be.
# ---------------------------------------------------------------------------
def _wa(name, tag="WASC"):
    return u.in_scope({"courtTag": tag, "caseName": name, "blurb": name})


CRIMINAL_KEEP = [
    "The State of Western Australia v Raven [No 2]",
    "Western Australia v Montani",
    "Stefanski v Western Australia",
    "RCB v The State of Western Australia (a pseudonym)",
    "The Queen v Smith",
    "Police v Jones",
    "DPP (WA) v Smith Holdings Pty Ltd",          # DPP signal beats the "Pty Ltd" civil word
    "Western Australia v BHP Billiton Iron Ore Pty Ltd",  # State prosecuting a company — keep
    "Inquest into the death of John Citizen",
    "The State of W.A. v Smith",
]

CIVIL_DROP = [
    "Westpac Banking Corporation Ltd v Smith",
    "ABC Nominees Pty Ltd v Minister for Lands (Western Australia)",
    "Bob Jane Corporation Pty Ltd v Sheriff of Western Australia",
    "Commissioner of State Revenue v Acme Holdings Pty Ltd",
    "Legal Profession Complaints Committee v Smith",
    "XYZ Pty Ltd v State Administrative Tribunal",
    "Insurance Commission of Western Australia v Jones",
]


def test_in_scope_keeps_criminal():
    for n in CRIMINAL_KEEP:
        ok, why = _wa(n)
        assert ok, f"criminal case wrongly dropped: {n!r} ({why})"


def test_in_scope_drops_civil():
    for n in CIVIL_DROP:
        ok, why = _wa(n)
        assert not ok, f"civil case wrongly kept: {n!r}"
        assert "civil party" in why, f"dropped for the wrong reason: {n!r} ({why})"


def test_in_scope_non_wa_courts_not_name_filtered():
    # HCA is not a name-only WA court: a civil-looking name is NOT dropped on that basis
    ok, _ = u.in_scope({"courtTag": "HCA", "caseName": "Westpac v Smith Pty Ltd",
                        "blurb": "Westpac v Smith Pty Ltd"})
    assert ok


def test_in_scope_drop_keywords_still_apply():
    ok, why = u.in_scope({"courtTag": "WASC", "caseName": "Smith v Minister for Immigration",
                         "blurb": "judicial review of a migration visa decision"})
    assert not ok and "out-of-scope topic" in why


def test_in_scope_gated_courts_need_topic():
    # a persuasive (gated) court with no investigation/evidence keyword is dropped
    ok, why = u.in_scope({"courtTag": "QCA", "caseName": "Re a costs dispute",
                         "blurb": "costs of a commercial appeal"})
    assert not ok and "no investigation/evidence topic" in why
    ok, _ = u.in_scope({"courtTag": "QCA", "caseName": "R v Smith",
                       "blurb": "admissibility of a confession at trial"})
    assert ok


# ---------------------------------------------------------------------------
# HANDOFF 7.2 — parse_alert: the text scan is a FALLBACK. An authority merely
# CITED inside an alert blurb must never become its own candidate while the
# alert's real entries are reachable as jade.io links (this is how the civil
# costs case Oshlack [1998] HCA 11 was auto-analysed into the library).
# ---------------------------------------------------------------------------
ALERT_HTML = (
    '<html><body><table>'
    '<tr><td><a href="https://jade.io/viewArticle.html?aid=1">Rose v The State of '
    'Western Australia [2026] WASCA 123</a></td></tr>'
    '<tr><td>CRIMINAL LAW &ndash; appeal against sentence. Da Silva v The King '
    '(2024) 391 FLR 101; DPP(Cth) v Merrill [2015] VSCA 52; Kleindyk v The Queen '
    '[2016] WASCA 123, applied.</td></tr>'
    '<tr><td><a href="https://jade.io/viewArticle.html?aid=2">Suppressed '
    '[2026] WASCA 113</a></td></tr>'
    '<tr><td>COSTS &ndash; Oshlack v Richmond River Council (1998) 193 CLR 72; '
    '[1998] HCA 11, applied.</td></tr>'
    '</table></body></html>')


def test_parse_alert_ignores_authorities_cited_in_blurbs():
    ids = [it["id"] for it in u.parse_alert(ALERT_HTML)]
    assert ids == ["wasca-2026-123", "wasca-2026-113"], ids
    for cited in ("hca-1998-11", "wasca-2016-123", "vsca-2015-52"):
        assert cited not in ids, f"cited authority became a candidate: {cited}"


def test_parse_alert_link_names_are_taken_as_given():
    by_id = {it["id"]: it for it in u.parse_alert(ALERT_HTML)}
    # a one-word name is a real WA case name and must survive untouched
    assert by_id["wasca-2026-113"]["caseName"] == "Suppressed"
    assert by_id["wasca-2026-123"]["caseName"] == "Rose v The State of Western Australia"
    assert all(it["via"] == "link" for it in by_id.values())
    assert not any(it["nameSuspect"] for it in by_id.values())


def test_parse_alert_falls_back_when_links_are_rewritten():
    # a mail-security wrapper rewrites every href off the jade.io host: the link
    # pass finds nothing, so the text scan must still surface the real entries
    # (losing a decision is far worse than a noisy watchlist row).
    wrapped = ALERT_HTML.replace("https://jade.io/viewArticle.html?aid=", "https://mg.test/r/")
    by_id = {it["id"]: it for it in u.parse_alert(wrapped)}
    assert "wasca-2026-123" in by_id and "wasca-2026-113" in by_id, sorted(by_id)
    assert by_id["wasca-2026-113"]["caseName"] == "Suppressed"
    assert by_id["wasca-2026-113"]["via"] == "scan"
    # the fallback IS permissive: cited authorities come back too. They are kept
    # (never dropped) and held out of auto-analysis by auto_analysis_ok instead.
    assert "wasca-2016-123" in by_id


def test_parse_alert_never_loses_a_citation_to_name_trouble():
    # every shape a reviewer flagged as a potential silent drop: a parallel
    # citation on the entry's own line, a citation-first listing, and two
    # citations running together. Each must still yield its case.
    for body, want in [
        ("Smith v The State of Western Australia (2026) 61 WAR 1; [2026] WASCA 141",
         {"wasca-2026-141": "Smith v The State of Western Australia"}),
        ("Farrugia v The King (2026) 100 ALJR 1; [2026] HCA 28",
         {"hca-2026-28": "Farrugia v The King"}),
        ("Suppressed [2026] WASCA 142 [2026] WASCA 143 Suppressed",
         {"wasca-2026-142": "Suppressed", "wasca-2026-143": "Suppressed"}),
    ]:
        got = {it["id"]: it["caseName"] for it in u.parse_alert(body)}
        assert got == want, (body, got)
    # a citation with no recoverable name still reaches the watchlist: the
    # placeholder is the pipeline's safe degradation, never grounds for a drop.
    got = {it["id"]: it["caseName"] for it in u.parse_alert(
        "[2026] WASCA 131 New appeal. Kelly v The State of Western Australia [2026] WASCA 132")}
    assert got["wasca-2026-131"] == "(case name pending)"
    assert got["wasca-2026-132"] == "Kelly v The State of Western Australia"


def test_name_quality_flag_never_drops_anything():
    for junk in [   # verbatim mangled names from data/state.json's pending queue
        "(2024) 282 CLR 460; Helensburgh Coal Pty Ltd v Bartley (2025) 424 ALR 1; R v HCZ",
        "rkson v The Queen (2011) 32 VR 361; [2011] VSCA 157; Hili v The Queen",
        "King (2024) 391 FLR 101; DPP(Cth) v Merrill [2015] VSCA 52; Kleindyk v The Queen",
        "v Merrill [2015] VSCA 52; Kleindyk v The Queen [2016] WASCA 123; Lam v The King",
        "2; Kleindyk v The Queen [2016] WASCA 123; Lam v The King [2025] WASCA 9",
        "appeal disclosed no arguable error of the kind described in House v R",
        "ter Of An Application BY Thomas William Raymond Towle for Leave to Issue Or File",
        "(case name pending)", "",
    ]:
        assert not u._looks_like_case_name(junk), f"junk name looks clean: {junk!r}"
    for real in [   # every genuine name shape in today's queue, plus locked shapes
        "Suppressed", "KHUU", "Re Frigger", "MRV v SNW [No 2]", "Le v The King",
        "Farrugia v The King", "The King v Ko", "Stack v WA Police", "Bin Saad v WA Police",
        "DJF v Director of Public Prosecutions", "The State of Western Australia v Hoskin [No 4]",
        "Malone Darcy Fleming (a pseudonym) v The State of Western Australia",
        "Downes v The State of Western Australia [No 3]", "The State of W.A. v Smith",
        "Re Jones; Ex parte Smith", "Ex parte Coward", "R v Smith",
        "Inquest into the death of John Citizen", "Varelis v Rispoli [No 2] (S)",
    ]:
        assert u._looks_like_case_name(real), f"real case name flagged: {real!r}"


# ---------------------------------------------------------------------------
# HANDOFF 7.3 — WASC (first instance) requires a POSITIVE criminal signal;
# WASCA stays on the conservative civil-keyword rule. Both lists are the live
# 2026-09 watchlist (data/state.json) verbatim.
# ---------------------------------------------------------------------------
WASC_KEEP = [
    "Stack v WA Police", "Wood v WA Police", "Wilkinson v WA Police",     # \bpolice\b
    "Bin Saad v WA Police",
    "The State of Western Australia v TJD [No 2]",
    "The State of Western Australia v Hill [No 3]",
    "The State of Western Australia v Van Beek [No 2]",
    "The State of Western Australia v Hoskin [No 4]",
    "The State of Western Australia v KPB [No 5]",
    "The State of Western Australia v Paraha [No 2]",
    "The State of Western Australia v Moussa [No 3]",
    "The State of Western Australia v Blurton",
    "DJF v Director of Public Prosecutions",         # DPP + initials party
    "Minchin v Director of Public Prosecutions",
    "Suppressed",                                    # WASC_RESCUE
]

WASC_CIVIL_NOISE = [
    "Palaloi v Western Australian Industrial Appeal Court", "Haskett v Jago",
    "Allen v Milanova", "Muenkel v Muenkel", "Psaila v Psaila", "Lane v Briggs",
    "Bacha v Cordero Jimenez", "Hayes v Hayes", "Tallott v Stein", "Daniel v Gray",
    "C BY Next Friend XYZ v The Church of Jesus Christ of LATTER-DAY SAINTS AUSTRALIA",
    "Re Frigger", "Varelis v Rispoli [No 2] (S)",
]


def test_wasc_keeps_every_genuine_criminal_name():
    for n in WASC_KEEP:
        ok, why = _wa(n)
        assert ok, f"genuine WASC criminal case wrongly dropped: {n!r} ({why})"


def test_wasc_drops_person_v_person_civil():
    for n in WASC_CIVIL_NOISE:
        assert not _wa(n)[0], f"WASC civil noise wrongly kept: {n!r}"


def test_police_signal_really_fires_on_wa_police():
    # four live WASC keeps rest on this single alternative and nothing else
    for n in ("Stack v WA Police", "Wood v WA Police", "Wilkinson v WA Police",
              "Bin Saad v WA Police"):
        assert u.CRIMINAL_NAME.search(n), n
        assert not u.CIVIL_NAME.search(n), n


def test_state_of_wa_without_the_article_is_a_criminal_signal():
    # both are ordinary renderings of a WA indictment / appeal and matched
    # NOTHING before this change
    for n in ("State of Western Australia v Smith", "Smith v State of Western Australia"):
        assert u.CRIMINAL_NAME.search(n), n
        assert _wa(n)[0], n
    for n in CIVIL_DROP:            # and the seven civil regression names still drop
        assert not _wa(n)[0], n


def test_wasc_rescue_words_are_not_in_criminal_name():
    # WASC_RESCUE must stay OUT of CRIMINAL_NAME: CRIMINAL_NAME also gates the
    # pre-existing civil drop, so widening it would weaken that filter in BOTH
    # WA courts. This is the test that fails if someone "tidies up" by merging them.
    assert u.WASC_RESCUE.search("Suppressed")
    assert not u.CRIMINAL_NAME.search("Suppressed")
    for tag in ("WASC", "WASCA"):
        for n in ("Smith v Bail Bonds Pty Ltd", "Suppressed Holdings Pty Ltd v Brown"):
            ok, why = u.in_scope({"courtTag": tag, "caseName": n, "blurb": n})
            assert not ok and "civil party" in why, (tag, n, why)


def test_initials_party_is_case_sensitive_and_party_anchored():
    for n in ("MRV v SNW [No 2]", "DJF v Director of Public Prosecutions",
              "The State of Western Australia v TJD [No 2]", "A.B. v C.D.", "H v J",
              "Smith v TJD (S)"):
        assert u.INITIALS_PARTY.search(n), f"initials party missed: {n!r}"
    # an ordinary short surname must NOT read as initials (it would if this lived
    # inside CRIMINAL_NAME, which is re.I) ...
    for n in ("Lane v Briggs", "Daniel v Gray", "Haskett v Jago", "Hayes v Hayes"):
        assert not u.INITIALS_PARTY.search(n), f"surname read as initials: {n!r}"
    # ... nor a capitalised token mid-name (live WASC 374), nor a corporate acronym
    for n in ("C BY Next Friend XYZ v The Church of Jesus Christ of LATTER-DAY SAINTS",
              "XYZ Pty Ltd v State Administrative Tribunal",
              "ABC Nominees Pty Ltd v Minister for Lands (Western Australia)"):
        assert not u.INITIALS_PARTY.search(n), f"wrongly read as initials: {n!r}"


def test_initials_party_does_not_weaken_the_civil_filter():
    # INITIALS_PARTY must never feed the CIVIL_NAME branch — a 2-5 letter corporate
    # acronym in the defendant slot is not a protected-identity party.
    for tag in ("WASC", "WASCA"):
        for n in ("Jones v ANZ", "Acme Pty Ltd v NAB [No 2]", "Perth City Council v XYZ",
                  "Insurance Commission of Western Australia v ABC"):
            ok, why = u.in_scope({"courtTag": tag, "caseName": n, "blurb": n})
            assert not ok and "civil party" in why, f"civil filter weakened: {tag} {n!r} ({why})"


def test_wasc_never_judges_a_name_it_could_not_parse():
    # a parse failure is ambiguous, and HANDOFF 5 says keep when ambiguous. These
    # are kept with their correct citation and links, exactly as today.
    for n in ("(case name pending)", "", "   ",
              "pplied R v Smith (1999) 106 A Crim R 149; The State of WA v Jones"):
        it = {"courtTag": "WASC", "caseName": n, "blurb": n,
              "nameSuspect": not u._looks_like_case_name(n)}
        ok, why = u.in_scope(it)
        assert ok, f"unparsed name wrongly dropped: {n!r} ({why})"


def test_wasc_keeps_criminal_matters_in_civil_dress():
    # WA criminal work that is NOT styled against the Crown/police/DPP
    for n in ("Re an application by Smith for a writ of habeas corpus",
              "Kelly v Nowak - appeal against a restraining order",
              "Attorney General (WA) v Smith - criminal contempt",
              "Smith v Superintendent of Casuarina Prison",
              "Re Smith; Ex parte Jones",
              "Re an application by SMITH under the Criminal Property Confiscation Act 2000 (WA)",
              "Re an application for bail by Smith"):
        ok, why = _wa(n)
        assert ok, f"criminal matter in civil dress wrongly dropped: {n!r} ({why})"
    # and the catchword second chance, for an entry whose party name is unhelpful
    ok, _ = u.in_scope({"courtTag": "WASC", "caseName": "Ferguson v Nowak", "nameSuspect": False,
                        "blurb": "CRIMINAL LAW - application to exclude records of interview "
                                 "- whether admissions were made voluntarily"})
    assert ok


def test_wasca_is_not_subject_to_the_wasc_signal_rule():
    for n in ("Haskett v Jago", "Muenkel v Muenkel", "Re Frigger"):
        assert _wa(n, tag="WASCA")[0], f"WASCA wrongly tightened: {n!r}"
    for n in ("Mehrabi v The State of Western Australia", "Le v The King", "MRV v SNW [No 2]",
              "Malone Darcy Fleming (a pseudonym) v The State of Western Australia",
              "Frigger v The State of Western Australia", "Suppressed",
              "Downes v The State of Western Australia [No 3]"):
        ok, why = _wa(n, tag="WASCA")
        assert ok, f"genuine WASCA case wrongly dropped: {n!r} ({why})"
    # the Frigger tension is resolved BY COURT, not by name
    assert _wa("Re Frigger", tag="WASCA")[0] and not _wa("Re Frigger")[0]


def test_hcasj_vexatious_leave_applications_dropped():
    for n in ("In the Matter Of An Application BY Gerrard Tate for Leave To Issue Or File",
              "In the Matter Of An Application By KLH For Leave To Issue Or File",
              "In the Matter Of An Application BY Djuran Bunjileenee For Leave To Issue Or File",
              # clean_case_name's 80-char fallback clips the head - key on the tail
              "ter Of An Application BY Thomas William Raymond Towle for Leave to Issue Or File"):
        ok, why = u.in_scope({"courtTag": "HCASJ", "caseName": n, "blurb": n})
        assert not ok and "vexatious" in why, f"vexatious leave application kept: {n!r} ({why})"


def test_hcasj_rule_reads_the_name_not_a_neighbours_blurb():
    # a blurb is a window over running text (or an anchor's whole parent), so the
    # formula bleeds in from the entry listed BEFORE this one. Matching the blurb
    # would drop genuine High Court single-justice criminal decisions.
    bleed = ("In the Matter Of An Application BY Djuran Bunjileenee For Leave To Issue Or "
             "File [2026] HCASJ 29 Nguyen v The King [2026] HCASJ 32")
    for n in ("Nguyen v The King", "R v Jones", "Suppressed", "Farrugia v The King",
              "In the Matter of an Application by Smith for Leave to Appeal",
              "Application by Smith for an extension of time to file an application "
              "for special leave"):
        ok, why = u.in_scope({"courtTag": "HCASJ", "caseName": n, "blurb": bleed})
        assert ok, f"real single-justice matter wrongly dropped: {n!r} ({why})"
    # and the rule is court-scoped: it can never fire on a WA court or the HCA
    n = "In the Matter Of An Application BY X for Leave To Issue Or File"
    for tag in ("WASC", "WASCA", "HCA"):
        _, why = u.in_scope({"courtTag": tag, "caseName": n, "blurb": n})
        assert "vexatious" not in why, f"{tag} hit the HCASJ-only rule ({why})"


def test_screened_out_flags_only_the_new_judgement_calls():
    assert u._screened_out("WASC: no criminal signal (name-only entry)")
    assert u._screened_out("HCASJ: vexatious-proceedings leave application")
    # the pre-existing civil drop has been silent since session 5 — listing it too
    # would only dilute the block that carries the genuinely risky WASC calls
    assert not u._screened_out("WASCA: civil party, no criminal signal")
    assert not u._screened_out("out-of-scope topic")
    assert not u._screened_out("court VSCA not in scope")
    assert not u._screened_out("QCA: no investigation/evidence topic keyword")


# ---------------------------------------------------------------------------
# HANDOFF 7.4 — auto_analysis_ok gates UNREVIEWED PUBLICATION, never the
# watchlist. A False here must never mean the case is lost: it stays pending and
# still goes out in the watchlist email carrying its reason.
# ---------------------------------------------------------------------------
def _hca(name, blurb=None, via="link", tag="HCA", **kw):
    it = {"courtTag": tag, "caseName": name, "via": via,
          "blurb": name if blurb is None else blurb,
          "nameSuspect": not u._looks_like_case_name(name)}
    it.update(kw)
    return it


def test_auto_analysis_keeps_genuine_hca_criminal():
    for name, cite in [("Farrugia v The King", "[2026] HCA 28"),
                       ("The King v Ko", "[2026] HCA 29")]:
        ok, why = u.auto_analysis_ok(_hca(name, f"{name} {cite}"))
        assert ok, f"genuine HCA criminal case wrongly held: {name!r} ({why})"
    for name in ("MRV v SNW [No 2]", "Suppressed", "R v Smith"):
        assert u.auto_analysis_ok(_hca(name, tag="HCASJ"))[0], name


def test_topic_alone_would_hold_the_genuine_hca_cases():
    # locks the REASON the party-name pass exists: a Jade HCA link blurb is just
    # the name plus the citation, so TOPIC_KEYWORDS matches nothing in either.
    for blurb in ("Farrugia v The King [2026] HCA 28", "The King v Ko [2026] HCA 29"):
        assert not u.TOPIC_KEYWORDS.search(blurb), \
            f"TOPIC_KEYWORDS now matches {blurb!r} — do NOT simplify away the party pass"


# The three the fetcher's first run published (20/09/2026) — each carries a keyword
# the old gate matched anywhere in the text ('forensic', 'sexual'/'fabricat', 'exclud').
# Abridged from the Court's pages; the shape (area – segment – … . Words and
# phrases – … . Act …) is the Court's.
HAINES_CATCH = ('Mental health – Forensic patient – Extension of status as forensic patient – Where making '
                'of extension order subject to Mental Health and Cognitive Impairment Forensic Provisions '
                'Act 2020 (NSW), s 122(1) – Whether to accept concession. Words and phrases – "extension '
                'order", "forensic patient", "unfit to be tried". Mental Health and Cognitive Impairment '
                'Forensic Provisions Act 2020 (NSW) , s 122(1).')
GXT25_CATCH = ('Administrative law – Judicial review – Where delegate refused application for Protection '
               '(Subclass 866) visa – Where plaintiff claimed protection on basis of fear of harm by reason '
               'of religion –Where plaintiff subsequently raised fear of harm by reason of sexual orientation '
               '– Where delegate found sexual orientation claim fabricated – Whether denial of procedural '
               'fairness. Words and phrases – "adverse inference", "fabricated claim". Migration Act 1958 '
               '(Cth) , ss 57, 486A.')
ORICA_CATCH = ('Industrial law (Cth) – Coal industry – Statutory interpretation – Where cl 4.3(g) of Award '
               'excluded "supply of shotfiring or other explosive services by an employer not otherwise '
               'engaged in the black coal mining industry" – Whether cl 4.3(g) of Award applied to location '
               'limb. Words and phrases – "eligible employee", "shotfirer". Coal Mining Industry (Long '
               'Service Leave) Administration Act 1992 (Cth) , ss 3, 4(1), 39A(1).')


def test_catchword_areas_reads_the_courts_area_headings():
    assert u.catchword_areas(HAINES_CATCH) == ["Mental health"]
    assert u.catchword_areas(GXT25_CATCH) == ["Administrative law"]
    assert u.catchword_areas(ORICA_CATCH) == ["Industrial law (Cth)"]
    # several areas, in the Court's order, stopping at Words and phrases (a typo'd
    # "Words and phrase s" is on the live EGH19 page); a minus sign or an unspaced
    # dash (both on live pages) is a segment break; a hyphen inside a word is not
    two = ("Constitutional law (Cth) − Judicial power of Commonwealth – Chapter III. Criminal law –Sentencing "
           "– Non-parole period – ss 10-11. Words and phrase s – \"non-parole\". Crimes Act 1914 (Cth), s 19AB.")
    assert u.catchword_areas(two) == ["Constitutional law (Cth)", "Criminal law"]
    assert u.catchword_areas("Bail – Application for bail pending special leave.") == ["Bail"]
    assert u.catchword_areas("") == [] and u.catchword_areas(None) == []
    assert u.criminal_catchwords(two) and u.criminal_catchwords("Evidence – Admissibility.")
    assert u.criminal_catchwords("Proceeds of crime – Forfeiture order.") and u.criminal_catchwords("Police – Powers.")
    for c in (HAINES_CATCH, GXT25_CATCH, ORICA_CATCH):
        assert not u.criminal_catchwords(c), c[:40]
    # the keyword the old gate fell for is still in the text: the heading is what decides
    assert u.TOPIC_KEYWORDS.search(HAINES_CATCH) and u.TOPIC_KEYWORDS.search(GXT25_CATCH) and u.TOPIC_KEYWORDS.search(ORICA_CATCH)
    # a keyword in a LATER segment of a non-criminal area does not rescue it
    assert not u.criminal_catchwords("Torts – Police – False imprisonment – Whether arrest lawful.")
    assert not u.criminal_catchwords("Migration – Character test – Where criminal record – Sentence of 12 months.")


def test_auto_analysis_trusts_the_courts_catchwords_when_present():
    # hca.lookup() puts the Court's own catchwords on the item: their AREA HEADINGS
    # decide, whatever the party names or the rest of the text look like. "R Lawyers
    # v Mr Daily" has a criminal-looking name and a civil subject; "EGH19 v Minister"
    # is migration; "The King v Ko" is criminal.
    it = _hca("R Lawyers v Mr Daily [No 2]", "R Lawyers v Mr Daily [No 2] [2026] HCA 31")
    it["catchwords"] = "Legal practitioners – Costs – Solicitor's lien – Whether lien survives termination of retainer."
    ok, why = u.auto_analysis_ok(it)
    assert not ok and "catchwords are not criminal" in why and "Solicitor's lien" in why
    assert "area: Legal practitioners" in why
    it = _hca("EGH19 v Minister for Immigration & Citizenship", "EGH19 v Minister [2026] HCA 33")
    it["catchwords"] = "Migration – Visa cancellation – Whether decision affected by jurisdictional error."
    assert not u.auto_analysis_ok(it)[0]
    # the three the first run published: held now, with the Court's area in the reason
    for name, catch, area in (("Dale Haines by his litigation guardian Barbara Ramjan v Attorney General of NSW", HAINES_CATCH, "Mental health"),
                              ("GXT25 v Minister for Immigration and Citizenship", GXT25_CATCH, "Administrative law"),
                              ("Coal Mining Industry (Long Service Leave Funding) Corporation v Orica Australia Pty Ltd", ORICA_CATCH, "Industrial law (Cth)")):
        it = _hca(name, f"{name} [2026] HCA 30")
        it["catchwords"] = catch
        ok, why = u.auto_analysis_ok(it)
        assert not ok and f"area: {area}" in why, (name, why)
    it = _hca("Ko v The King", "Ko v The King [2026] HCA 29")
    it["catchwords"] = "Criminal practice – Trial – Adequacy of jury directions – Attempted importation of a border controlled drug."
    assert u.auto_analysis_ok(it)[0]
    it = _hca("Smith v Jones", "Smith v Jones [2026] HCA 40")            # a civil name, an evidence subject
    it["catchwords"] = "Evidence – Admissibility – Tendency evidence – Whether probative value substantially outweighs prejudice."
    assert u.auto_analysis_ok(it)[0]
    it = _hca("Smith v Jones", "Smith v Jones [2026] HCA 40")            # no catchwords: the old rules apply
    assert not u.auto_analysis_ok(it)[0]
    it["catchwords"] = ""
    assert not u.auto_analysis_ok(it)[0]


def test_pending_record_carries_the_catchwords():
    rec = u._pending_record({"id": "hca-2026-31", "citation": "[2026] HCA 31", "courtTag": "HCA", "year": "2026",
                             "num": "31", "catchwords": "Legal practitioners – Costs",
                             "hcaMeta": {"decided": "09/09/2026", "pdf": "https://www.hcourt.gov.au/x/31.pdf"}})
    assert rec["catchwords"] == "Legal practitioners – Costs"
    assert rec["hcaMeta"] == {"decided": "09/09/2026", "pdf": "https://www.hcourt.gov.au/x/31.pdf"}
    assert u._pending_record({"id": "x", "citation": "[2026] HCA 1", "courtTag": "HCA", "year": "2026",
                              "num": "1"})["hcaMeta"] == {}
    assert u._pending_record({"id": "x", "citation": "[2026] HCA 1", "courtTag": "HCA", "year": "2026",
                              "num": "1"})["catchwords"] == ""


def test_auto_analysis_holds_civil_hca():
    # the case that actually shipped unreviewed (hca-1998-11), framed as
    # generously as possible — as a clean, single-case alert link
    ok, why = u.auto_analysis_ok(_hca(
        "Oshlack v Richmond River Council",
        "Oshlack v Richmond River Council (1998) 193 CLR 72; [1998] HCA 11 - costs - "
        "public interest litigation - discretion of the primary judge"))
    assert not ok and "no criminal party" in why, why
    # and a contaminated blurb must not rescue a civil case: the live Concut row
    # matches TOPIC_KEYWORDS only on "Aborig" bleeding in from the next citation
    ok, _ = u.auto_analysis_ok(_hca(
        "Concut Pty Ltd v Worrell",
        "Concut Pty Ltd v Worrell [2000] HCA 64; Koompahtoo Local Aboriginal Land "
        "Council v Sanpine Pty Ltd [2007] HCA 61"))
    assert not ok, "civil employment case rescued by a neighbour's catchwords"


def test_auto_analysis_holds_an_unverified_name_in_any_court():
    # build_case writes caseName straight into cases.json and the .md front
    # matter, so a name we could not parse must never be published unreviewed —
    # including one that arrived through the LINK pass (live [2026] HCASJ 31).
    for it in (_hca("rkson v The Queen (2011) 32 VR 361; [2011] VSCA 157; Hili v The Queen",
                    via="scan"),
               _hca("ter Of An Application BY Thomas William Raymond Towle for Leave to Iss",
                    tag="HCASJ"),
               _hca("Kleindyk v The Queen [2016] WASCA 123; Lam v The King", tag="QCA")):
        ok, why = u.auto_analysis_ok(it)
        assert not ok and "could not be parsed" in why, (it["caseName"], why)


def test_auto_analysis_is_not_a_scope_filter():
    # WASC/WASCA civil noise is 7.3's problem, and the gated persuasive courts
    # already passed a topic gate in in_scope()
    for tag in ("WASC", "WASCA", "QCA", "NTSC"):
        ok, why = u.auto_analysis_ok(_hca("Haskett v Jago", tag=tag))
        assert ok, f"gate is not a scope filter — {tag} must pass ({why})"


def test_auto_analysis_exempts_email_submissions():
    ok, _ = u.auto_analysis_ok(_hca("Oshlack v Richmond River Council", blurb="",
                                    via="submission", suppliedText="x" * 900))
    assert ok, "a judgment supplied by hand must never be held"


def test_pending_record_carries_the_gate_fields():
    # state.json is the only thing that survives a run: drop any of these and the
    # gate forgets provenance (publishable again) or re-emails every run.
    rec = u._pending_record({
        "id": "hca-1998-11", "citation": "[1998] HCA 11", "courtTag": "HCA",
        "year": "1998", "num": "11", "caseName": "Oshlack v Richmond River Council",
        "via": "scan", "nameSuspect": True, "holdReason": "no criminal party",
        "heldNotified": True, "notified": True})
    assert rec["via"] == "scan" and rec["nameSuspect"] is True
    assert rec["holdReason"] == "no criminal party" and rec["heldNotified"] is True


# ---------------------------------------------------------------------------
# HANDOFF 7.1 — data/blocklist.json: ids the pipeline must never add. A missing
# file is fine; a BROKEN one stops the run (once Oshlack is out of cases.json
# this file is the only thing stopping the next alert that cites it re-adding it,
# so degrading to "block nothing" would silently restore the bug).
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _blocklist_file(content):
    orig = u.BLOCKLIST_PATH
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "blocklist.json"
        if content is not None:
            p.write_text(content, encoding="utf-8")
        u.BLOCKLIST_PATH = p
        try:
            yield p
        finally:
            u.BLOCKLIST_PATH = orig


def _entry(**kw):
    e = {"id": "hca-1998-11", "citation": "[1998] HCA 11",
         "caseName": "Oshlack v Richmond River Council", "reason": "civil costs case"}
    e.update(kw)
    return json.dumps({"blocked": [e]})


def test_blocklist_id_matches_the_pipelines_own_ids():
    for cite in ("[1998] HCA 11", "[2026] WASCA 111", "[2026] WASC 377"):
        m = u.CITATION_RE.search(cite)
        assert u.blocklist_id_for_citation(cite) == u._item_from_match(m, "", "", "")["id"]
    for junk in ("Oshlack v Richmond River Council", "", None,
                 "[2015] VSCA 52; Kleindyk v The Queen [2016] WASCA 123"):
        assert u.blocklist_id_for_citation(junk) is None, junk


def test_blocklist_loads_a_good_entry_and_missing_file_is_fine():
    with _blocklist_file(_entry()):
        b = u.load_blocklist()
    assert set(b) == {"hca-1998-11"}
    assert b["hca-1998-11"]["citation"] == "[1998] HCA 11"
    with _blocklist_file(None):
        assert u.load_blocklist() == {}
    with _blocklist_file("   \n"):
        assert u.load_blocklist() == {}


def test_blocklist_refuses_anything_it_cannot_fully_verify():
    # blocking the wrong id is a SILENT FALSE DROP of a real criminal decision,
    # so an entry that cannot be cross-checked must stop the run, not be applied.
    for content in ("{", "not json", "null", '{"blocked": "hca-1998-11"}',
                    '{"nope": []}', '["hca-1998-11"]',
                    _entry(citation="[1998] HCA 12"),      # id/citation disagree
                    _entry(id="hca-1998-1"),               # one-character typo
                    _entry(citation=""),                   # no cross-check possible
                    _entry(caseName=""), _entry(reason="")):
        with _blocklist_file(content):
            try:
                u.load_blocklist()
            except SystemExit:
                continue
            raise AssertionError(f"unverifiable blocklist accepted: {content[:60]!r}")


def test_drop_blocked_clears_the_single_choke_point():
    blocked = {"hca-1998-11": {"id": "hca-1998-11", "citation": "[1998] HCA 11",
                               "caseName": "Oshlack", "reason": "civil"}}
    work = {"hca-1998-11": {"id": "hca-1998-11", "citation": "[1998] HCA 11"},
            "wasca-2026-111": {"id": "wasca-2026-111", "citation": "[2026] WASCA 111"},
            "hca-2026-28": {"id": "hca-2026-28", "citation": "[2026] HCA 28"}}
    assert [d["id"] for d in u.drop_blocked(work, blocked)] == ["hca-1998-11"]
    # gone from analysis, from cases.json AND from the pending queue (all three
    # are built from work{}), while the two genuine keeps are untouched
    assert set(work) == {"wasca-2026-111", "hca-2026-28"}
    # a judgment supplied by hand overrides the note rather than vanishing
    sub = {"hca-1998-11": {"id": "hca-1998-11", "citation": "[1998] HCA 11",
                           "_msgid": "<m@x>", "suppliedText": "y" * 900}}
    assert u.drop_blocked(sub, blocked) == [] and "hca-1998-11" in sub
    assert "BLOCKED hca-1998-11" in u.blocked_line(blocked["hca-1998-11"])


def test_shipped_blocklist_is_present_valid_and_blocks_no_real_case():
    # guards the repo's own data/blocklist.json, not just the loader
    assert u.BLOCKLIST_PATH.exists(), "data/blocklist.json is required (HANDOFF 7.1)"
    loaded = u.load_blocklist()          # dies if any entry cannot be verified
    assert "hca-1998-11" in loaded, "Oshlack must stay blocked while it is out of cases.json"
    cases = json.loads((u.CASES_PATH).read_text(encoding="utf-8"))
    for cid in loaded:
        assert cid not in {c.get("id") for c in cases}, \
            f"{cid} is blocked but still in cases.json — remove it by hand (HANDOFF 7.1)"
    pending = u.load_state().get("pending", [])
    for cid in loaded:
        assert cid not in {p.get("id") for p in pending}, \
            f"{cid} is blocked but still queued in state.json — prune it (HANDOFF 7.6)"
    # wasca-2026-129 (MRV v SNW [No 2]) was on this list from its watchlist name alone;
    # the judgment turned out to be a Family Court property/parenting appeal (CACV 32 of
    # 2025) and is blocklisted on purpose (14/09/2026).
    for cid in ("wasca-2026-111", "wasca-2026-113", "wasca-2026-114", "wasca-2026-123",
                "wasca-2026-128", "wasc-2026-331", "wasc-2026-356",
                "wasc-2026-359", "wasc-2026-377", "wasc-2026-380", "hca-2026-28",
                "hca-2026-29"):
        assert cid not in loaded, f"blocklist wrongly blocks a genuine criminal case: {cid}"
    assert "wasca-2026-129" in loaded, "MRV v SNW [No 2] is civil and must stay blocked"


def test_screened_seen_round_trips_so_a_drop_is_reported_once():
    """The 'screened out' email is the ONLY safety net for the WASC positive-signal
    rule, so it must be read. With LOOKBACK_DAYS = 3 and three runs a day the same
    alert is re-read up to nine times; without screenedSeen the same names reprint
    in up to nine consecutive emails and become noise. Lock the round-trip."""
    import tempfile, pathlib as _pl
    with tempfile.TemporaryDirectory() as d:
        orig = u.STATE_PATH
        try:
            u.STATE_PATH = _pl.Path(d) / "state.json"
            # a fresh/absent state still exposes the key, defaulted
            assert u.load_state()["screenedSeen"] == []
            u.save_state([], ["<msg@id>"], ["wasc-2026-999", "hcasj-2026-99"])
            back = u.load_state()
            assert back["screenedSeen"] == ["wasc-2026-999", "hcasj-2026-99"]
            assert back["processed"] == ["<msg@id>"]
            # byte-stable when nothing changed -> quiet runs make no commit
            first = u.STATE_PATH.read_bytes()
            u.save_state([], ["<msg@id>"], ["wasc-2026-999", "hcasj-2026-99"])
            assert u.STATE_PATH.read_bytes() == first
            # a legacy state.json with no screenedSeen key must not crash
            u.STATE_PATH.write_text('{"pending": [], "processed": []}', encoding="utf-8")
            assert u.load_state()["screenedSeen"] == []
        finally:
            u.STATE_PATH = orig


# ---------------------------------------------------------------------------
# small regression locks for adjacent helpers
# ---------------------------------------------------------------------------
def test_jade_url_validation():
    assert u._is_jade_url("https://jade.io/article/123")
    assert u._is_jade_url("https://www.jade.io/x")
    assert not u._is_jade_url("https://evil.example/jade.io")
    assert not u._is_jade_url("javascript:alert(1)")
    assert not u._is_jade_url("")


def test_austlii_url_is_empty_for_a_reported_citation():
    assert u.austlii_url({"courtTag": "NSWSC", "year": "1971", "num": ""}) == ""
    assert u.austlii_url({"courtTag": "WASCA", "year": "2026", "num": "111"}).endswith("/wa/WASCA/2026/111.html")


def test_dmy_to_iso():
    assert u.dmy_to_iso("07/09/2022") == "2022-09-07"
    assert u.dmy_to_iso("7/9/2022") == "2022-09-07"
    assert u.dmy_to_iso("2022-09-07") == "2022-09-07"
    assert u.dmy_to_iso("September 2022") is None


# ---------------------------------------------------------------------------
# write_llm_file — the .md writer; `source` is optional provenance (session 11)
# ---------------------------------------------------------------------------
def _sample_case():
    return {"id": "wasc-2099-1", "caseName": "Test v Case", "citation": "[2099] WASC 1",
            "court": "Supreme Court of Western Australia", "decided": "01/01/2099",
            "relevance": "AWARENESS", "austliiUrl": "https://example.invalid/1",
            "tags": ["one", "two"], "oneLine": "One <i>line</i>.", "whatHappened": "A <b>b</b>.",
            "whatHeld": "C.", "whatItMeans": "D.", "verdict": "AWARENESS — e.", "files": {}}


def test_write_llm_file_default_shape_is_unchanged(tmp_files_dir=None):
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        old = u.FILES_DIR
        try:
            u.FILES_DIR = Path(d)
            case = _sample_case()
            u.write_llm_file(case, "JUDGMENT BODY", {})
            md = (Path(d) / "wasc-2099-1" / "wasc-2099-1.md").read_text(encoding="utf-8")
        finally:
            u.FILES_DIR = old
    assert "source:" not in md
    assert "## Full judgment (source text)\n\nJUDGMENT BODY\n" in md
    assert 'tags: ["one", "two"]' in md
    assert "## What happened\nA b.\n" in md              # tags stripped in the .md
    assert "## One line\nOne <i>line</i>.\n" in md        # oneLine keeps its markup
    assert case["files"] == {"llm": "data/files/wasc-2099-1/wasc-2099-1.md"}


def test_write_llm_file_records_source_when_given():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        old = u.FILES_DIR
        try:
            u.FILES_DIR = Path(d)
            u.write_llm_file(_sample_case(), "BODY", {"flags": ["check s 1"]},
                             source='Cases/Test v Case - [2099] WASC 1.doc')
            md = (Path(d) / "wasc-2099-1" / "wasc-2099-1.md").read_text(encoding="utf-8")
        finally:
            u.FILES_DIR = old
    fm = md.split("---")[1]
    assert 'source: "Cases/Test v Case - [2099] WASC 1.doc"' in fm
    assert fm.index("austliiUrl:") < fm.index("source:") < fm.index("tags:")   # attach_text.py order
    assert "## Full judgment (source text - Cases/Test v Case - [2099] WASC 1.doc)\n\nBODY\n" in md
    assert "## Flags (verify before relying)\n- check s 1\n" in md

# ---------------------------------------------------------------------------
# zero-dependency runner
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# The daily bot end to end — the Court route and the fact-check (20/09/2026).
# Everything external is stubbed: IMAP, the corpus, the Court's site, the two
# model calls, git, SMTP. What is pinned:
#   * a pending HCA item the corpus cannot supply is resolved from the Court's own
#     site, gated on the Court's catchwords BEFORE any download, and published with
#     the Court's judgment date and the licence attribution in the .md;
#   * a Court item whose catchwords are not criminal is HELD on the watchlist with
#     no download and no model call;
#   * every built case is audited; a PROBLEMS verdict holds it (no call, report on
#     disk under data/audits/, report text in the email); a CLEAN one keeps its call.
# ---------------------------------------------------------------------------
BOT_ANALYSIS = {
    "oneLine": "A jury-direction appeal.", "whatHappened": "KO was tried.", "whatHeld": "Appeal <b>dismissed</b>.",
    "whatItMeans": "Direct the jury properly.", "verdict": "AWARENESS — a directions case.",
    "outcome": "Appeal dismissed", "weight": "Full court", "tags": ["jury directions"], "relevance": "AWARENESS",
    "decided": "01/01/2026", "appealFrom": "NSWCCA", "flags": [],
}
BOT_CLEAN = {"verdict": "CLEAN", "problems": [], "unconfirmed": []}
BOT_DIRTY = {"verdict": "PROBLEMS", "unconfirmed": [],
             "problems": [{"field": "whatHeld", "claim": "dismissed", "why": "it was allowed", "judgmentSays": "Appeal allowed."}]}


@contextlib.contextmanager
def _bot_sandbox(pending, metas, texts, analysis=None, audit=None):
    """Temp data dir; stubs for IMAP/corpus/Court/model/git/mail. Yields a dict that
    collects what the bot did (emails, pushes, downloads)."""
    import hca
    import audit as AU
    saved = (u.DATA, u.CASES_PATH, u.STATE_PATH, u.BLOCKLIST_PATH, u.FILES_DIR, u.fetch_alert_html,
             u.fetch_submissions, u.fetch_judgment_text, hca.lookup, hca.fetch_text, u.analyse, u.get_client,
             AU.audit_case, u.commit_and_push, u.send_email, u.send_watchlist_email, u.send_health_email)
    seen = {"emails": [], "watchlist": [], "health": [], "pushed": [], "downloads": [], "analysed": []}
    with tempfile.TemporaryDirectory() as d:
        u.DATA = pathlib.Path(d)
        u.CASES_PATH = u.DATA / "cases.json"
        u.STATE_PATH = u.DATA / "state.json"
        u.BLOCKLIST_PATH = u.DATA / "blocklist.json"
        u.FILES_DIR = u.DATA / "files"
        u.CASES_PATH.write_text("[]", encoding="utf-8")
        u.STATE_PATH.write_text(json.dumps({"pending": pending, "processed": [], "screenedSeen": []}), encoding="utf-8")
        u.fetch_alert_html = lambda user, pw, since: []
        u.fetch_submissions = lambda user, pw, since, processed: []
        u.fetch_judgment_text = lambda citation: None
        hca.lookup = lambda citation, name="", **kw: metas.get(citation)

        def fetch_text(meta):
            seen["downloads"].append(meta["citation"])
            return texts[meta["citation"]], ["no counsel / solicitors block found"], hca.source_label(meta)
        hca.fetch_text = fetch_text

        def analyse(client, item, text, truncated):
            seen["analysed"].append(item["id"])
            return json.loads(json.dumps(analysis or BOT_ANALYSIS))
        u.analyse = analyse
        u.get_client = lambda: object()
        AU.audit_case = audit or (lambda client, case, text: json.loads(json.dumps(BOT_CLEAN)))
        u.commit_and_push = lambda label: (seen["pushed"].append(label), True)[1]
        u.send_email = lambda user, pw, cases, stats=None, held=None: seen["emails"].append((cases, stats, held))
        u.send_watchlist_email = lambda user, pw, items, stats=None: seen["watchlist"].append((items, stats))
        u.send_health_email = lambda user, pw, stats, errors, gave_up: seen["health"].append(stats)
        os.environ.setdefault("MAIL_USERNAME", "bot@example.invalid")
        os.environ.setdefault("MAIL_PASSWORD", "x")
        try:
            yield seen
        finally:
            (u.DATA, u.CASES_PATH, u.STATE_PATH, u.BLOCKLIST_PATH, u.FILES_DIR, u.fetch_alert_html,
             u.fetch_submissions, u.fetch_judgment_text, hca.lookup, hca.fetch_text, u.analyse, u.get_client,
             AU.audit_case, u.commit_and_push, u.send_email, u.send_watchlist_email, u.send_health_email) = saved


def _pending_hca(num, name, first_seen="2026-09-09T00:00:00+00:00"):
    return {"id": f"hca-2026-{num}", "citation": f"[2026] HCA {num}", "courtTag": "HCA", "year": "2026",
            "num": str(num), "caseName": name, "jadeUrl": "", "blurb": name, "via": "link",
            "nameSuspect": False, "firstSeen": first_seen, "notified": True, "holdReason": "", "heldNotified": False}


KO_META = {"name": "The King v Ko", "citation": "[2026] HCA 29", "decided": "12/08/2026", "caseNumber": "S172/2025",
           "coram": "Gageler CJ", "catchwords": "Criminal practice – Trial – Adequacy of jury directions.",
           "pdf": "https://www.hcourt.gov.au/sites/default/files/eresources/2026-08-12/HCA/Ko.pdf", "docx": "",
           "url": "https://www.hcourt.gov.au/x/king-v-ko"}
EGH_META = dict(KO_META, name="EGH19 v Minister for Immigration & Citizenship", citation="[2026] HCA 33",
                catchwords="Migration – Visa cancellation – Jurisdictional error.", pdf="https://www.hcourt.gov.au/x/33.pdf")
KO_TEXT = "HIGH COURT OF AUSTRALIA\nGAGELER CJ\n\nThe King v Ko [2026] HCA 29\n\n" + ("The appeal is dismissed. " * 80)


def test_bot_resolves_hca_from_the_court_gates_on_catchwords_and_audits():
    pending = [_pending_hca(29, "The King v Ko"), _pending_hca(33, "EGH19 v Minister for Immigration & Citizenship")]
    with _bot_sandbox(pending, {"[2026] HCA 29": KO_META, "[2026] HCA 33": EGH_META}, {"[2026] HCA 29": KO_TEXT}) as seen:
        u.main()
        cases = json.loads(u.CASES_PATH.read_text(encoding="utf-8"))
        state = json.loads(u.STATE_PATH.read_text(encoding="utf-8"))
        md = (u.FILES_DIR / "hca-2026-29" / "hca-2026-29.md").read_text(encoding="utf-8")
        reports = sorted(p.name for p in (u.DATA / "audits").rglob("*.md"))
    # Ko: fetched from the Court, published with a call, the Court's date, the attribution
    assert seen["downloads"] == ["[2026] HCA 29"] and seen["analysed"] == ["hca-2026-29"]
    assert [c["id"] for c in cases] == ["hca-2026-29"]
    ko = cases[0]
    assert ko["relevance"] == "AWARENESS" and "needsReview" not in ko
    assert ko["decided"] == "12/08/2026" and ko["date"] == "2026-08-12"      # the Court's date beat the model's 01/01/2026
    assert ko["caseName"] == "The King v Ko"
    assert "cleaner: no counsel / solicitors block found" in ko["flags"]
    assert 'source: "High Court of Australia (copy of the version at https://www.hcourt.gov.au/sites/default/files/eresources/2026-08-12/HCA/Ko.pdf)"' in md
    assert reports == ["hca-2026-29.md"]                                      # audited, CLEAN, report kept
    # EGH19: the Court's catchwords say migration -> held on the watchlist, no download, no model call
    held = [p for p in state["pending"] if p["id"] == "hca-2026-33"]
    assert held and "catchwords are not criminal" in held[0]["holdReason"] and "Migration" in held[0]["holdReason"]
    assert held[0]["catchwords"].startswith("Migration") and held[0]["caseName"].startswith("EGH19")
    assert not any(p["id"] == "hca-2026-29" for p in state["pending"])
    # one new-cases email (nothing held by the audit), one watchlist email carrying the hold, one push
    assert len(seen["emails"]) == 1 and seen["emails"][0][2] == [] and seen["emails"][0][1]["auditHeld"] == 0
    assert len(seen["watchlist"]) == 1 and [w["id"] for w in seen["watchlist"][0][0]] == ["hca-2026-33"]
    assert seen["pushed"] and seen["pushed"][0].startswith("Pipeline: add 1 case")


def test_bot_does_not_ask_the_court_again_for_a_case_its_page_already_held():
    import hca
    # 33: held last run on the Court's catchwords; 31: the Court's page had no
    # catchwords and the name rule held it (hcaMeta stored, caseName from the Court)
    egh = _pending_hca(33, "EGH19 v Minister for Immigration & Citizenship")
    egh["catchwords"] = EGH_META["catchwords"]
    egh["holdReason"] = "HCA: the Court's catchwords are not criminal — area: Migration — 'Migration – Visa'"
    egh["heldNotified"] = True
    daily = _pending_hca(31, "R Lawyers v Mr Daily [No 2]")
    daily["hcaMeta"] = {"decided": "09/09/2026", "coram": "Gageler CJ", "caseNumber": "A8/2025",
                        "url": "https://www.hcourt.gov.au/x/31", "pdf": "https://www.hcourt.gov.au/x/31.pdf"}
    daily["holdReason"] = "HCA: no criminal party in the case name and no single-case investigation/evidence topic"
    daily["heldNotified"] = True
    calls = []
    with _bot_sandbox([egh, daily], {"[2026] HCA 33": EGH_META}, {}) as seen:
        inner = hca.lookup
        hca.lookup = lambda citation, name="", **kw: (calls.append(citation), inner(citation, name, **kw))[1]
        u.main()
        assert calls == [], f"the Court was asked again for a case its own page already held: {calls}"
        assert seen["downloads"] == [] and seen["analysed"] == []
        state = json.loads(u.STATE_PATH.read_text(encoding="utf-8"))
        by_id = {p["id"]: p for p in state["pending"]}
        assert "area: Migration" in by_id["hca-2026-33"]["holdReason"]
        assert by_id["hca-2026-33"]["catchwords"].startswith("Migration")
        assert "no criminal party" in by_id["hca-2026-31"]["holdReason"]
        assert by_id["hca-2026-31"]["hcaMeta"]["pdf"].endswith("/31.pdf")      # survives the round trip
    # a stored gate verdict is re-read every run: catchwords that PASS go to the Court and the download
    ko = _pending_hca(29, "The King v Ko")
    ko["catchwords"] = KO_META["catchwords"]
    with _bot_sandbox([ko], {"[2026] HCA 29": KO_META}, {"[2026] HCA 29": KO_TEXT}) as seen:
        u.main()
        assert seen["downloads"] == ["[2026] HCA 29"] and seen["analysed"] == ["hca-2026-29"]


def test_bot_holds_a_case_the_audit_fails_and_reports_it():
    pending = [_pending_hca(29, "The King v Ko")]
    with _bot_sandbox(pending, {"[2026] HCA 29": KO_META}, {"[2026] HCA 29": KO_TEXT},
                      audit=lambda client, case, text: json.loads(json.dumps(BOT_DIRTY))) as seen:
        u.main()
        cases = json.loads(u.CASES_PATH.read_text(encoding="utf-8"))
        md = (u.FILES_DIR / "hca-2026-29" / "hca-2026-29.md").read_text(encoding="utf-8")
        report_files = list((u.DATA / "audits").rglob("hca-2026-29.md"))
        report_text = report_files[0].read_text(encoding="utf-8") if report_files else ""
    ko = cases[0]
    assert ko["relevance"] == "" and ko["needsReview"]["problems"] == 1 and ko["needsReview"]["call"] == "AWARENESS"
    assert ko["needsReview"]["report"].startswith("data/audits/") and ko["needsReview"]["report"].endswith("hca-2026-29.md")
    assert "VERDICT: PROBLEMS" in report_text and "it was allowed" in report_text and "daily bot" in report_text
    assert "needsReview: true" in md
    cases_sent, stats, held = seen["emails"][0]
    assert stats["auditHeld"] == 1 and len(held) == 1 and held[0][0]["id"] == "hca-2026-29"
    assert "it was allowed" in held[0][1]


def test_bot_holds_when_the_audit_call_itself_fails():
    def broken(client, case, text):
        raise RuntimeError("audit refused by safety classifier")
    with _bot_sandbox([_pending_hca(29, "The King v Ko")], {"[2026] HCA 29": KO_META},
                      {"[2026] HCA 29": KO_TEXT}, audit=broken) as seen:
        u.main()
        cases = json.loads(u.CASES_PATH.read_text(encoding="utf-8"))
    assert cases[0]["relevance"] == "" and cases[0]["needsReview"]["problems"] == 1
    assert "audit call failed" in seen["emails"][0][2][0][1]


def _main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERR  {name}: {type(e).__name__}: {e}")
    total = len(tests)
    print(f"\n{total - failed}/{total} passed" + ("" if not failed else f", {failed} FAILED"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
