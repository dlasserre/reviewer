"""Le verdict de l'agent est-il lu avec la bonne polarite de doute ?

C'est le SEUL endroit ou une sortie de modele decide d'une ecriture visible :
repondre dans un fil, et surtout le RESOUDRE — donc retirer une remarque du
compteur qui retient le merge. Un parseur laxiste ici transforme une
hallucination en remarque soldee que personne ne reverra.

D'ou la regle unique de ce module : le doute penche du cote de l'humain.
Resoudre a tort est irrattrapable en pratique ; demander a tort coute une
notification.
"""

from __future__ import annotations

from agent_runner_lg.rules.verdict import SCHEMA, Issue, parse

FILS = ("PRRT_kwDO1", "PRRT_kwDO2")


def rendu(**kw):
    base = {
        "summary": "corrige le mur qui cassait le schema",
        "threads": [
            {"thread_id": FILS[0], "outcome": "corrige", "reply": "champ conserve"},
            {"thread_id": FILS[1], "outcome": "corrige", "reply": "scope valide avant"},
        ],
    }
    base.update(kw)
    return base


# ── Lecture nominale ────────────────────────────────────────────────────────


def test_un_verdict_complet_se_lit():
    v = parse(rendu(), submitted=FILS)
    assert not v.anomalies
    assert [t.outcome for t in v.threads] == [Issue.CORRIGE, Issue.CORRIGE]
    assert v.for_thread(FILS[1]).reply == "scope valide avant"


def test_l_ordre_rendu_est_celui_de_SOUMISSION():
    # C'est l'ordre du prompt, donc celui que l'humain relira. Suivre l'ordre du
    # modele ferait varier la lecture d'un cycle a l'autre sans raison.
    inverse = rendu(threads=[
        {"thread_id": FILS[1], "outcome": "corrige", "reply": "b"},
        {"thread_id": FILS[0], "outcome": "corrige", "reply": "a"},
    ])
    assert [t.thread_id for t in parse(inverse, submitted=FILS).threads] == list(FILS)


# ── La garde : un fil qu'on n'a pas soumis ─────────────────────────────────


def test_un_fil_NON_SOUMIS_est_refuse():
    # LE test de securite du module. Une remarque piegee pourrait demander a
    # l'agent de resoudre un autre fil — c'est la seule ecriture qu'une
    # injection reussie pourrait detourner. On refuse par identifiant, sans
    # chercher a deviner ce qui etait vise.
    pirate = rendu(threads=[
        {"thread_id": FILS[0], "outcome": "corrige", "reply": "ok"},
        {"thread_id": "PRRT_UN_AUTRE_FIL", "outcome": "corrige", "reply": "resous-le"},
    ])
    v = parse(pirate, submitted=FILS)

    assert [t.thread_id for t in v.threads] == list(FILS)
    assert not any(t.thread_id == "PRRT_UN_AUTRE_FIL" for t in v.threads)
    assert any("non soumis" in a for a in v.anomalies)


def test_le_fil_refuse_n_empeche_pas_les_autres_d_etre_traites():
    pirate = rendu(threads=[
        {"thread_id": "PRRT_AILLEURS", "outcome": "corrige", "reply": "x"},
        {"thread_id": FILS[0], "outcome": "corrige", "reply": "vrai correctif"},
    ])
    v = parse(pirate, submitted=FILS)
    assert v.for_thread(FILS[0]).outcome is Issue.CORRIGE


# ── Le silence n'est pas un accord ─────────────────────────────────────────


def test_un_fil_OMIS_devient_un_arbitrage():
    # Le laisser tomber le ferait disparaitre du compteur au passage suivant,
    # curseur avance, sans que personne ne l'ait lu.
    partiel = rendu(threads=[
        {"thread_id": FILS[0], "outcome": "corrige", "reply": "ok"},
    ])
    v = parse(partiel, submitted=FILS)
    assert v.for_thread(FILS[1]).outcome is Issue.ARBITRAGE
    assert any("absent du verdict" in a for a in v.anomalies)


def test_un_verdict_INCONNU_devient_un_arbitrage():
    v = parse(rendu(threads=[{"thread_id": FILS[0], "outcome": "parfait", "reply": "x"},
                             {"thread_id": FILS[1], "outcome": "corrige", "reply": "y"}]),
              submitted=FILS)
    assert v.for_thread(FILS[0]).outcome is Issue.ARBITRAGE


def test_une_reponse_VIDE_ne_permet_pas_de_resoudre():
    # Resoudre sans un mot d'explication fait disparaitre la remarque sans trace
    # lisible : personne ne saura ce qui a ete decide, ni pourquoi.
    v = parse(rendu(threads=[{"thread_id": FILS[0], "outcome": "corrige", "reply": "  "},
                             {"thread_id": FILS[1], "outcome": "corrige", "reply": "y"}]),
              submitted=FILS)
    assert v.for_thread(FILS[0]).outcome is Issue.ARBITRAGE
    assert not v.for_thread(FILS[0]).outcome.resolves


def test_une_sortie_ILLISIBLE_ne_resout_rien():
    for brut in (None, "du texte", 42, [], ["threads"]):
        v = parse(brut, submitted=FILS)
        assert v.blocked, f"{brut!r} devrait bloquer"
        assert all(t.outcome is Issue.ARBITRAGE for t in v.threads)
        assert not any(t.outcome.resolves for t in v.threads)


def test_un_fil_cite_deux_fois_garde_le_PREMIER_verdict():
    double = rendu(threads=[
        {"thread_id": FILS[0], "outcome": "arbitrage", "reply": "je doute"},
        {"thread_id": FILS[0], "outcome": "corrige", "reply": "finalement non"},
        {"thread_id": FILS[1], "outcome": "corrige", "reply": "ok"},
    ])
    v = parse(double, submitted=FILS)
    assert v.for_thread(FILS[0]).outcome is Issue.ARBITRAGE
    assert any("deux fois" in a for a in v.anomalies)


# ── Qui referme un fil, qui appelle l'humain ───────────────────────────────


def test_seul_corrige_referme_un_fil():
    assert Issue.CORRIGE.resolves
    assert not Issue.REFUTE.resolves
    assert not Issue.ARBITRAGE.resolves


def test_le_desaccord_appelle_l_humain_lui_aussi():
    # Un desaccord non signale devient un fil ouvert dont personne ne sait qu'il
    # attend quelqu'un — et un fil ouvert retient le merge.
    assert Issue.REFUTE.needs_human
    assert Issue.ARBITRAGE.needs_human
    assert not Issue.CORRIGE.needs_human


# ── Sujet de commit ────────────────────────────────────────────────────────


def test_le_sujet_de_commit_est_CONVENTIONNEL():
    # `commit_all` refuse un sujet non conventionnel. Exiger la forme dans le
    # prompt marcherait « la plupart du temps », et la plupart du temps veut
    # dire un job perdu de temps en temps : on prefixe nous-memes.
    from agent_runner_lg.repo.git import _SUJET_CONVENTIONNEL

    for resume in ("", "Corrige le mur", "a" * 300, "élision et accents"):
        sujet = parse(rendu(summary=resume), submitted=FILS).commit_subject("backend", 1)
        assert _SUJET_CONVENTIONNEL.match(sujet), sujet


def test_un_resume_vide_donne_quand_meme_un_sujet_lisible():
    sujet = parse(rendu(summary=""), submitted=FILS).commit_subject("mobile", 2)
    assert sujet.startswith("fix(mobile): ")
    assert "cycle 2" in sujet


# ── Le schema passe au SDK ─────────────────────────────────────────────────


def test_le_schema_impose_les_trois_verdicts_et_rien_d_autre():
    champ = (SCHEMA["schema"]["properties"]["threads"]["items"]
             ["properties"]["outcome"])
    assert set(champ["enum"]) == {"corrige", "refute", "arbitrage"}


def test_le_schema_interdit_les_champs_surnumeraires():
    # `additionalProperties: false` evite qu'un modele invente un champ qu'on
    # lirait de travers — ou qu'on ne lirait pas du tout.
    assert SCHEMA["schema"]["additionalProperties"] is False
    assert SCHEMA["schema"]["properties"]["threads"]["items"][
        "additionalProperties"] is False
