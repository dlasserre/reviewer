"""La reconciliation decide-t-elle avec TOUT ce qu'on sait ?

Ce module n'avait aucun test jusqu'au 27/08/2026, et c'est ce qui a laissé
passer le defaut le plus couteux du demon : la photo rendue par la forge etait
passee telle quelle a `decide`, alors que quatre de ses champs ne viennent pas
de la forge du tout.

Le job ECRIVAIT bien son curseur en base a la fin de chaque cycle. Personne ne
le relisait. L'agent aurait donc repris les memes remarques a chaque passage,
indefiniment — et `max_review_cycles`, la borne censee arreter ca, ne pouvait
pas se declencher puisque son compteur repartait de zero a chaque fois.

La panne etait invisible en lecture seule : `status` affichait `NEEDS_FIX`, ce
qui est exactement ce qu'on attend d'une PR avec des fils ouverts. Elle ne se
serait manifestee qu'une fois le demon arme, sur le quota.
"""

from __future__ import annotations

import textwrap
from dataclasses import replace
from datetime import timedelta

import pytest

from reviewer.config import load_profile
from reviewer.output.events import Journal
from reviewer.rules.machine import Action, Check, PullSnapshot, State, Thread
from reviewer.graph.sweep import sweep_profile
from reviewer.store.leases import PullState, StateStore

CODEX = "chatgpt-codex-connector[bot]"
P1 = "**![P1 Badge](https://img.shields.io/badge/P1-orange)**  Ne court-circuitez pas"


class LecteurFige:
    """Un lecteur de forge qui rend toujours la meme photo."""

    def __init__(self, pulls):
        self.pulls = list(pulls)
        self.appels = 0

    async def open_pulls(self, repo: str):
        self.appels += 1
        return [p for p in self.pulls if p.repo == repo]


def snapshot(**kw) -> PullSnapshot:
    base = dict(
        number=727, repo="backend", head_sha="abc", head_ref="fix/727-exemple", base_ref="dev",
        checks=(Check("Test backend", "completed", "success"),),
        threads=(Thread("PRRT_1", 1, CODEX, False, P1, "app/service.py", 229),),
        nudge_sent=True,
    )
    base.update(kw)
    return PullSnapshot(**base)


@pytest.fixture
def atelier(tmp_path):
    t = str(tmp_path).replace("\\", "/")
    (tmp_path / "p.yaml").write_text(textwrap.dedent(f"""
        project: essai
        workspace: {t}/ws
        forge:
          org: UneOrg
        reviewers:
          trust: [ "{CODEX}" ]
        max_review_cycles: 3
        repos:
          backend:
            access: write
            path: {t}/ws/backend
    """), encoding="utf-8")
    profil = load_profile(tmp_path / "p.yaml")
    store = StateStore(tmp_path / "state.db")
    journal = Journal(tmp_path / "logs", profile="essai")
    yield profil, store, journal
    store.close()


async def passer(atelier, pulls):
    profil, store, journal = atelier
    return await sweep_profile(profil, LecteurFige(pulls), journal, store)


# ── Le curseur de remarques ────────────────────────────────────────────────


async def test_sans_etat_local_une_remarque_neuve_est_du_travail(atelier):
    rapport = await passer(atelier, [snapshot()])
    assert rapport.outcomes[0].decision.state is State.NEEDS_FIX


async def test_le_curseur_ENREGISTRE_est_relu(atelier):
    # LE test du defaut. Le job a note « j'ai traite jusqu'au commentaire 1 ».
    # Sans relecture, la meme remarque repart en travail a chaque passage.
    profil, store, journal = atelier
    store.save_pull_state(PullState("essai", "backend", 727,
                                    last_handled_comment_id=1))

    rapport = await passer(atelier, [snapshot()])
    d = rapport.outcomes[0].decision

    assert Action.RUN_AGENT not in d.actions, d.reason
    assert d.state is State.READY_FOR_HUMAN


async def test_une_remarque_PLUS_RECENTE_que_le_curseur_reste_du_travail(atelier):
    profil, store, journal = atelier
    store.save_pull_state(PullState("essai", "backend", 727,
                                    last_handled_comment_id=1))
    neuve = snapshot(threads=(
        Thread("PRRT_1", 1, CODEX, False, P1, "a.py", 1),
        Thread("PRRT_9", 9, CODEX, False, P1, "b.py", 2),
    ))
    d = (await passer(atelier, [neuve])).outcomes[0].decision
    assert d.state is State.NEEDS_FIX
    assert [t.id for t in d.threads] == ["PRRT_9"]


# ── Le compteur de cycles ──────────────────────────────────────────────────


async def test_le_cycle_ENREGISTRE_est_relu(atelier):
    # Sans relecture, `max_review_cycles` ne se declenche JAMAIS : le compteur
    # repart de zero a chaque passage, et la boucle n'a plus de borne.
    profil, store, journal = atelier
    store.save_pull_state(PullState("essai", "backend", 727, review_cycle=3))

    d = (await passer(atelier, [snapshot()])).outcomes[0].decision

    assert d.state is State.NEEDS_HUMAN
    assert "cycle(s)" in d.reason
    assert Action.ASK_HUMAN in d.actions


async def test_le_cycle_relu_apparait_dans_la_photo_rendue(atelier):
    # `Outcome.snapshot` sert au job ET a l'affichage : une photo qui aurait
    # perdu le cycle ferait ecrire « cycle 1 » a chaque passage.
    profil, store, journal = atelier
    store.save_pull_state(PullState("essai", "backend", 727, review_cycle=2))
    o = (await passer(atelier, [snapshot()])).outcomes[0]
    assert o.snapshot.review_cycle == 2


# ── Le bail ────────────────────────────────────────────────────────────────


async def test_un_bail_DETENU_est_vu_par_la_reconciliation(atelier):
    # Sinon une PR en cours de traitement se relit comme du travail a prendre,
    # et le seul rempart restant est la course sur `acquire`.
    profil, store, journal = atelier
    store.acquire("essai", "backend", 727, "un-job", ttl=timedelta(minutes=30))

    d = (await passer(atelier, [snapshot()])).outcomes[0].decision
    assert d.state is State.AGENT_WORKING
    assert Action.RUN_AGENT not in d.actions


async def test_un_bail_EXPIRE_ne_bloque_plus(atelier):
    profil, store, journal = atelier
    store.acquire("essai", "backend", 727, "un-job-mort",
                  ttl=timedelta(seconds=-1))
    d = (await passer(atelier, [snapshot()])).outcomes[0].decision
    assert d.state is not State.AGENT_WORKING


# ── L'etat est par PROJET et par PR ────────────────────────────────────────


async def test_le_curseur_d_une_PR_ne_deteint_pas_sur_une_autre(atelier):
    profil, store, journal = atelier
    store.save_pull_state(PullState("essai", "backend", 727,
                                    last_handled_comment_id=99))
    autre = replace(snapshot(), number=728)
    d = (await passer(atelier, [autre])).outcomes[0].decision
    assert d.state is State.NEEDS_FIX


# ── Ce qui n'a pas change ──────────────────────────────────────────────────


async def test_un_depot_hors_ecriture_reste_hors_perimetre(atelier):
    profil, store, journal = atelier
    object.__setattr__(profil.repos["backend"], "access",
                       type(profil.repos["backend"].access).CONTEXT)
    rapport = await passer(atelier, [snapshot()])
    assert rapport.outcomes == ()
    assert rapport.skipped and rapport.skipped[0][0] == "backend"


# ── La liste `branches` du profil, enfin appliquee ──────────────────────────
# Elle etait declaree et validee au chargement, puis lue par personne. Un
# reglage de securite qui ne gouverne rien se lit pourtant comme une garantie.


async def test_une_branche_hors_des_motifs_du_profil_est_ecartee(atelier):
    rapport = await passer(atelier, [snapshot(head_ref="experimental/bidouille")])
    d = rapport.outcomes[0].decision
    assert d.state is State.IDLE
    assert Action.RUN_AGENT not in d.actions
    assert "hors des motifs" in d.reason
    # Le message doit nommer les motifs, sinon il faut aller lire le YAML.
    assert "feat/*" in d.reason


async def test_une_branche_conforme_au_profil_passe(atelier):
    rapport = await passer(atelier, [snapshot(head_ref="hotfix/730-urgence")])
    assert rapport.outcomes[0].decision.state is State.NEEDS_FIX


async def test_une_branche_inconnue_n_est_pas_ecartee_par_le_filtre(atelier):
    # « Je ne connais pas la branche » n'est pas « elle ne correspond pas ».
    # La refuser ici produirait un saut silencieux justifie par un message
    # nommant une branche vide ; l'echec explicite viendra du worktree.
    rapport = await passer(atelier, [snapshot(head_ref="")])
    assert "hors des motifs" not in rapport.outcomes[0].decision.reason


# ── Le perimetre par auteur ─────────────────────────────────────────────────
#
# Le reglage qui rend possible « un demon par developpeur ». Les baux vivent
# dans une base LOCALE : deux demons sur deux machines n'ont aucune exclusion
# mutuelle. Ce qui les empeche de se marcher dessus, ce n'est pas le bail, c'est
# le fait que leurs ensembles de travail soient DISJOINTS.


def _pr(numero: int, auteur: str) -> PullSnapshot:
    return PullSnapshot(
        number=numero, repo="backend", head_sha=f"sha{numero}",
        head_ref=f"fix/{numero}-truc", author=auteur,
        threads=(Thread(f"T{numero}", numero, "un-bot", False,
                        "**![P1 Badge](x)** truc", "a.py", 1),),
    )


class LecteurFige:
    def __init__(self, pulls):
        self._pulls = pulls

    async def open_pulls(self, repo):
        return list(self._pulls)


def _profil_avec(tmp_path, auteurs):
    t = str(tmp_path).replace("\\", "/")
    (tmp_path / "p.yaml").write_text(textwrap.dedent(f"""
        project: essai
        workspace: {t}
        forge:
          org: UneOrg
        reviewers:
          trust: [un-bot]
        scope:
          authors: {auteurs}
        repos:
          backend:
            access: write
            path: {t}
    """), encoding="utf-8")
    return load_profile(tmp_path / "p.yaml")


async def test_hors_perimetre_la_PR_n_est_PAS_balayee(tmp_path):
    profil = _profil_avec(tmp_path, ["moi"])
    store = StateStore(tmp_path / "s.db")
    try:
        rapport = await sweep_profile(
            profil, LecteurFige([_pr(1, "moi"), _pr(2, "quelqu-un-dautre")]),
            Journal(tmp_path / "logs"), store)
    finally:
        store.close()
    assert [o.number for o in rapport.outcomes] == [1]


async def test_hors_perimetre_ne_compte_PAS_comme_rien_a_faire(tmp_path):
    # Une PR qui n'appartient pas a ce demon ne doit pas figurer au bilan : l'y
    # compter ferait croire qu'on s'en occupe et qu'il n'y a rien a y faire.
    profil = _profil_avec(tmp_path, ["moi"])
    store = StateStore(tmp_path / "s.db")
    try:
        rapport = await sweep_profile(
            profil, LecteurFige([_pr(9, "quelqu-un-dautre")]),
            Journal(tmp_path / "logs"), store)
    finally:
        store.close()
    assert rapport.outcomes == ()
    assert "aucune PR" in rapport.summary()


async def test_un_perimetre_VIDE_prend_tout(tmp_path):
    # Le cas du demon UNIQUE, partage par une equipe : une absence d'avis ne
    # doit pas se lire comme une interdiction totale.
    profil = _profil_avec(tmp_path, [])
    store = StateStore(tmp_path / "s.db")
    try:
        rapport = await sweep_profile(
            profil, LecteurFige([_pr(1, "moi"), _pr(2, "toi")]),
            Journal(tmp_path / "logs"), store)
    finally:
        store.close()
    assert sorted(o.number for o in rapport.outcomes) == [1, 2]


async def test_la_casse_du_login_n_exclut_personne(tmp_path):
    # Un perimetre qui ne reconnait personne est un demon qui ne fait rien, sans
    # que rien ne dise pourquoi. C'est exactement la panne muette qu'on evite.
    profil = _profil_avec(tmp_path, ["MOI"])
    store = StateStore(tmp_path / "s.db")
    try:
        rapport = await sweep_profile(
            profil, LecteurFige([_pr(1, "moi")]), Journal(tmp_path / "logs"), store)
    finally:
        store.close()
    assert [o.number for o in rapport.outcomes] == [1]
