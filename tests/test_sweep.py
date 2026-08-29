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

from agent_runner_lg.config import load_profile
from agent_runner_lg.output.events import Journal
from agent_runner_lg.rules.machine import Action, Check, PullSnapshot, State, Thread
from agent_runner_lg.graph.sweep import sweep_profile
from agent_runner_lg.store.leases import PullState, StateStore

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
