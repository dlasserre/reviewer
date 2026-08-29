"""La machine a etats decide-t-elle juste ?

Les cas sont ecrits d'apres des PR REELLES, pas d'apres le schema qu'on aurait
souhaite : c'est la mesure qui a impose la conception, notamment le fait que les
relecteurs automatiques n'emettent jamais `CHANGES_REQUESTED`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from reviewer.rules.machine import (
    Action,
    Check,
    PullSnapshot,
    Severity,
    State,
    Thread,
    decide,
    severity_of,
)

T0 = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)
CODEX = "chatgpt-codex-connector[bot]"
COPILOT = "copilot-pull-request-reviewer[bot]"
TRUST = frozenset({CODEX.lower(), COPILOT.lower(), "dlasserre"})

BADGE_P1 = "**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-orange?style=flat)</sub></sub>  Ne court-circuitez pas la creation**"
BADGE_P3 = "**<sub><sub>![P3 Badge](https://img.shields.io/badge/P3-blue?style=flat)</sub></sub>  Nommage discutable**"


def pr(**kw) -> PullSnapshot:
    base = dict(number=714, repo="backend", head_sha="0584d4e")
    base.update(kw)
    return PullSnapshot(**base)


# « La phase de revue est passee » : checks conclus depuis longtemps, relance
# deja emise. Sans ca, une PR verte et sans remarque est en WAITING_REVIEW — ce
# qui est le comportement VOULU (Codex arrive ~4 min apres les checks), mais qui
# n'est pas ce que ces tests-la cherchent a montrer.
FINI = dict(checks_concluded_at=T0 - timedelta(hours=1), nudge_sent=True)


def vert(nom="Test backend") -> Check:
    return Check(nom, "completed", "success")


def fil(cid: int, body: str = BADGE_P1, *, author: str = CODEX, resolved: bool = False,
        path: str | None = "app/service.py", line: int | None = 751) -> Thread:
    return Thread(id=f"PRRT_{cid}", comment_id=cid, author=author,
                  resolved=resolved, body=body, path=path, line=line)


def d(snapshot, **kw):
    kw.setdefault("trusted_reviewers", TRUST)
    kw.setdefault("now", T0)
    return decide(snapshot, **kw)


# ── Le constat central : Codex ne dit jamais « changes requested » ──────────


def test_un_fil_ouvert_declenche_le_travail_meme_sans_changes_requested():
    # Toutes les revues Codex observees sont `COMMENTED`. L'etat de revue n'est
    # donc PAS le signal : ce sont les fils.
    r = d(pr(checks=(vert(),), threads=(fil(1),), checks_concluded_at=T0))
    assert r.state is State.NEEDS_FIX
    assert Action.RUN_AGENT in r.actions
    assert r.consumes_cycle


def test_un_fil_resolu_ne_declenche_rien():
    r = d(pr(checks=(vert(),), threads=(fil(1, resolved=True),), **FINI))
    assert r.state is State.READY_FOR_HUMAN


def test_un_auteur_hors_allowlist_est_ignore():
    # Un commentaire arbitraire ne doit pas pouvoir faire travailler l'agent.
    r = d(pr(checks=(vert(),), threads=(fil(1, author="passant-anonyme"),), **FINI))
    assert r.state is State.READY_FOR_HUMAN


def test_le_curseur_porte_sur_l_identifiant_pas_sur_la_date():
    # Une remarque re-ancree sur un nouveau commit change de `commit_id` et de
    # ligne, mais garde son `id`. La dater la ferait retraiter a chaque push.
    ancienne = fil(100)
    r = d(pr(checks=(vert(),), threads=(ancienne,), last_handled_comment_id=100, **FINI))
    assert r.state is State.READY_FOR_HUMAN

    nouvelle = fil(101)
    r = d(pr(checks=(vert(),), threads=(ancienne, nouvelle), last_handled_comment_id=100,
             checks_concluded_at=T0))
    assert r.state is State.NEEDS_FIX
    assert [t.comment_id for t in r.threads] == [101]


# ── Severite ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("corps", "attendu"), [
    (BADGE_P1, Severity.P1),
    (BADGE_P3, Severity.P3),
    ("![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat) Autre chose", Severity.P2),
    ("**P2** remarque en tete", Severity.P2),
    ("[P3] nommage", Severity.P3),
    ("Une remarque sans etiquette", Severity.UNKNOWN),
])
def test_la_severite_se_lit_dans_le_texte(corps, attendu):
    assert severity_of(corps) is attendu


def test_une_remarque_non_etiquetee_bloque():
    # Le doute penche du cote de l'humain : ignorer ce qu'on n'a pas su lire
    # transformerait chaque evolution du format en remarques perdues.
    assert Severity.UNKNOWN.blocking
    r = d(pr(checks=(vert(),), threads=(fil(1, "Remarque sans badge"),), checks_concluded_at=T0))
    assert r.state is State.NEEDS_FIX


def test_que_du_p3_ne_consomme_pas_de_cycle_et_propose_une_dette():
    r = d(pr(checks=(vert(),), threads=(fil(1, BADGE_P3), fil(2, BADGE_P3)),
             checks_concluded_at=T0))
    assert r.state is State.READY_FOR_HUMAN
    assert Action.PROPOSE_DEBT in r.actions
    assert Action.LABEL_READY in r.actions
    assert not r.consumes_cycle          # trois cycles ne se brulent pas sur du nommage
    assert len(r.threads) == 2           # mais on les rend, pour rediger la proposition


def test_un_p1_parmi_des_p3_rend_l_ensemble_bloquant():
    r = d(pr(checks=(vert(),), threads=(fil(1, BADGE_P3), fil(2, BADGE_P1)),
             checks_concluded_at=T0))
    assert r.state is State.NEEDS_FIX


# ── Checks ──────────────────────────────────────────────────────────────────


def test_un_check_en_echec_est_du_travail():
    r = d(pr(checks=(Check("Test backend", "completed", "failure"),), checks_concluded_at=T0))
    assert r.state is State.NEEDS_FIX
    assert r.consumes_cycle
    assert "Test backend" in r.reason


def test_un_check_rouge_passe_avant_les_fils():
    # Corriger une remarque sur une branche dont la CI est rouge produit un
    # correctif qu'on ne sait pas valider.
    r = d(pr(checks=(Check("Test backend", "completed", "failure"),),
             threads=(fil(1),), checks_concluded_at=T0))
    assert "check" in r.reason.lower()


@pytest.mark.parametrize("conclusion", ["cancelled", "stale"])
def test_un_run_annule_ou_perime_ne_dit_rien(conclusion):
    # `cancel-in-progress` annule des qu'un push rend un run caduc : c'est le
    # cas NORMAL. Le compter comme un echec produirait des blocages fantomes.
    r = d(pr(checks=(vert(), Check("Vieux run", "completed", conclusion)), **FINI))
    assert r.state is State.READY_FOR_HUMAN


@pytest.mark.parametrize("conclusion", ["timed_out", "action_required"])
def test_un_timeout_ou_une_action_requise_sont_de_vrais_echecs(conclusion):
    r = d(pr(checks=(Check("Test backend", "completed", conclusion),), checks_concluded_at=T0))
    assert r.state is State.NEEDS_FIX


def test_un_check_en_cours_fait_attendre():
    r = d(pr(checks=(vert(), Check("Build", "in_progress", None)), threads=(fil(1),)))
    assert r.state is State.WAITING_CI


@pytest.mark.parametrize("nom", [
    "quality-gate", "project-sync", "dependency-gate / check-dependencies",
    "branch-policy / check-branch-policy", "release", "claude",
])
def test_notre_propre_machinerie_ne_compte_pas(nom):
    # Ces checks jugent le PROCESSUS, pas le code livre. Les compter reviendrait
    # a bloquer une PR parce qu'une colonne manque dans un tableau.
    r = d(pr(checks=(vert(), Check(nom, "completed", "failure")), **FINI))
    assert r.state is State.READY_FOR_HUMAN


def test_un_skipping_de_notre_machinerie_ne_fait_pas_attendre():
    r = d(pr(checks=(vert(), Check("release", "queued", None)), **FINI))
    assert r.state is State.READY_FOR_HUMAN


# ── Fenetre de revue, et la relance ─────────────────────────────────────────


def test_la_fenetre_de_revue_fait_patienter():
    r = d(pr(checks=(vert(),), checks_concluded_at=T0 - timedelta(minutes=2)),
          review_window_s=600)
    assert r.state is State.WAITING_REVIEW
    assert "fenetre" in r.reason.lower()


def test_passe_la_fenetre_on_relance_une_fois():
    # 2 PR sur 12 n'ont recu aucune revue : attendre un evenement qui n'arrivera
    # peut-etre jamais impose une borne de temps.
    r = d(pr(checks=(vert(),), checks_concluded_at=T0 - timedelta(minutes=20)),
          review_window_s=600)
    assert r.state is State.WAITING_REVIEW
    assert Action.NUDGE_REVIEW in r.actions


def test_on_ne_relance_pas_deux_fois():
    r = d(pr(checks=(vert(),), checks_concluded_at=T0 - timedelta(minutes=20), nudge_sent=True),
          review_window_s=600)
    assert r.state is State.READY_FOR_HUMAN
    assert Action.NUDGE_REVIEW not in r.actions


def test_sans_relance_configuree_on_conclut_directement():
    r = d(pr(checks=(vert(),), checks_concluded_at=T0 - timedelta(minutes=20)),
          review_window_s=600, nudge_enabled=False)
    assert r.state is State.READY_FOR_HUMAN


def test_la_fenetre_part_de_la_fin_des_CHECKS_pas_de_l_ouverture():
    # Sur un gros depot, la CI dure plus longtemps que la fenetre : compter
    # depuis l'ouverture la ferait expirer avant que la revue soit possible.
    juste_fini = pr(checks=(vert(),), checks_concluded_at=T0)
    assert d(juste_fini, review_window_s=600).state is State.WAITING_REVIEW


# ── Cycles ──────────────────────────────────────────────────────────────────


def test_les_cycles_epuises_rendent_la_main():
    r = d(pr(checks=(vert(),), threads=(fil(1),), review_cycle=3, checks_concluded_at=T0),
          max_review_cycles=3)
    assert r.state is State.NEEDS_HUMAN
    assert Action.LABEL_NEEDS_HUMAN in r.actions
    assert Action.RUN_AGENT not in r.actions


def test_les_cycles_epuises_valent_aussi_pour_la_ci():
    r = d(pr(checks=(Check("Test backend", "completed", "failure"),), review_cycle=3,
             checks_concluded_at=T0), max_review_cycles=3)
    assert r.state is State.NEEDS_HUMAN


def test_le_dernier_cycle_travaille_encore():
    r = d(pr(checks=(vert(),), threads=(fil(1),), review_cycle=2, checks_concluded_at=T0),
          max_review_cycles=3)
    assert r.state is State.NEEDS_FIX


def test_du_p3_seul_ne_bute_pas_sur_les_cycles():
    # Le mineur ne consomme pas de cycle : il ne doit donc pas non plus etre
    # bloque par leur epuisement.
    r = d(pr(checks=(vert(),), threads=(fil(1, BADGE_P3),), review_cycle=3,
             checks_concluded_at=T0), max_review_cycles=3)
    assert r.state is State.READY_FOR_HUMAN


# ── Les cas ou il n'y a rien a faire ────────────────────────────────────────


def test_un_bail_detenu_arrete_tout():
    # Idempotence : un webhook recu deux fois ne lance pas deux agents.
    r = d(pr(lease_held=True, checks=(Check("x", "completed", "failure"),), threads=(fil(1),)))
    assert r.state is State.AGENT_WORKING


@pytest.mark.parametrize("champ", ["merged", "closed", "draft"])
def test_une_pr_fermee_ou_en_brouillon_ne_demande_rien(champ):
    r = d(pr(**{champ: True}, checks=(Check("x", "completed", "failure"),), threads=(fil(1),)))
    assert r.state is State.IDLE


def test_une_pr_verte_et_sans_remarque_attend_l_humain():
    r = d(pr(checks=(vert(),), checks_concluded_at=T0 - timedelta(hours=1), nudge_sent=True))
    assert r.state is State.READY_FOR_HUMAN
    assert Action.LABEL_READY in r.actions


# ── Idempotence et purete ───────────────────────────────────────────────────


def test_la_decision_est_stable_pour_un_meme_etat():
    p = pr(checks=(vert(),), threads=(fil(1),), checks_concluded_at=T0)
    a, b = d(p), d(p)
    assert (a.state, a.reason, a.actions) == (b.state, b.reason, b.actions)


def test_la_decision_ne_modifie_pas_l_instantane():
    p = pr(checks=(vert(),), threads=(fil(1),), checks_concluded_at=T0)
    avant = (p.review_cycle, p.last_handled_comment_id, p.nudge_sent)
    d(p)
    assert (p.review_cycle, p.last_handled_comment_id, p.nudge_sent) == avant


def test_chaque_decision_porte_une_raison_lisible():
    # « Pourquoi cet agent travaille ? » doit se lire sans ouvrir le code.
    cas = [
        pr(),
        pr(merged=True),
        pr(draft=True),
        pr(lease_held=True),
        pr(checks=(Check("x", "completed", "failure"),), checks_concluded_at=T0),
        pr(checks=(Check("x", "in_progress", None),)),
        pr(checks=(vert(),), threads=(fil(1),), checks_concluded_at=T0),
        pr(checks=(vert(),), threads=(fil(1, BADGE_P3),), checks_concluded_at=T0),
        pr(checks=(vert(),), checks_concluded_at=T0 - timedelta(minutes=2)),
        pr(checks=(vert(),), threads=(fil(1),), review_cycle=9, checks_concluded_at=T0),
    ]
    for p in cas:
        r = d(p)
        assert r.reason.endswith((".", ")")), r.reason
        assert len(r.reason) > 20, r.reason


# ── Une CI illisible n'est pas une CI verte ─────────────────────────────────


def test_une_ci_illisible_arrete_tout():
    # `checks=()` est ambigu : « aucun check » et « je n'ai pas pu les lire »
    # se ressemblent, et les deux donnent `failed=[]`, `pending=[]` — donc un
    # feu vert. Une PR dont la CI est rouge passerait pour bonne.
    d = decide(pr(checks_readable=False, threads=(fil(1),), **FINI),
               trusted_reviewers={CODEX}, max_review_cycles=3, now=T0)
    assert d.state is State.NEEDS_HUMAN
    assert "illisible" in d.reason
    assert Action.RUN_AGENT not in d.actions


def test_le_garde_passe_avant_les_fils_et_les_checks():
    # Meme avec un fil bloquant et des checks verts en memoire, l'illisibilite
    # gagne : on ne sait pas ce que la CI dit VRAIMENT.
    d = decide(pr(checks_readable=False, checks=(vert(),), threads=(fil(1),), **FINI),
               trusted_reviewers={CODEX}, max_review_cycles=3, now=T0)
    assert d.state is State.NEEDS_HUMAN


def test_une_ci_lisible_et_vide_reste_traitee_normalement():
    # Le drapeau ne doit pas transformer « ce depot n'a pas de CI » en panne :
    # `plantifia` n'a aucun workflow, et ses PR doivent rester exploitables.
    d = decide(pr(checks=(), threads=(fil(1),), **FINI),
               trusted_reviewers={CODEX}, max_review_cycles=3, now=T0)
    assert d.state is State.NEEDS_FIX


# ── La branche de tete, et ce qu'on en fait ─────────────────────────────────
# Une PR de livraison (`dev` -> `main`) n'est PAS ecartee : le job derive une
# branche de travail (`derived_branch`) et le correctif revient par une PR
# distincte. C'est le chemin normal du depot — cf. `test_job.py`, le cas reel
# de `backend#727` le 27/08/2026, ou refuser rendait l'agent inutile.


def test_une_branche_de_travail_normale_reste_traitee():
    # Le garde ne doit pas mordre sur le cas courant : une PR de correction qui
    # vise l'integration est exactement ce que l'agent doit prendre.
    d = decide(pr(head_ref="fix/718-ancre-gbif", base_ref="dev",
                  threads=(fil(1),), **FINI),
               trusted_reviewers={CODEX}, max_review_cycles=3, now=T0)
    assert d.state is State.NEEDS_FIX


def test_une_branche_inconnue_ne_declenche_pas_le_garde():
    # `head_ref` vide veut dire « je ne sais pas », pas « c'est partage ». Le
    # refus arrivera plus tard, en nommant ce qui manque.
    d = decide(pr(head_ref="", threads=(fil(1),), **FINI),
               trusted_reviewers={CODEX}, max_review_cycles=3, now=T0)
    assert d.state is State.NEEDS_FIX
