"""Les baux tiennent-ils l'idempotence ?

C'est le seul etat reellement stocke du demon, et donc le seul endroit ou une
erreur produit deux agents sur la meme PR. Les cas ci-dessous sont ceux de la
liste de pannes : webhook duplique, crash, reboot, PID reutilise, job en cours.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from reviewer.store.leases import PullState, StateStore, pid_alive

T0 = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    with StateStore(tmp_path / "state.db") as s:
        yield s


# ── Vivacite d'un PID ───────────────────────────────────────────────────────


def test_le_processus_courant_est_vivant():
    # Et surtout : ce test ne doit PAS tuer le processus. `os.kill(pid, 0)`
    # sous Windows appelle TerminateProcess — le « test » supprimerait la suite.
    assert pid_alive(os.getpid()) is True


def test_un_pid_invraisemblable_est_mort():
    assert pid_alive(0) is False
    assert pid_alive(-1) is False


def test_un_pid_libre_est_mort():
    # 999999 depasse le plafond usuel des deux plateformes.
    assert pid_alive(999_999) is False


# ── Prise de bail ───────────────────────────────────────────────────────────


def test_on_prend_un_bail_libre(store):
    b = store.acquire("p", "backend", 714, "j1", now=T0)
    assert b is not None and b.job_id == "j1"
    assert store.lease("p", "backend", 714).job_id == "j1"


def test_un_second_preneur_est_refuse(store):
    # LE cas d'idempotence : un webhook recu deux fois, ou deux passages de
    # reconciliation qui se chevauchent.
    assert store.acquire("p", "backend", 714, "j1", now=T0) is not None
    assert store.acquire("p", "backend", 714, "j2", now=T0) is None


def test_deux_pr_differentes_ne_se_genent_pas(store):
    assert store.acquire("p", "backend", 714, "j1", now=T0) is not None
    assert store.acquire("p", "backend", 715, "j2", now=T0) is not None


def test_deux_profils_ne_se_genent_pas(store):
    assert store.acquire("a", "backend", 714, "j1", now=T0) is not None
    assert store.acquire("b", "backend", 714, "j2", now=T0) is not None


# ── Expiration et reprise ───────────────────────────────────────────────────


def test_un_bail_expire_est_reprenable(store):
    store.acquire("p", "backend", 714, "j1", ttl=timedelta(minutes=30), now=T0)
    plus_tard = T0 + timedelta(minutes=31)
    assert store.acquire("p", "backend", 714, "j2", now=plus_tard) is not None
    assert store.lease("p", "backend", 714).job_id == "j2"


def test_un_bail_dont_le_processus_est_mort_est_reprenable(store):
    # Crash, reboot : le PID n'existe plus, on n'attend pas l'expiration.
    store.acquire("p", "backend", 714, "j1", pid=999_999, ttl=timedelta(hours=8), now=T0)
    assert store.acquire("p", "backend", 714, "j2", now=T0) is not None


def test_un_bail_vivant_et_non_expire_resiste(store):
    store.acquire("p", "backend", 714, "j1", pid=os.getpid(),
                  ttl=timedelta(minutes=30), now=T0)
    assert store.acquire("p", "backend", 714, "j2", now=T0 + timedelta(minutes=5)) is None


def test_l_expiration_prime_sur_la_vivacite_du_pid(store):
    # Les PID se reutilisent : un bail dont le PID est vivant peut appartenir a
    # un tout autre programme. L'expiration est la garantie, le PID un
    # raccourci.
    store.acquire("p", "backend", 714, "j1", pid=os.getpid(),
                  ttl=timedelta(minutes=1), now=T0)
    assert store.acquire("p", "backend", 714, "j2", now=T0 + timedelta(minutes=2)) is not None


# ── Renouvellement ──────────────────────────────────────────────────────────


def test_le_detenteur_prolonge_son_bail(store):
    b = store.acquire("p", "backend", 714, "j1", ttl=timedelta(minutes=30), now=T0)
    prolonge = store.renew(b, ttl=timedelta(minutes=30), now=T0 + timedelta(minutes=20))
    assert prolonge is not None
    # Le bail devait expirer a T0+30 ; il court maintenant jusqu'a T0+50.
    assert store.acquire("p", "backend", 714, "j2", now=T0 + timedelta(minutes=31)) is None


def test_un_ancien_detenteur_ne_reprend_pas_par_surprise(store):
    perdu = store.acquire("p", "backend", 714, "j1", ttl=timedelta(minutes=1), now=T0)
    store.acquire("p", "backend", 714, "j2", now=T0 + timedelta(minutes=2))
    # `j1` a perdu le bail entre-temps : son renouvellement ne doit rien faire.
    assert store.renew(perdu, now=T0 + timedelta(minutes=3)) is None
    assert store.lease("p", "backend", 714).job_id == "j2"


# ── Liberation ──────────────────────────────────────────────────────────────


def test_liberer_rend_le_bail(store):
    b = store.acquire("p", "backend", 714, "j1", now=T0)
    store.release(b)
    assert store.lease("p", "backend", 714) is None
    assert store.acquire("p", "backend", 714, "j2", now=T0) is not None


def test_liberer_deux_fois_ne_coute_rien(store):
    b = store.acquire("p", "backend", 714, "j1", now=T0)
    store.release(b)
    store.release(b)          # ne leve pas
    assert store.lease("p", "backend", 714) is None


def test_liberer_ne_prend_pas_le_bail_d_un_autre(store):
    ancien = store.acquire("p", "backend", 714, "j1", ttl=timedelta(minutes=1), now=T0)
    store.acquire("p", "backend", 714, "j2", now=T0 + timedelta(minutes=2))
    store.release(ancien)     # `j1` libere le SIEN, pas celui de `j2`
    assert store.lease("p", "backend", 714).job_id == "j2"


# ── Balayage au demarrage ───────────────────────────────────────────────────


def test_le_balayage_retire_les_baux_morts_et_les_rend(store):
    store.acquire("p", "backend", 714, "mort", pid=999_999, ttl=timedelta(hours=8), now=T0)
    store.acquire("p", "backend", 715, "vivant", pid=os.getpid(),
                  ttl=timedelta(hours=8), now=T0)

    retires = store.sweep_dead(now=T0)

    # Rendus, pas juste supprimes : un bail nettoye en silence ferait
    # disparaitre la trace d'un job interrompu.
    assert [b.job_id for b in retires] == ["mort"]
    assert store.lease("p", "backend", 714) is None
    assert store.lease("p", "backend", 715) is not None


def test_les_baux_actifs_se_listent(store):
    store.acquire("p", "backend", 714, "vivant", pid=os.getpid(), now=T0)
    store.acquire("p", "backend", 715, "mort", pid=999_999, now=T0)
    assert [b.job_id for b in store.active_leases(now=T0)] == ["vivant"]


# ── Suivi par PR ────────────────────────────────────────────────────────────


def test_une_pr_inconnue_rend_un_etat_neutre(store):
    s = store.pull_state("p", "backend", 999)
    assert s.review_cycle == 0
    assert s.last_handled_comment_id == 0
    assert s.claude_session is None
    assert s.nudge_sent is False


def test_l_etat_se_relit_apres_ecriture(store):
    store.save_pull_state(PullState("p", "backend", 714, claude_session="5b3f2c1a",
                                    worktree="C:/wt/x", review_cycle=2,
                                    last_handled_comment_id=3852553052, nudge_sent=True))
    s = store.pull_state("p", "backend", 714)
    assert s.claude_session == "5b3f2c1a"
    assert s.review_cycle == 2
    assert s.last_handled_comment_id == 3852553052
    assert s.nudge_sent is True


def test_l_etat_survit_a_une_reouverture_de_la_base(tmp_path):
    chemin = tmp_path / "state.db"
    with StateStore(chemin) as s:
        s.save_pull_state(PullState("p", "backend", 714, claude_session="abc", review_cycle=1))
    # Reboot Windows, redemarrage du demon.
    with StateStore(chemin) as s:
        assert s.pull_state("p", "backend", 714).claude_session == "abc"


def test_le_curseur_ne_recule_jamais(store):
    store.bump_cursor("p", "backend", 714, 200)
    # Deux jobs qui se terminent dans le desordre : le plus ancien ne doit pas
    # rouvrir des remarques deja soldees — un cycle brule pour rien.
    store.bump_cursor("p", "backend", 714, 100)
    assert store.pull_state("p", "backend", 714).last_handled_comment_id == 200


def test_le_curseur_avance(store):
    store.bump_cursor("p", "backend", 714, 100)
    store.bump_cursor("p", "backend", 714, 300)
    assert store.pull_state("p", "backend", 714).last_handled_comment_id == 300


def test_le_curseur_n_ecrase_pas_le_reste(store):
    store.save_pull_state(PullState("p", "backend", 714, claude_session="abc", review_cycle=2))
    store.bump_cursor("p", "backend", 714, 500)
    s = store.pull_state("p", "backend", 714)
    assert (s.claude_session, s.review_cycle, s.last_handled_comment_id) == ("abc", 2, 500)


def test_le_suivi_est_independant_du_bail(store):
    # Un bail relache ne doit pas effacer la session Claude : c'est justement
    # elle qu'on veut retrouver au cycle suivant.
    store.save_pull_state(PullState("p", "backend", 714, claude_session="abc"))
    b = store.acquire("p", "backend", 714, "j1", now=T0)
    store.release(b)
    assert store.pull_state("p", "backend", 714).claude_session == "abc"


def test_replace_conserve_les_champs_non_touches(store):
    s = store.save_pull_state(PullState("p", "backend", 714, claude_session="abc",
                                        review_cycle=1))
    store.save_pull_state(replace(s, review_cycle=2))
    assert store.pull_state("p", "backend", 714).claude_session == "abc"
